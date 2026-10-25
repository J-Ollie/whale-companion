"""Bounded ADB transport probe, independent of character sleep and screen state."""
import json
import math
import re
import threading
import time
from pathlib import Path

# Fixed commands only. One connection attempt to the already paired serial;
# never disconnect, discover ports, wake, screenshot or operate an application.
PROBE = '''import os,subprocess,json,sys
env=os.environ.copy()
env['HOME']='/AstrBot/data/phone-runtime/home'
env['ADB_VENDOR_KEYS']='/AstrBot/data/phone-runtime/home/.android/adbkey'
adb='/AstrBot/data/phone-runtime/platform-tools/adb'
base=[adb,'-s',sys.argv[1]]
try:
 r=subprocess.run(base+['get-state'],capture_output=True,timeout=4,env=env)
 if (r.returncode or r.stdout.strip()!=b'device') and b'unauthorized' not in r.stderr.lower() and r.stdout.strip()!=b'unauthorized':
  subprocess.run([adb,'connect',sys.argv[1]],capture_output=True,timeout=8,env=env)
  r=subprocess.run(base+['get-state'],capture_output=True,timeout=4,env=env)
 state=r.stdout.decode(errors='replace').strip()
 err=r.stderr.lower()
 if b'unauthorized' in err or state=='unauthorized':status='unauthorized'
 elif r.returncode or state!='device':status='disconnected'
 else:
  r=subprocess.run(base+['shell','echo','whale-link-ok'],capture_output=True,timeout=4,env=env)
  status='connected' if r.returncode==0 and r.stdout.strip()==b'whale-link-ok' else 'unresponsive'
except subprocess.TimeoutExpired:status='timeout'
except Exception:status='unavailable'
print(json.dumps({'status':status}))
'''

LABELS={'connected':'无线调试已连通','disconnected':'手机调试暂未连通',
        'unauthorized':'调试连接未授权','unresponsive':'调试连接无响应',
        'timeout':'手机连接检测超时','unavailable':'暂时无法检测','unconfigured':'尚未配置手机连接'}

CONNECT_CLIENT='''import socket,json,sys
try:
 with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
  s.settimeout(33);s.connect('/AstrBot/data/plugin_data/companion_phone/connection-control.sock')
  s.sendall((json.dumps({'action':'connect','serial':sys.argv[1]})+'\\n').encode())
  raw=b''
  while b'\\n' not in raw and len(raw)<2048:
   part=s.recv(2048-len(raw))
   if not part:break
   raw+=part
  value=json.loads(raw)
  print(json.dumps({'ok':value.get('ok') is True,'code':value.get('code')}))
except Exception:print(json.dumps({'ok':False,'code':'unavailable'}))
'''


def endpoint(ip,port):
    if ip!='100.64.0.10':raise ValueError('请使用这台已配对备用机的远程连接 IP。')
    if type(port) not in (str,int) or not re.fullmatch(r'[0-9]{1,5}',str(port)) or not 1<=int(port)<=65535:
        raise ValueError('连接端口应为 1–65535，请填写无线调试的连接端口，而非配对端口。')
    return ip+':'+str(int(port))


def read_object(path):
    if path.stat().st_size>32768:raise ValueError()
    value=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict):raise ValueError()
    return value


def rhythm_status(root):
    result={'mode':'unknown','label':'作息状态暂不可用'}
    try:
        value=read_object(Path(root)/'data/astrbot/plugin_data/companion_phone/rhythm.json')
        now=time.time();at=value.get('updated_at');until=value.get('until')
        if type(at) not in (int,float) or not math.isfinite(at) or not 0<=now-at<600:return result
        mode=value.get('mode')
        if mode in ('asleep','resting') and (type(until) not in (int,float) or not math.isfinite(until) or until<=now):return result
        label={'asleep':'小鲸鱼正在睡觉','resting':'小鲸鱼正在休息',
               'awake':'小鲸鱼清醒中','browsing':'小鲸鱼正在玩手机'}.get(mode)
        if label:return {'mode':mode,'label':label}
    except (OSError,ValueError,TypeError):pass
    return result


