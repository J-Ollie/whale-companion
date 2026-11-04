"""Finite compatibility mapping to typed Android executor operations. Never executes shell on phone."""
from .executor import ExecutorError


class NativeTransport:
    def __init__(self,executor):
        self.executor=executor;self.xml={}

    async def rpc(self,op,args=None):return await self.executor.rpc(op,args or {})

    async def shell(self,args):
        a=tuple(str(x) for x in args);state=self.executor.status
        if a==('getprop','ro.product.model'):return str(state.get('model','V2046A'))
        if a==('getprop','ro.build.version.release'):return str(state.get('android','13'))
        if a==('dumpsys','power'):return 'mWakefulness='+('Awake' if state.get('screen_on') else 'Asleep')
        if a==('dumpsys','window','policy'):
            return 'showing=true secure=true' if state.get('locked') else 'showing=false secure=false'
        if a in (('dumpsys','window','displays'),('dumpsys','activity','activities')):
            await self.rpc('state')
            package=state.get('foreground','')
            return ('mCurrentFocus=Window{a u0 '+package+'/.MainActivity}\n'
                    'mResumedActivity: ActivityRecord{a u0 '+package+'/.MainActivity}') if package else ''
        if a[:2]==('uiautomator','dump') and len(a)==3 and a[2].startswith('/data/local/tmp/companion'):
            result=await self.rpc('xml',{'package':state.get('foreground','')})
            self.xml[a[2]]=result['xml'];return 'UI hierarchy dumped'
        if a[:1]==('cat',) and len(a)==2 and a[1] in self.xml:return self.xml[a[1]]
        if a[:2]==('rm','-f') and len(a)==3 and a[2].startswith('/data/local/tmp/companion'):
            self.xml.pop(a[2],None);return ''
        if a[:2]==('input','tap') and len(a)==4:
            await self.rpc('tap',{'x':int(a[2]),'y':int(a[3]),'package':state.get('foreground','')});return ''
        if a[:2]==('input','swipe') and len(a)==7:
            await self.rpc('swipe',dict(zip(('x1','y1','x2','y2','duration'),map(int,a[2:])),package=state.get('foreground','')));return ''
        if a[:2]==('input','keyevent') and len(a)==3:
            await self.rpc('key',{'key':int(a[2]),'package':state.get('foreground','')});return ''
        if a[:4]==('cmd','package','resolve-activity','--brief') and len(a)==5:
            result=await self.rpc('resolve',{'package':a[4]});return result.get('component','')
        if a[:4]==('am','start','-W','-n') and len(a)==5:
            await self.rpc('open_app',{'package':a[4].split('/')[0]});return 'Status: ok'
        raise ExecutorError('operation_requires_legacy_adb')
