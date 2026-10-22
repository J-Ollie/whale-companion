"""Bounded image library, scoped Android album access, no arbitrary file API."""
import asyncio
import hashlib
import io
import json
import re
import time
from pathlib import Path
from PIL import Image,ImageSequence
from .phone import PhoneError
from . import public_images


class MediaLibrary:
    def __init__(self,root,phone,config):
        self.root=Path(root)/'media'; self.root.mkdir(exist_ok=True); self.root.chmod(0o700)
        self.phone,self.config=phone,config
        self.path=self.root/'index.json'
        self.index={'assets':{},'candidates':{}}
        self.lock=asyncio.Lock()
        if self.path.exists():
            try: self.index.update(json.loads(self.path.read_text(encoding='utf-8')))
            except (ValueError,OSError): raise PhoneError('图片库索引损坏，保留文件并暂停图片操作。')

    def save(self):
        tmp=self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.index,ensure_ascii=False),encoding='utf-8'); tmp.chmod(0o600); tmp.replace(self.path)

    def shareable(self,row,public=False):
        if row.get('origin') in ('web','generated'): return True
        if row.get('origin')=='public_screen': return True
        if row.get('origin')=='private_screen': return not public
        if row.get('origin')=='album':
            scope=self.config.get('album_share_scope','disabled')
            return scope=='all' or (scope=='shared_album' and row.get('bucket')==self.config.get('share_album_name','小鲸鱼可分享'))
        return False

    def describe(self,row):
        return {k:row.get(k) for k in ('id','label','origin','animated','at')}|{'shareable':self.shareable(row)}

    def recent(self,limit=12,shareable=True,public=False):
        values=sorted(self.index['assets'].values(),key=lambda r:r['at'],reverse=True)
        return [self.describe(r) for r in values if not shareable or self.shareable(r,public)][:limit]

    def ingest(self,raw,*,origin,label='',source='',bucket=''):
        if not raw or len(raw)>8*1024*1024: raise PhoneError('图片为空或超过8MB。')
        with Image.open(io.BytesIO(raw)) as picture:
            if picture.width*picture.height>20000000: raise PhoneError('图片尺寸过大。')
            count=getattr(picture,'n_frames',1)
            animated=count>1 and count<=80 and picture.width*picture.height*count<=50000000
            output=io.BytesIO()
            if animated:
                frames=[]; durations=[]
                for frame in ImageSequence.Iterator(picture):
                    durations.append(max(40,min(2000,int(frame.info.get('duration',120)))))
                    frame=frame.convert('RGBA'); frame.thumbnail((512,512)); frames.append(frame.copy())
                frames[0].save(output,format='GIF',save_all=True,append_images=frames[1:],duration=durations,loop=0,disposal=2)
                ext='gif'
            else:
                picture.seek(0); frame=picture.convert('RGB'); frame.thumbnail((1600,1600))
                frame.save(output,format='JPEG',quality=90); ext='jpg'
        data=output.getvalue()
        if len(data)>8*1024*1024: raise PhoneError('重编码后的图片仍超过8MB。')
        identifier='media:'+hashlib.sha256(data).hexdigest()[:24]
        if identifier in self.index['assets']: return self.index['assets'][identifier]
        used=sum(p.stat().st_size for p in self.root.glob('*') if p.is_file())
        if used+len(data)>1024**3 or len(self.index['assets'])>=2000: raise PhoneError('图片库已达1GB或2000张上限；不会自动删除原图。')
        filename=identifier[6:]+'.'+ext
        path=self.root/filename; path.write_bytes(data); path.chmod(0o600)
        row={'id':identifier,'filename':filename,'origin':origin,'label':' '.join(str(label).split())[:100],
             'source':source[:1200],'bucket':bucket,'animated':animated,'at':time.time()}
        self.index['assets'][identifier]=row; self.save(); return row

    def asset(self,identifier,share=False,public=False):
        if not isinstance(identifier,str) or not re.fullmatch(r'media:[a-f0-9]{24}',identifier): raise PhoneError('图片编号无效；不能传入路径或网址。')
        row=self.index['assets'].get(identifier)
        if not row or (share and not self.shareable(row,public)): raise PhoneError('没有找到已获分享授权的图片。')
        path=self.root/row['filename']
        if path.resolve().parent!=self.root.resolve() or not path.is_file(): raise PhoneError('图片文件不可用。')
        return row,path

    def preview_bytes(self,identifier):
        _,path=self.asset(identifier)
        with Image.open(path) as picture:
            picture.seek(0); image=picture.convert('RGB'); image.thumbnail((800,1000))
            out=io.BytesIO(); image.save(out,format='JPEG',quality=85)
        return out.getvalue()

    def post_image(self,identifier,target):
        row,path=self.asset(identifier,share=True,public=True)
        with Image.open(path) as picture:
            picture.seek(0); frame=picture.convert('RGB'); frame.thumbnail((1280,1280)); frame.save(target,format='JPEG',quality=90)
        target.chmod(0o600)
        return row

    async def search(self,query):
        candidates=await asyncio.to_thread(public_images.search,query)
        result=[]
        for row in candidates:
            identifier='web:'+hashlib.sha256(row['url'].encode()).hexdigest()[:20]
            self.index['candidates'][identifier]={**row,'kind':'web','expires':time.time()+900}
            result.append({'id':identifier,'title':row['title'],'source':row['source']})
        self._prune(); self.save(); return result

    def _prune(self):
        self.index['candidates']={k:r for k,r in self.index['candidates'].items() if r.get('expires',0)>time.time()}
        self.index['candidates']=dict(list(self.index['candidates'].items())[-60:])

    async def album(self,offset=0):
        if isinstance(offset,bool) or not isinstance(offset,int) or offset<0:
            raise PhoneError('相册分页位置必须为非负整数。')
        scope=self.config.get('album_share_scope','disabled')
        if scope not in ('all','shared_album'): raise PhoneError('相册分享范围尚未确定，暂不读取照片列表。')
        self.phone.ready(); await self.phone._connect()
        args=['content','query','--uri','content://media/external/images/media',
              '--projection','_id:bucket_display_name:_size:date_added','--sort','date_added DESC']
        if scope=='shared_album':
            name=self.config.get('share_album_name','小鲸鱼可分享')
            if name!='小鲸鱼可分享': raise PhoneError('分享相册名称未通过配置检查。')
            args+=['--where',"bucket_display_name='小鲸鱼可分享'"]
        text=await self.phone._shell(*args)
        if re.search(r'Permission Denial|SecurityException|permission denied',text,re.I):
            raise PhoneError('手机系统拒绝读取相册，不能把权限问题说成没有照片。')
        result=[]
        total=0
        for line in text.splitlines():
            match=re.search(r'_id=(\d+), bucket_display_name=(.*?), _size=(\d+), date_added=(\d+)',line)
            if not match: continue
            number,bucket,size,at=match.groups()
            if not 0<int(size)<=8*1024*1024: continue
            if scope=='shared_album' and bucket!='小鲸鱼可分享': continue
            total+=1
            if total<=offset or len(result)>=12: continue
            identifier='album:'+number
            row={'kind':'album','number':number,'bucket':bucket,'size':int(size),'expires':time.time()+900}
            self.index['candidates'][identifier]=row
            result.append({'id':identifier,'album':bucket,'bytes':int(size),'added_at':int(at)})
        self.album_total=total
        self._prune(); self.save(); return result

    async def materialize(self,identifier):
        if isinstance(identifier,str) and identifier.startswith('media:'): return self.asset(identifier)[0]
        row=self.index['candidates'].get(identifier)
        if not row or row.get('expires',0)<time.time(): raise PhoneError('图片候选已过期，请重新搜索或列出相册。')
        if row['kind']=='web':
            raw=await asyncio.to_thread(public_images.public_bytes,row['url'])
            return self.ingest(raw,origin='web',label=row.get('title',''),source=row['source'])
        scope=self.config.get('album_share_scope','disabled')
        if scope!='all' and not (scope=='shared_album' and row['bucket']=='小鲸鱼可分享'): raise PhoneError('照片不在当前分享范围。')
        self.phone.ready(); await self.phone._connect()
        raw=await self.phone._run(['-s',self.phone.state['serial'],'exec-out','content','read','--uri',
                                  'content://media/external/images/media/'+row['number']])
        return self.ingest(raw,origin='album',label=row['bucket']+'相册照片',bucket=row['bucket'])

    async def save_seen(self,rect,label='',public=False):
        from .access import enabled_apps,PUBLIC_APPS
        if not isinstance(rect,(list,tuple)) or len(rect)!=4: raise PhoneError('需要图片区域的四个坐标。')
        x1,y1,x2,y2=[self.phone.coord(v) for v in rect]
        if x2-x1<30 or y2-y1<30 or (x2-x1)*(y2-y1)>900000: raise PhoneError('请只框选图片区域，不保存整个手机界面。')
        self.phone._point(x1,y1)  # Need a fresh observation before choosing a crop.
        apps=enabled_apps(self.config)
        jpg,_,_=await self.phone.screenshot(allowed_packages=frozenset(apps))
        package=await self.phone.foreground_app()
        is_public=bool(public and apps.get(package) in PUBLIC_APPS)
        with Image.open(io.BytesIO(jpg)) as frame:
            w,h=frame.size; image=frame.crop((round(w*x1/1000),round(h*y1/1000),round(w*x2/1000),round(h*y2/1000)))
            out=io.BytesIO(); image.save(out,format='JPEG',quality=92)
        row=self.ingest(out.getvalue(),origin='public_screen' if is_public else 'private_screen',label=label or '浏览图片的屏幕裁剪图')
        await self.save_to_phone(row['id'])
        return row

    async def save_to_phone(self,identifier):
        row,path=self.asset(identifier)
        folder='/sdcard/Pictures/小鲸鱼收藏'
        target=folder+'/'+row['filename']
        self.phone.ready(); await self.phone._connect()
        await self.phone._shell('mkdir','-p',folder)
        await self.phone._run(['-s',self.phone.state['serial'],'push',str(path),target])
        await self.phone._shell('am','broadcast','-a','android.intent.action.MEDIA_SCANNER_SCAN_FILE','-d','file://'+target)
        return target
