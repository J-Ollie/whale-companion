"""Loopback-only QQ diagnostics. No automatic login, restart or message sending."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
import urllib.request
from urllib.parse import urlsplit, parse_qs
from library import Library
from panel_auth import PanelAuth
from archive import Archive
from visitors import Visitors
from balance import DeepSeekBalance
from phone_health import PhoneHealth, project as phone_public
from billing import DeepSeekBilling
from platform_billing import PlatformBilling
from presence import PublicPresence
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(os.environ.get('QQ_RECOVERY_ROOT', '/opt/qq-companion'))
DATA = ROOT / 'recovery-data'
OWNER, BOT = '100000001', '100000002'
NAPCAT, ASTRBOT = 'qq-companion-napcat', 'qq-companion-astrbot'
COOLDOWN = 600
ANSI = re.compile(r'\x1b\[[0-9;]*m')
WEB = Path(__file__).parent / 'web'


def command(args, timeout=12):
    result = subprocess.run(args, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Command failed: ' + args[0])
    return result.stdout


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    temp.chmod(0o600)
    temp.replace(path)


class NapCat:
    def __init__(self):
        self.credential = None
        self.expires = 0
        self.lock = threading.RLock()

    def request(self, route, body, authenticated=True):
        with self.lock:
            if authenticated and time.time() >= self.expires:
                env = dict(line.split('=', 1) for line in (ROOT / 'deployment.env').read_text().splitlines() if '=' in line)
                digest = hashlib.sha256((env['NAPCAT_WEBUI_TOKEN'] + '.napcat').encode()).hexdigest()
                reply = self.request('/auth/login', {'hash': digest}, False)
                self.credential = reply['data']['Credential']
                self.expires = time.time() + 600
            headers = {'Content-Type': 'application/json'}
            if authenticated:
                headers['Authorization'] = 'Bearer ' + self.credential
            req = urllib.request.Request('http://127.0.0.1:6099/api' + route,
                                         data=json.dumps(body).encode(), headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    result = json.load(response)
                if result.get('code') not in (0, None):
                    self.expires = 0
                return result
            except Exception:
                self.expires = 0
                raise

    def status(self):
        result = self.request('/QQLogin/CheckLoginStatus', {})
        if result.get('code') != 0:
            raise RuntimeError('QQ management query failed')
        d = result.get('data') or {}
        return {k: d.get(k) for k in ('isLogin', 'isOffline', 'coreReady', 'loginPhase')}


class SnowLuma:
    """Use the authenticated OneBot interface; never export QQ cookies."""
    def request(self, action):
        credentials = json.loads((ROOT / 'snowluma/credentials.json').read_text())
        request = urllib.request.Request('http://127.0.0.1:3000/' + action,
            data=b'{}', headers={'Content-Type': 'application/json',
                                 'Authorization': 'Bearer ' + credentials['onebot_token']})
        with urllib.request.urlopen(request, timeout=6) as response:
            result = json.load(response)
        if result.get('status') != 'ok' or result.get('retcode') != 0:
            raise RuntimeError('SnowLuma action unavailable')
        return result.get('data') or {}

    def status(self):
        try:
            status = self.request('get_status')
            account = self.request('get_login_info')
            ready = str(account.get('user_id')) == BOT and status.get('online') is True
            return {'isLogin': ready, 'isOffline': not ready, 'coreReady': ready,
                    'loginPhase': 'ready' if ready else 'not_ready'}
        except Exception:
            return {'isLogin': False, 'isOffline': False, 'coreReady': False,
                    'loginPhase': 'waiting_login_or_hook'}


class Monitor:
    def __init__(self):
        DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_file = DATA / 'state.json'
        # Corrupt recovery accounting must not silently reset the rate limit.
        self.persist = json.loads(self.state_file.read_text()) if self.state_file.exists() else {'last_recovery': 0}
        self.lock = threading.RLock()
        self.probe_lock = threading.Lock()
        self.action_lock = threading.Lock()
        gateway_file = ROOT / 'gateway.json'
        gateway = json.loads(gateway_file.read_text()) if gateway_file.exists() else {}
        self.backend = gateway.get('backend', 'napcat')
        self.gateway = 'qq-companion-snowluma' if self.backend == 'snowluma' else NAPCAT
        self.api = SnowLuma() if self.backend == 'snowluma' else NapCat()
        self.snapshot = {'checked_at': 0, 'level': 'unknown', 'summary': '正在检查云端状态'}
        self.challenge = None
        self.operation = {'state': 'idle', 'message': ''}
        self.stop = threading.Event()
        self.phone = PhoneHealth(ROOT,command,ASTRBOT)

    def event(self, kind, details):
        # Only fixed diagnostic metadata belongs in this journal.
        path = DATA / 'events.jsonl'
        with self.lock:
            if path.exists() and path.stat().st_size > 1024 * 1024:
                path.replace(DATA / 'events.previous.jsonl')
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'at': time.time(), 'kind': kind, **details}, ensure_ascii=False) + '\n')
            path.chmod(0o600)

    def logs(self, container):
        result = subprocess.run(['docker', 'logs', '--since', '10m', '--tail', '500', container],
                                capture_output=True, timeout=10)
        if result.returncode:
            raise RuntimeError('Log query failed')
        return ANSI.sub('', (result.stdout + result.stderr).decode('utf-8', errors='replace'))

    def diagnose(self, nc, ab):
        with self.lock:
            item = self.challenge
            if not item:
                return
            # Exact OneBot receive prefix from the real owner, not an outgoing quote.
            incoming = f'接收 <- 私聊 ({OWNER})'
            item['napcat_received'] = item['napcat_received'] or any(incoming in x and item['text'] in x for x in nc.splitlines())
            item['astrbot_received'] = item['astrbot_received'] or any(
                'core.event_bus:' in x and f'/{OWNER}:' in x and item['text'] in x for x in ab.splitlines())
            if item['astrbot_received']:
                if item['first_received_at'] == 0:
                    item['first_received_at'] = time.time()
                # Never equate an unrelated proactive send with this request's reply.
                item['result'] = 'received'
            elif time.time() > item['expires']:
                item['result'] = 'not_received'
            else:
                item['result'] = 'waiting'

    def probe(self):
        if not self.probe_lock.acquire(blocking=False):
            return
        try:
            result = {'checked_at': time.time(), 'containers': {}, 'panel': False, 'onebot': False,
                      'qq': {}, 'level': 'unknown', 'summary': ''}
            try:
                rows = json.loads(command(['docker', 'inspect', self.gateway, ASTRBOT]))
                result['containers'] = {r['Name'].lstrip('/'): {'running': bool(r['State']['Running']),
                                                             'oom': bool(r['State'].get('OOMKilled'))} for r in rows}
            except Exception:
                result['summary'] = '容器状态读取失败，请检查服务器服务'
            try:
                with urllib.request.urlopen('http://127.0.0.1:6185/', timeout=4) as response:
                    result['panel'] = response.status == 200
            except Exception:
                pass
            try:
                code = "from pathlib import Path;print(sum(1 for p in ('/proc/net/tcp','/proc/net/tcp6') for l in Path(p).read_text().splitlines()[1:] if l.split()[1].endswith(':1837') and l.split()[3]=='01'))"
                result['onebot'] = int(command(['docker', 'exec', ASTRBOT, 'python', '-c', code]).strip()) > 0
            except Exception:
                pass
            try:
                result['qq'] = self.api.status()
            except Exception:
                pass
            try:
                self.diagnose(self.logs(self.gateway), self.logs(ASTRBOT))
            except Exception:
                pass
            result.update(backend=self.backend, gateway_container=self.gateway)
            result['phone']=self.phone.query()
            if not all(result['containers'].get(n, {}).get('running') for n in (self.gateway, ASTRBOT)):
                result.update(level='error', summary='服务容器未运行或无法读取')
            elif not result['qq']:
                result.update(level='warning', summary='QQ 管理接口无响应，不能判断账号在线')
            elif not result['qq'].get('isLogin') or result['qq'].get('isOffline'):
                result.update(level='error', summary='QQ 未登录或已离线，需要恢复登录')
            elif not result['panel'] or not result['onebot']:
                result.update(level='error', summary='QQ 接入与聊天服务之间连接异常')
            else:
                result.update(level='unverified', summary='服务连接已建立，实际消息收发情况有待验证。')
            with self.lock:
                challenge = self.challenge
                if challenge and challenge['result'] == 'not_received':
                    if result['phone']['rhythm']['mode'] in ('asleep','resting') and challenge.get('napcat_received'):
                        result.update(level='unverified',summary='QQ 已接收测试消息，小鲸鱼正在休息或睡觉；即时回复不作为连接故障依据。')
                    else:
                        result.update(level='error', summary='本轮测试超时未到达聊天服务，可能假在线；请确认已发送测试文字')
                elif challenge and challenge['result'] == 'received' and time.time() - challenge['first_received_at'] < 300 and result['level'] == 'unverified':
                    result.update(level='received', summary='近期测试消息已到达聊天服务；回复是否收到仍需你确认')
                old = (self.snapshot.get('level'), self.snapshot.get('summary'))
                old_phone=self.snapshot.get('phone',{}).get('status')
                self.snapshot = result
            if old != (result['level'], result['summary']):
                self.event('health_changed', {'level': result['level'], 'summary': result['summary']})
            if old_phone and old_phone!=result['phone']['status']:
                self.event('phone_connection_changed',{'message':'备用机：'+result['phone']['label']})
        finally:
            self.probe_lock.release()

    def status(self):
        with self.lock:
            path = DATA / 'events.jsonl'
            events = []
            if path.exists():
                for line in path.read_text(encoding='utf-8').splitlines()[-15:]:
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        pass
            return {**self.snapshot, 'challenge': self.challenge, 'operation': self.operation,
                    'recovery_wait': max(0, int(COOLDOWN - (time.time() - self.persist['last_recovery']))),
                    'events': events}

    def check_phone(self):
        value=self.phone.query()
        with self.lock:
            old=self.snapshot.get('phone',{}).get('status')
            self.snapshot['phone']=value
        if old and old!=value['status']:
            self.event('phone_connection_changed',{'message':'备用机：'+value['label']})
        return value

    def reconnect_phone(self,ip,port):
        value=self.phone.reconnect(ip,port)
        with self.lock:self.snapshot['phone']=value
        self.event('phone_connection_changed',{'message':'备用机无线调试已验证并更新连接设置'})
        return value

    def new_challenge(self):
        with self.lock:
            self.challenge = {'text': '连通检查' + secrets.token_hex(3), 'created': time.time(),
                              'expires': time.time() + 120, 'napcat_received': False,
                              'astrbot_received': False, 'first_received_at': 0, 'result': 'waiting'}
            return self.challenge.copy()

    def refresh_qr(self):
        if self.backend == 'snowluma':
            raise ValueError('请打开 SnowLuma 远程桌面，在 QQ 登录窗口扫码或刷新二维码')
        if not self.action_lock.acquire(blocking=False):
            raise ValueError('恢复操作进行中，请稍后')
        try:
            if self.api.status().get('isLogin'):
                raise ValueError('后台仍声称已登录；如测试无法到达，请使用一次恢复接入')
            result = self.api.request('/QQLogin/RefreshQRcode', {})
            if result.get('code') != 0:
                raise ValueError('二维码暂时无法刷新，请查看 QQ 登录管理')
            self.event('qr_refreshed', {})
        finally:
            self.action_lock.release()

    def qr(self):
        if self.backend == 'snowluma':
            raise ValueError('SnowLuma 请在远程桌面的 QQ 登录窗口扫码')
        if self.api.status().get('isLogin'):
            raise ValueError('当前后台声称已登录，不展示旧二维码')
        data = command(['docker', 'exec', NAPCAT, 'cat', '/app/napcat/cache/qrcode.png'])
        if not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data) > 1024 * 1024:
            raise ValueError('当前没有可用二维码')
        return data

    def recover(self):
        if not self.action_lock.acquire(blocking=False):
            raise ValueError('恢复操作已在进行，请勿重复操作')
        try:
            with self.lock:
                if time.time() - self.persist['last_recovery'] < COOLDOWN:
                    raise ValueError('恢复有 10 分钟冷却，请勿反复重登')
                self.persist['last_recovery'] = time.time()
                atomic_json(self.state_file, self.persist)
                self.operation = {'state': 'running', 'message': '正在重启 QQ 接入，之后仅尝试一次保存的登录态'}
            self.event('recovery_requested', {})
            threading.Thread(target=self.recover_worker, daemon=True).start()
        except Exception:
            self.action_lock.release()
            raise

    def recover_worker(self):
        message = '恢复未完成，请查看 QQ 登录管理；没有循环重试'
        try:
            command(['docker', 'restart', self.gateway], timeout=45)
            if self.backend == 'snowluma':
                message = 'SnowLuma 已重启一次，请在远程桌面确认 QQ 登录；不会自动循环重登'
                self.event('recovery_finished', {'message': message})
                return
            self.api.expires = 0
            deadline = time.time() + 50
            while time.time() < deadline:
                try:
                    status = self.api.status()
                    break
                except Exception:
                    time.sleep(2)
            else:
                raise RuntimeError('Management endpoint unavailable')
            if status.get('isLogin'):
                message = '接入已恢复连接，请发送测试文字核验实际收发'
            else:
                # One login action only. Failures need the human, never an automatic retry loop.
                answer = self.api.request('/QQLogin/SetQuickLogin', {'uin': BOT})
                if answer.get('code') == 0:
                    message = '已提交一次保存登录态恢复，请检查状态；如需授权请到 QQ 管理页确认'
                else:
                    self.api.request('/QQLogin/RefreshQRcode', {})
                    message = '保存的登录态无法恢复，请用机器人小号扫码；不会再次自动登录'
            self.event('recovery_finished', {'message': message})
        except Exception:
            self.event('recovery_failed', {'message': message})
        finally:
            with self.lock:
                self.operation = {'state': 'finished', 'message': message}
            self.action_lock.release()
            self.probe()

    def run(self):
        while not self.stop.is_set():
            try:
                self.probe()
            except Exception:
                self.event('monitor_error', {'message': '检查异常，等待下一次定期检查'})
            self.stop.wait(5 if self.challenge and self.challenge['result'] == 'waiting' else 60)


def guest_status(snapshot):
    """Only the fixed status fields rendered by the visitor view; no management metadata."""
    result={k:snapshot.get(k) for k in ('checked_at','level','summary','backend','gateway_container')}
    names=(ASTRBOT,'qq-companion-snowluma','qq-companion-napcat')
    result['containers']={name:{'running':bool(row.get('running'))} for name,row in snapshot.get('containers',{}).items() if name in names and isinstance(row,dict)}
    result['qq']={'isLogin':bool((snapshot.get('qq') or {}).get('isLogin'))}
    result['phone']=phone_public(snapshot.get('phone'))
    result['events']=[{k:row[k] for k in ('at','summary','message','kind') if k in row} for row in snapshot.get('events',[]) if isinstance(row,dict)]
    challenge=snapshot.get('challenge')
    result['challenge']={k:challenge[k] for k in ('text','created','expires','napcat_received','astrbot_received','first_received_at','result') if k in challenge} if isinstance(challenge,dict) else None
    return result


def make_handler(monitor, token, public_origin='', public_token='', billing_service=None):
    library=Library(ROOT)
    archive=Archive(ROOT)
    auth=PanelAuth(ROOT/'recovery-data')
    visitors=Visitors(ROOT)
    balance=DeepSeekBalance(ROOT)
    billing=billing_service or PlatformBilling(ROOT)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No access log containing fragments, QR data or authentication.

        def reply(self, code, payload, content_type='application/json; charset=utf-8'):
            if not isinstance(payload, bytes):
                payload = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            if getattr(self,'visitor_cookie',None):
                self.send_header('Set-Cookie','whale_visitor='+self.visitor_cookie+'; Path=/; Max-Age=15552000; HttpOnly; SameSite=Lax'+('; Secure' if self.public_request() else ''))
            if getattr(self,'backend_cookie',None) is not None:
                self.send_header('Set-Cookie',self.backend_cookie)
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(payload)

        def public_request(self):
            return bool(public_origin) and self.headers.get('Host') == '127.0.0.1:6190' and (
                self.headers.get('X-Forwarded-Proto') == 'https' and
                self.headers.get('X-Forwarded-Host') == urlsplit(public_origin).netloc and
                self.headers.get('Origin') in (None, public_origin))

        def authorized(self):
            supplied = self.headers.get('X-Recovery-Key', '')
            host = self.headers.get('Host', '')
            origin = self.headers.get('Origin')
            local = host in ('127.0.0.1:16190', 'localhost:16190', '127.0.0.1:6190') and (
                origin is None or origin == 'http://' + host)
            return (local and hmac.compare_digest(supplied, token)) or (
                self.public_request() and bool(public_token) and hmac.compare_digest(supplied, public_token))

        def visitor_address(self):
            # Only our authenticated loopback HTTPS proxy may supply X-Real-IP.
            # Nginx overwrites this header with its actual remote_addr.
            if (self.client_address[0] in ('127.0.0.1','::1') and self.public_request()
                    and public_token and hmac.compare_digest(self.headers.get('X-Recovery-Key',''),public_token)):
                return self.headers.get('X-Real-IP')
            return None

        def visitor_identity(self):
            cookie=SimpleCookie()
            try:cookie.load(self.headers.get('Cookie',''))
            except Exception:pass
            value=cookie.get('whale_visitor')
            identity,token,fresh=visitors.identity(value.value if value else None)
            if fresh:self.visitor_cookie=token
            return identity

        def do_GET(self):
            assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                      '/style.css': ('style.css', 'text/css; charset=utf-8'),
                      '/whale-girl.webp': ('whale-girl.webp', 'image/webp'),
                      '/companion-winter-awake.webp': ('companion-winter-awake.webp', 'image/webp'),
                      '/companion-winter-sleep.webp': ('companion-winter-sleep.webp', 'image/webp'),
                      '/companion-maid-awake.webp': ('companion-maid-awake.webp', 'image/webp'),
                      '/companion-maid-sleep.webp': ('companion-maid-sleep.webp', 'image/webp')}
            if self.path in assets:
                name, mime = assets[self.path]
                self.reply(200, (WEB / name).read_bytes(), mime)
                return
            if not self.authorized():
                self.reply(403, {'error': '请通过本地“检查与恢复”入口打开'})
                return
            if self.path=='/api/backend-auth':
                cookie=SimpleCookie()
                try:cookie.load(self.headers.get('Cookie',''))
                except Exception:pass
                value=cookie.get('whale_backend')
                allowed=self.public_request() and auth.role(value.value if value else '')=='admin'
                self.reply(204,b'') if allowed else self.reply(403,{'error':'请先在管理中心以管理员身份登录'})
                return
            if self.path=='/api/auth/info':
                self.reply(200,{**auth.info(), 'public_access': self.public_request()})
                return
            role=auth.role(self.headers.get('X-Panel-Session',''))
            if role is None:
                self.reply(401,{'error':'请先登录','code':'login_required'})
                return
            try:
                route=urlsplit(self.path)
                query=parse_qs(route.query)
                if role=='guest' and not (route.path in ('/api/status','/api/archive') or (route.path=='/api/diaries' and query.get('book',['public'])[0]=='public')):
                    self.reply(403,{'error':'访客无权访问此内容','code':'guest_forbidden'})
                    return
                if route.path=='/api/usage':
                    self.reply(200,billing.query())
                    return
                if route.path=='/api/phone-settings':
                    self.reply(200,monitor.phone.settings())
                    return
                if route.path=='/api/balance':
                    self.reply(200,balance.query())
                    return
                if route.path=='/api/archive':
                    self.reply(200,archive.snapshot(guest=role=='guest',day=query.get('day',[''])[0]))
                    return
                if route.path=='/api/visitors':
                    self.reply(200,visitors.snapshot(int(query.get('before',['0'])[0])))
                    return
                if route.path=='/api/memories':
                    # Guest whitelist above rejects this route before any database access.
                    self.reply(200,library.memories(qq=query.get('qq',[''])[0],kind=query.get('kind',[''])[0],
                        term=query.get('term',[''])[0],state=query.get('state',['active'])[0],
                        q=query.get('q',[''])[0],before=int(query.get('before',['0'])[0])))
                    return
                if route.path=='/api/memory-evidence':
                    self.reply(200,library.memory_evidence(query.get('qq',[''])[0],query.get('id',[''])[0]))
                    return
                if route.path in ('/api/chats','/api/diaries'):
                    before=int(query.get('before',['0'])[0])
                    if not 0<=before<=1000000000:raise ValueError('分页参数无效')
                    if route.path=='/api/chats':
                        data=library.chats(query.get('qq',[''])[0],before)
                    else:data=library.diaries(query.get('book',['public'])[0],before)
                    self.reply(200,data)
                    return
                if route.path.startswith('/api/chat-media/'):
                    data,mime=library.media(route.path.removeprefix('/api/chat-media/'))
                    self.reply(200,data,mime)
                    return
                if self.path == '/api/status':
                    self.reply(200, guest_status(monitor.status()) if role=='guest' else monitor.status())
                elif self.path == '/api/qr':
                    self.reply(200, monitor.qr(), 'image/png')
                else:
                    self.reply(404, {'error': '未知入口'})
            except Exception as exc:
                self.reply(409, {'error': str(exc) if isinstance(exc, ValueError) else '读取失败，请稍后再试'})

        def do_POST(self):
            if not self.authorized() or self.headers.get('Content-Type') != 'application/json':
                self.reply(403, {'error': '请求未授权'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                limit=4096
                if self.path=='/api/usage/import':
                    if auth.role(self.headers.get('X-Panel-Session',''))!='admin':
                        self.reply(403,{'error':'仅管理员可导入账单'})
                        return
                    limit=2*1024*1024
                if not 0 <= length <= limit:
                    raise ValueError('请求过大')
                body=json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(body,dict):raise ValueError('请求格式不正确')
                if self.path in ('/api/auth/setup','/api/auth/login'):
                    if self.path.endswith('/setup') and self.public_request():
                        self.reply(403, {'error':'请通过桌面入口设置管理密码'})
                        return
                    try:
                        value=auth.setup(body.get('password'),body.get('confirm')) if self.path.endswith('/setup') else auth.login(body.get('password'))
                        self.reply(200,value)
                    except ValueError as exc:
                        self.reply(400,{'error':str(exc)})
                    return
                if self.path=='/api/auth/guest':
                    self.backend_cookie='whale_backend=; Path=/manage/; Max-Age=0; HttpOnly; Secure; SameSite=Strict'
                    try:self.reply(200,auth.guest())
                    except ValueError as exc:self.reply(400,{'error':str(exc)})
                    return
                role=auth.role(self.headers.get('X-Panel-Session',''))
                if role is None:
                    self.reply(401,{'error':'请先登录','code':'login_required'})
                    return
                if self.path in ('/api/visit','/api/visitor-profile','/api/guestbook'):
                    if role!='guest':
                        self.reply(403,{'error':'此入口用于访客自愿填写，管理员浏览不计入访客记录'})
                        return
                    visitor= self.visitor_identity()
                    if self.path=='/api/visit':data=visitors.visit(visitor,body.get('page'),self.visitor_address())
                    elif self.path=='/api/visitor-profile':data=visitors.profile(visitor,body.get('nickname',''))
                    else:data=visitors.note(visitor,body.get('nickname',''),body.get('message'),body.get('submission_id'))
                    self.reply(200,data)
                    return
                if role=='guest' and self.path not in ('/api/check','/api/test'):
                    self.reply(403,{'error':'访客无权执行此操作','code':'guest_forbidden'})
                    return
                if self.path == '/api/usage/import':
                    if billing.path.exists():raise ValueError('自动同步已连接，请先断开再导入账单。')
                    self.reply(200,billing.ledger.import_report(body.get('report'),body.get('key_id'),body.get('confirmed_all_keys')))
                    return
                if self.path == '/api/backend-access':
                    if not self.public_request():raise ValueError('请从公网管理中心打开服务器后台。')
                    self.backend_cookie='whale_backend='+self.headers.get('X-Panel-Session','')+'; Path=/manage/; Max-Age=43200; HttpOnly; Secure; SameSite=Strict'
                    self.reply(200,{'ok':True})
                    return
                if self.path == '/api/usage/connect':
                    self.reply(200,billing.connect(body.get('token'),self.headers.get('X-Panel-Session','')))
                    return
                if self.path == '/api/usage/select':
                    self.reply(200,billing.select(body.get('connection_id'),body.get('key_id'),self.headers.get('X-Panel-Session',''),body.get('confirmed')))
                    return
                if self.path == '/api/usage/disconnect':
                    self.reply(200,billing.disconnect())
                    return
                if self.path == '/api/check':
                    threading.Thread(target=monitor.probe, daemon=True).start()
                elif self.path == '/api/phone-check':
                    self.reply(200,monitor.check_phone())
                    return
                elif self.path == '/api/phone-connect':
                    self.reply(200,monitor.reconnect_phone(body.get('ip'),body.get('port')))
                    return
                elif self.path == '/api/test':
                    self.reply(200, monitor.new_challenge())
                    return
                elif self.path == '/api/refresh-qr':
                    monitor.refresh_qr()
                elif self.path == '/api/recover':
                    monitor.recover()
                else:
                    self.reply(404, {'error': '未知入口'})
                    return
                self.reply(200, {'ok': True})
            except Exception as exc:
                self.reply(409, {'error': str(exc) if isinstance(exc, ValueError) else '操作未完成，请检查状态；不会自动重试登录'})
    return Handler


def main():
    monitor = Monitor()
    token_path = DATA / 'access-token'
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(32), encoding='ascii')
    token_path.chmod(0o600)
    token = token_path.read_text().strip()
    assert len(token) >= 32
    threading.Thread(target=monitor.run, daemon=True).start()
    threading.Thread(target=PublicPresence(ROOT,Archive(ROOT)).run, daemon=True).start()
    public_origin = os.environ.get('QQ_RECOVERY_PUBLIC_ORIGIN', '')
    if public_origin and (urlsplit(public_origin).scheme != 'https' or public_origin != 'https://' + urlsplit(public_origin).netloc):
        raise ValueError('QQ_RECOVERY_PUBLIC_ORIGIN must be an HTTPS origin')
    public_token = ''
    if public_origin:
        public_token = Path(os.environ['QQ_RECOVERY_PUBLIC_PROXY_KEY_FILE']).read_text().strip()
        if len(public_token) < 32:raise ValueError('Invalid public proxy credential')
    billing_service=PlatformBilling(ROOT)
    threading.Thread(target=billing_service.run,daemon=True).start()
    ThreadingHTTPServer(('127.0.0.1', 6190), make_handler(monitor, token, public_origin, public_token,billing_service)).serve_forever()


if __name__ == '__main__':
    main()