def project(value):
    """Same fixed fields for visitors, no private host/port, errors or rhythm data."""
    if not isinstance(value,dict):value={}
    status=value.get('status')
    if status not in LABELS:status='unavailable'
    at=value.get('checked_at')
    rhythm=value.get('rhythm',{})
    mode=rhythm.get('mode') if isinstance(rhythm,dict) else 'unknown'
    labels={'asleep':'小鲸鱼正在睡觉','resting':'小鲸鱼正在休息','awake':'小鲸鱼清醒中',
            'browsing':'小鲸鱼正在玩手机','unknown':'作息状态暂不可用'}
    if mode not in labels:mode='unknown'
    return {'status':status,'label':LABELS[status],
            'checked_at':at if type(at) in (int,float) and math.isfinite(at) else None,
            'rhythm':{'mode':mode,'label':labels[mode]}}


class PhoneHealth:
    def __init__(self,root,runner,container):
        self.root=Path(root);self.runner=runner;self.container=container
        self.lock=threading.Lock();self.snapshot={};self.last_attempt=0;self.last_reconnect=0

    def settings(self):
        try:
            serial=read_object(self.root/'data/astrbot/plugin_data/companion_phone/state.json')['serial']
            ip,port=serial.rsplit(':',1);endpoint(ip,port)
            return {'ip':ip,'port':int(port)}
        except (OSError,ValueError,KeyError,TypeError):return {'ip':'100.64.0.10','port':None}

    def reconnect(self,ip,port):
        serial=endpoint(ip,port)
        if not self.lock.acquire(blocking=False):raise ValueError('手机连接检测正在进行，请稍后再试。')
        try:
            if time.time()-self.last_reconnect<30:raise ValueError('请等待30秒再尝试重新连接。')
            self.last_reconnect=time.time()
            try:
                raw=self.runner(['docker','exec',self.container,'python','-c',CONNECT_CLIENT,serial],timeout=36)
                if len(raw)>2048:raise ValueError()
                reply=json.loads(raw)
            except Exception:raise ValueError('暂时无法连接手机控制服务，原设置保留。') from None
            if reply.get('ok') is not True or reply.get('code')!='connected':
                messages={'busy':'小鲸鱼正在操作手机，请等当前操作结束再连接。',
                          'timeout':'连接验证超时，原设置保留。',
                          'connection_failed':'连接验证未通过，请核对网络、无线调试连接端口和配对状态；原设置保留。'}
                raise ValueError(messages.get(reply.get('code'),'手机连接服务暂不可用，原设置保留。'))
            self.snapshot={'status':'connected','checked_at':time.time()};self.last_attempt=time.time()
            return project({**self.snapshot,'rhythm':rhythm_status(self.root)})
        finally:self.lock.release()

    def query(self):
        if not self.lock.acquire(blocking=False):return project({**self.snapshot,'rhythm':rhythm_status(self.root)})
        try:
            if self.snapshot and time.time()-self.last_attempt<15:
                return project({**self.snapshot,'rhythm':rhythm_status(self.root)})
            self.last_attempt=time.time();status='unconfigured'
            try:
                path=self.root/'data/astrbot/plugin_data/companion_phone/state.json'
                serial=read_object(path).get('serial','')
                match=re.fullmatch(r'100\.64\.0\.10:([0-9]{1,5})',serial)
                if not match or not 1<=int(match[1])<=65535:raise ValueError()
                raw=self.runner(['docker','exec',self.container,'python','-c',PROBE,serial],timeout=22)
                if len(raw)>1024:raise ValueError()
                status=json.loads(raw).get('status')
                if status not in LABELS:status='unavailable'
            except FileNotFoundError:status='unconfigured'
            except Exception:status='unavailable'
            self.snapshot={'status':status,'checked_at':time.time()}
            return project({**self.snapshot,'rhythm':rhythm_status(self.root)})
        finally:self.lock.release()
