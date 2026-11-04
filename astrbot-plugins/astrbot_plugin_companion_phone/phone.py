"""Small bounded ADB client. No arbitrary command, install, file, or macro API."""
import asyncio
import io
import json
import os
import re
import shlex
import time
import uuid
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path

from PIL import Image
from .ui import parse_ui, visible_nodes, consuming_target
from .executor import ExecutorError

PHONE_IP = '100.64.0.10'
TOOL_NAMES = ['companion_phone_status', 'companion_phone_screenshot',
              'companion_phone_tap', 'companion_phone_swipe', 'companion_phone_key',
              'companion_phone_open_app', 'companion_phone_type_ascii',
              'companion_phone_autonomous_browse','companion_chat_media','companion_site_visitors']


class PhoneError(Exception):
    pass


class PhoneOffline(PhoneError):
    pass


class PhonePortChanged(PhoneError):
    pass


class Phone:
    def __init__(self, root):
        self.root=Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.state_path=self.root/'state.json'
        self.state={'serial':PHONE_IP+':38605', 'paused':False}
        if self.state_path.exists():
            self.state.update(json.loads(self.state_path.read_text(encoding='utf-8')))
        self.validate_serial(self.state['serial'])
        self.lock=asyncio.Lock()
        self.screen=None
        self.ui=None
        self.actions=deque()
        self.last_port_discovery=None
        self._save()

    @staticmethod
    def validate_serial(serial):
        match=re.fullmatch(re.escape(PHONE_IP)+r':([0-9]{1,5})',serial)
        if not match or not 1 <= int(match[1]) <= 65535:
            raise PhoneError('只能连接已配对备用机的私有地址；端口应为 1–65535。')

    def _save(self):
        temp=self.state_path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.state,ensure_ascii=False),encoding='utf-8')
        temp.chmod(0o600)
        temp.replace(self.state_path)

    def pause(self, value):
        self.state['paused']=bool(value)
        self.screen=None
        self._save()

    def ready(self):
        if self.state['paused']:
            raise PhoneError('手机控制已暂停；请管理员发送 /恢复手机。')
        executor=getattr(self,'executor',None)
        if executor and (executor.status.get('paused') is True or executor.paired and not executor.online()):
            raise PhoneError('手机端已暂停、被使用者接管或连接中断；请在手机端恢复，不自动切换通道重做操作。')

    def native_available(self):
        executor=getattr(self,'executor',None)
        return bool(executor and executor.online() and not getattr(self,'legacy_only',False))

    async def _run(self, args, timeout=16):
        env=os.environ.copy()
        env['HOME']='/AstrBot/data/phone-runtime/home'
        env['ADB_VENDOR_KEYS']='/AstrBot/data/phone-runtime/home/.android/adbkey'
        proc=await asyncio.create_subprocess_exec(
            '/AstrBot/data/phone-runtime/platform-tools/adb', *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        try:
            out,err=await asyncio.wait_for(proc.communicate(),timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()
            if isinstance(exc,asyncio.CancelledError):
                raise
            raise PhoneError('手机连接超时。请检查 Tailscale、无线调试开关和连接端口；不要重复执行不确定成功的点按。') from exc
        if proc.returncode:
            # Avoid returning arbitrary stderr or device screen content as diagnostic commands.
            if b'device offline' in err.lower() or b'not found' in err.lower() or b'no devices/emulators' in err.lower():
                raise PhoneOffline('手机调试连接已离线。若刚熄屏，请检查 Tailscale 后台运行及网络；端口变化时发送 /连接手机 新端口。')
            raise PhoneError('ADB 未成功执行。手机可能离线或端口改变，请发送 /手机状态 检查。')
        return out

    async def _shell(self, *args):
        executor=getattr(self,'executor',None)
        maintenance=tuple(args[:1]) in (('getprop',),('echo',))
        if executor and (executor.status.get('paused') is True or executor.paired and not executor.online()) and not maintenance:
            raise PhoneError('手机端暂停或失联，未通过调试通道继续读屏或操作。')
        if self.native_available():
            try:return await self.native.shell(args)
            except ExecutorError as exc:raise PhoneError('手机端执行未完成：'+str(exc)+'；未切换通道重试。') from None
        return (await self._run(['-s', self.state['serial'], 'shell', shlex.join(str(a) for a in args)])).decode('utf-8',errors='replace').strip()

    async def _connect(self):
        if self.native_available():return
        try:
            try:
                await self._connect_once()
            except PhoneOffline:
                # Rebuild only this stale transport, never replay an input action.
                self.screen=None
                await self._run(['disconnect',self.state['serial']],timeout=6)
                await self._connect_once()
        except PhonePortChanged:
            await self._recover_port()

    async def _connect_once(self):
        out=(await self._run(['connect', self.state['serial']],timeout=12)).decode(errors='replace')
        if 'connected to ' not in out:
            if 'refused' in out.lower():
                raise PhonePortChanged('手机调试端口拒绝连接，可能已改变。')
            raise PhoneError('未能连接备用机。请保持手机联网和无线调试开启；端口变化时使用 /连接手机 IP:端口。')
        state=(await self._run(['-s',self.state['serial'],'get-state'],timeout=8)).decode().strip()
        if state!='device':
            raise PhoneOffline('手机调试连接离线，未处于可操作的 device 状态。')

    async def _discover_ports(self):
        # Android wireless ADB uses a dynamic listening port. Search only this
        # owner's fixed private phone, with bounded time/concurrency; no WAN scan.
        queue=asyncio.Queue()
        for port in range(32768,65536): queue.put_nowait(port)
        found=[]
        async def worker():
            while not queue.empty():
                try: port=queue.get_nowait()
                except asyncio.QueueEmpty: return
                writer=None
                try:
                    _,writer=await asyncio.wait_for(asyncio.open_connection(PHONE_IP,port),0.65)
                    found.append(port)
                except (OSError,asyncio.TimeoutError): pass
                finally:
                    if writer is not None: writer.close()
                    queue.task_done()
        tasks=[asyncio.create_task(worker()) for _ in range(128)]
        try:
            await asyncio.wait_for(asyncio.gather(*tasks),25)
        except asyncio.TimeoutError:
            pass
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
        return sorted(set(found))[:4]

    async def _recover_port(self):
        now=time.monotonic()
        if self.last_port_discovery is not None and now-self.last_port_discovery<900:
            raise PhoneError('调试端口暂不可用，已限制重复探测。可查看手机无线调试新端口并发送 /连接手机 新端口。')
        self.last_port_discovery=now
        old=self.state['serial']
        self.screen=None
        for port in await self._discover_ports():
            candidate=PHONE_IP+':'+str(port)
            if candidate==old: continue
            self.state['serial']=candidate
            try:
                await self._connect_once()
                # Use the existing cryptographic pairing; accept only this model.
                if await self._shell('getprop','ro.product.model')!='V2046A':
                    raise PhoneError('Recovered transport is not the authorized phone')
            except PhoneError:
                self.state['serial']=old
                continue
            except BaseException:
                self.state['serial']=old
                raise
            self.state['last_port_recovery']=time.time()
            self._save()
            return
        self.state['serial']=old
        raise PhoneError('未找到已配对备用机的新调试端口；请确认无线调试开启，必要时发送 /连接手机 新端口。')

    async def connect_to(self, serial):
        self.validate_serial(serial)
        async with self.lock:
            old=self.state['serial']
            self.state['serial']=serial
            try:
                await self._connect()
            except BaseException:
                self.state['serial']=old
                raise
            self.screen=None
            self._save()
        return '备用机已连接，端口已保存。'

    async def status(self):
        async with self.lock:
            executor=getattr(self,'executor',None)
            if executor and executor.paired:
                return ('手机端执行器'+('在线' if executor.online() else '暂不可用或已暂停')+'；'
                    +('安全锁屏，不能操作' if executor.status.get('locked') else '不绕过安全锁屏')+'；无线调试是独立通道。')
            # Status remains available while paused and never resumes actions.
            await self._connect()
            model=await self._shell('getprop','ro.product.model')
            android=await self._shell('getprop','ro.build.version.release')
            power=await self._screen_power()
            label={'Awake':'亮屏','Asleep':'熄屏','Dozing':'息屏待机','Dreaming':'屏保'}.get(power,'电源状态未知')
            return f'备用机在线；型号 {model}，Android {android}；{label}；控制'+('已暂停' if self.state['paused'] else '已启用')+'。'

    async def _screen_power(self):
        report=await self._shell('dumpsys','power')
        match=re.search(r'\bmWakefulness=(Awake|Asleep|Dozing|Dreaming)\b',report)
        return match[1] if match else 'Unknown'

    async def _wake_for_capture(self):
        power=await self._screen_power()
        if power not in ('Asleep','Dozing'):
            return
        self._reserve_action()
        self.screen=None
        await self._shell('input','keyevent',224)
        await asyncio.sleep(0.8)
        if await self._screen_power() in ('Asleep','Dozing'):
            raise PhoneError('手机仍处于熄屏状态，未取得可用画面。唤醒未确认成功；这不是模型截图授权被取消。')
        policy=await self._shell('dumpsys','window','policy')
        showing=bool(re.search(r'\bshowing=true\b',policy))
        secure=bool(re.search(r'\bsecure=true\b',policy))
        if showing and secure:
            raise PhoneError('手机已唤醒，但停在安全锁屏，需要你在手机上解锁后才能继续；没有取消截图授权。')

    async def foreground_app(self):
        # Android 13 / OriginOS puts current focus under displays, not windows.
        # A secondary empty display is normal; conflicting active foci are not.
        report=await self._shell('dumpsys','window','displays')
        packages=set(re.findall(r'mCurrentFocus=Window\{[^\n}]*\bu\d+\s+([A-Za-z0-9_.]+)/',report))
        if len(packages)!=1:
            raise PhoneError('无法确认当前前台应用，停止自主观察，不截取未知软件画面。')
        return packages.pop()

    async def _assert_foreground(self,allowed_packages):
        if allowed_packages is not None and await self.foreground_app() not in allowed_packages:
            self.screen=None
            raise PhoneError('前台已离开获准软件，停止自主观察和操作。')

    async def screenshot(self,allowed_packages=None):
        async with self.lock:
            self.ready()
            self.screen=None
            self.ui=None
            await self._connect()
            # Invoked only inside an owner-authorized screenshot task. No periodic wakeups.
            await self._wake_for_capture()
            await self._assert_foreground(allowed_packages)
            native=self.native_available()
            if native:
                import base64
                try:frame=await self.native.rpc('screenshot',{'package':await self.foreground_app()})
                except ExecutorError as exc:raise PhoneError('手机端截图未完成：'+str(exc)) from None
                png=base64.b64decode(frame['image'],validate=True)
            else:png=await self._run(['-s',self.state['serial'],'exec-out','screencap','-p'])
            await self._assert_foreground(allowed_packages)
            if len(png)>16*1024*1024 or not (png.startswith(b'\x89PNG\r\n\x1a\n') or native and png.startswith(b'\xff\xd8')):
                raise PhoneError('未取得有效屏幕图片，不应凭猜测操作。')
            with Image.open(io.BytesIO(png)) as img:
                img.load()
                w,h=img.size
                if native:w,h=int(frame['width']),int(frame['height'])
                self.screen=(w,h,time.monotonic())
                img=img.convert('RGB')
                img.thumbnail((900,1800))
                output=io.BytesIO()
                img.save(output,format='JPEG',quality=85)
                return output.getvalue(), (w,h), img.size

    async def _read_ui(self,package):
        path='/data/local/tmp/companion-ui-'+uuid.uuid4().hex+'.xml'
        try:
            await self._shell('uiautomator','dump',path)
            xml=await self._shell('cat',path)
            if self.screen is None:
                raise PhoneError('画面已失效，需要重新观察。')
            return parse_ui(xml,package,*self.screen[:2])
        finally:
            try: await self._shell('rm','-f',path)
            except PhoneError: pass

    async def ui_observation(self,allowed_packages=None):
        """Optional node assistance for a fresh screenshot, restricted to its foreground app."""
        async with self.lock:
            self.ready()
            self.ui=None
            self._point(0,0)
            package=await self.foreground_app()
            allowed=frozenset([package]) if allowed_packages is None else allowed_packages
            await self._assert_foreground(allowed)
            try:
                parsed=await self._read_ui(package)
            except (PhoneError,ValueError,ET.ParseError):
                await self._assert_foreground(frozenset([package]))
                return {'available':False,'nodes':[]}
            await self._assert_foreground(frozenset([package]))
            self._point(0,0)
            if not parsed['nodes']:
                return {'available':False,'nodes':[]}
            shown,targets=visible_nodes(parsed,uuid.uuid4().hex[:10])
            self.ui={'package':package,'screen':self.screen,'fingerprint':parsed['fingerprint'],'targets':targets}
            return {'available':True,'nodes':shown,'fingerprint':parsed['fingerprint']}

    def node_target(self,node_id):
        self.ready()
        self._point(0,0)
        if (not isinstance(node_id,str) or not self.ui or self.screen!=self.ui['screen']
                or node_id not in self.ui['targets']):
            raise PhoneError('控件编号不属于当前观察，未执行。请重新截图读取控件。')
        node=self.ui['targets'][node_id]
        if not node['enabled'] or not (node['clickable'] or node['editable']):
            raise PhoneError('该控件不可点击或已停用，未执行。')
        return dict(node['_attrs'])

    async def tap_node(self,node_id,allowed_packages=None):
        async with self.lock:
            if consuming_target(self.node_target(node_id)):
                raise PhoneError('付款、消费、订阅等控件不在授权范围，未点击。')
            current=self.ui
            allowed=frozenset([current['package']])
            if allowed_packages is not None and current['package'] not in allowed_packages:
                raise PhoneError('该控件所在应用不在本次授权范围，未执行。')
            await self._assert_foreground(allowed)
            try:
                fresh=await self._read_ui(current['package'])
            except (PhoneError,ValueError,ET.ParseError) as exc:
                self.screen=None;self.ui=None
                raise PhoneError('无法重新核对目标控件，未执行；请重新截图。') from exc
            await self._assert_foreground(allowed)
            self._point(0,0)
            if fresh['fingerprint']!=current['fingerprint']:
                self.screen=None;self.ui=None
                raise PhoneError('界面控件已变化，未点击旧目标；请重新截图读取控件。')
            point=current['targets'][node_id]['_point']
            self._reserve_action()
            self.screen=None;self.ui=None
            await self._shell('input','tap',*point)
            return '已执行一次核对后的控件点按；请重新观察页面，不能据此声称任务已完成。'

    @staticmethod
    def coord(value):
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not float(value).is_integer() or not 0<=value<=1000:
            raise PhoneError('屏幕坐标必须是 0–1000 的整数。')
        return int(value)

    def _point(self,x,y):
        x,y=self.coord(x),self.coord(y)
        if self.screen is None or time.monotonic()-self.screen[2]>30:
            raise PhoneError('先获取最新手机截图，再根据图中位置操作。每次点按或滑动后需重新截图。')
        w,h,_=self.screen
        return round(x*(w-1)/1000),round(y*(h-1)/1000)

    def _reserve_action(self):
        self.ready()
        now=time.monotonic()
        while self.actions and now-self.actions[0]>=60:
            self.actions.popleft()
        if len(self.actions)>=20:
            raise PhoneError('手机操作达到每分钟 20 次上限，请稍后再试。')
        if self.actions and now-self.actions[-1]<0.7:
            raise PhoneError('请稍等再操作手机。')
        self.actions.append(now)

    async def tap(self,x,y,allowed_packages=None):
        async with self.lock:
            self.ready()
            await self._assert_foreground(allowed_packages)
            point=self._point(x,y)
            self._reserve_action()
            self.screen=None
            await self._shell('input','tap',*point)
            return '已执行一次点按；是否达到目标请重新截图确认。'

    async def long_press(self,x,y,allowed_packages=None):
        async with self.lock:
            self.ready(); await self._assert_foreground(allowed_packages)
            point=self._point(x,y); self._reserve_action(); self.screen=None
            await self._shell('input','swipe',*point,*point,900)
            return '已长按一次，请重新观察菜单。'

    async def target_info(self,x,y):
        point=self._point(x,y)
        path='/data/local/tmp/companion-target.xml'
        try:
            await self._shell('uiautomator','dump',path)
            tree=ET.fromstring(await self._shell('cat',path))
            candidates=[]
            for node in tree.iter('node'):
                bounds=[int(n) for n in re.findall(r'\d+',node.get('bounds',''))]
                if len(bounds)==4 and bounds[0]<=point[0]<=bounds[2] and bounds[1]<=point[1]<=bounds[3]:
                    if node.get('clickable')=='true' or node.get('content-desc') or node.get('text'):
                        candidates.append(((bounds[2]-bounds[0])*(bounds[3]-bounds[1]),node.attrib))
            return min(candidates,key=lambda r:r[0])[1] if candidates else {}
        except ET.ParseError: return {}
        finally: await self._shell('rm','-f',path)

    async def swipe(self,x1,y1,x2,y2,duration_ms,allowed_packages=None,repeat=1):
        async with self.lock:
            self.ready()
            if type(repeat) is not int or not 1<=repeat<=5:
                raise PhoneError('连续滑动次数必须为1至5的整数。')
            if repeat>1 and allowed_packages is None:
                allowed_packages=frozenset([await self.foreground_app()])
            await self._assert_foreground(allowed_packages)
            start,end=self._point(x1,y1),self._point(x2,y2)
            if isinstance(duration_ms,bool) or not isinstance(duration_ms,(int,float)) or not float(duration_ms).is_integer() or not 100<=duration_ms<=2000:
                raise PhoneError('滑动时长必须为 100–2000 毫秒的整数。')
            self.screen=None
            for index in range(repeat):
                if index:await asyncio.sleep(.8)
                self.ready()
                await self._assert_foreground(allowed_packages)
                self._reserve_action()
                await self._shell('input','swipe',*start,*end,int(duration_ms))
            return f'已执行{repeat}次连续滑动，中间未截图；接下来观察最终页面，不能声称读过跳过的内容。'

    async def key(self,key):
        keys={'HOME':3,'BACK':4,'RECENTS':187,'WAKEUP':224,'ENTER':66}
        if key not in keys:
            raise PhoneError('仅支持 HOME、BACK、RECENTS、WAKEUP、ENTER。不能绕过锁屏。')
        async with self.lock:
            self.ready()
            await self._connect()
            self._reserve_action()
            self.screen=None
            await self._shell('input','keyevent',keys[key])
            return '已发送按键；请截图确认结果。WAKEUP 只能唤醒屏幕，不能解锁。'

    async def open_app(self,package):
        if not isinstance(package,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+',package) or len(package)>180:
            raise PhoneError('应用包名无效。只能提供准确包名，不能输入命令或网址。')
        async with self.lock:
            self.legacy_only=package=='com.tencent.mobileqq'
            self.ready()
            await self._connect()
            component=await self._shell('cmd','package','resolve-activity','--brief',package)
            component=component.splitlines()[-1].strip() if component else ''
            if not re.fullmatch(r'[A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+',component):
                raise PhoneError('未找到该应用的可启动页面；不要编造已打开。')
            self._reserve_action()
            self.screen=None
            result=await self._shell('am','start','-W','-n',component)
            if 'Error:' in result or 'Exception' in result or 'Status: ok' not in result:
                raise PhoneError('应用启动未确认成功，请截图检查。')
            return '应用启动返回成功；请截图确认当前页面。'

    async def _input_field(self):
        path='/data/local/tmp/companion-input-field.xml'
        try:
            await self._shell('uiautomator','dump',path)
            xml=await self._shell('cat',path)
            root=ET.fromstring(xml)
            fields=[n.attrib for n in root.iter('node') if n.get('focused')=='true' and 'EditText' in n.get('class','')]
            if len(fields)!=1 or fields[0].get('password')=='true':
                raise PhoneError('需要已聚焦的普通输入框；不向密码框输入。')
            return fields[0]
        except ET.ParseError:
            raise PhoneError('无法确认输入框，未输入。') from None
        finally:
            await self._shell('rm','-f',path)

    async def type_ascii(self,text,allowed_packages=None):
        # Keep the historical tool name for saved persona compatibility; now Unicode.
        if not isinstance(text,str) or not text or len(text)>200 or any(ord(c)<32 for c in text):
            raise PhoneError('请输入1–200字符的单行文字，不接受控制字符。')
        async with self.lock:
            self.ready()
            await self._assert_foreground(allowed_packages)
            self._point(0,0)  # Require a fresh screen and an observed focused field.
            self._reserve_action()
            self.screen=None
            field=await self._input_field()
            if self.native_available():
                try:await self.native.rpc('input',{'text':text,'package':await self.foreground_app()})
                except ExecutorError as exc:raise PhoneError('手机端输入未完成：'+str(exc)+'；不自动重试。') from None
                after=await self._input_field()
                if text not in after.get('text',''):raise PhoneError('手机端文字回读未确认；不自动重复输入。')
                return '文字已输入并回读；未自动发送。'
            bounds=[int(n) for n in re.findall(r'\d+',field.get('bounds',''))]
            if len(bounds)!=4: raise PhoneError('无法定位当前输入框。')
            original=(await self._shell('settings','get','secure','default_input_method')).strip()
            ime='com.android.adbkeyboard/.AdbIME'
            if not re.fullmatch(r'[A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+',original):
                raise PhoneError('无法确认原输入法，已停止，未输入。')
            try:
                await self._shell('ime','set',ime)
                actual=(await self._shell('settings','get','secure','default_input_method')).strip()
                if actual!=ime: raise PhoneError('中文输入组件未就绪，未输入。')
                import base64
                payload=base64.b64encode(text.encode('utf-8')).decode('ascii')
                # v2.4 registers its receiver only when its input view is shown.
                # Re-focus this same observed field after switching IME.
                await self._shell('input','tap',(bounds[0]+bounds[2])//2,(bounds[1]+bounds[3])//2)
                await asyncio.sleep(0.8)
                await self._shell('am','broadcast','-a','ADB_INPUT_B64','-p','com.android.adbkeyboard','--es','msg',payload)
                await asyncio.sleep(0.3)
                after=await self._input_field()
                if text not in after.get('text',''):
                    raise PhoneError('文字输入未通过输入框回读，不能声称成功；不要重复发送，请截图检查。')
            finally:
                if original!=ime:
                    await asyncio.shield(self._shell('ime','set',original))
            return '文字已在输入框回读验证，并已恢复原输入法。没有按发送键；可截图确认。'
