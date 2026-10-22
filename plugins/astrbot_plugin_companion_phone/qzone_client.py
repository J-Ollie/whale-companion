"""Restricted Qzone client; protocol adapted from Eganchiyu/qzone-sdk (MIT).

Only own-account list/upload/public-publish. No delete/comment/like API.
Credentials remain in process memory and never enter model requests.
"""
import base64
import json
import re
import urllib.parse
import urllib.request

BOT_QQ='100000002'
BASE='https://user.qzone.qq.com/proxy/domain/'
PUBLISH=BASE+'taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6'
LIST=BASE+'taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6'
UPLOAD='https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image'


class QzoneError(Exception): pass


def parse(raw):
    text=raw.decode('utf-8',errors='replace').strip()
    # fs responses wrap JSON in an HTML/JavaScript function with additional braces.
    # Decode only JSON objects, never evaluate script or use greedy outer braces.
    decoder=json.JSONDecoder()
    for start in (m.start() for m in re.finditer(r'\{',text)):
        try: result,_=decoder.raw_decode(text,start)
        except ValueError: continue
        if isinstance(result,dict) and ('code' in result or 'ret' in result): return result
    raise QzoneError('invalid_json')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): raise QzoneError('unexpected_redirect')


class QzoneClient:
    def __init__(self,cookies,qq):
        if str(qq)!=BOT_QQ: raise QzoneError('wrong_account')
        fields=dict(re.findall(r'(?:^|;\s*)([^=;\s]+)=([^;]*)',cookies))
        key=fields.get('p_skey','')
        if not key: raise QzoneError('missing_space_cookie')
        # If a cookie declares an account, require the same small account.
        for name in ('uin','p_uin'):
            if name in fields and fields[name].lstrip('o0')!=BOT_QQ: raise QzoneError('cookie_account_mismatch')
        value=5381
        for char in key: value+=(value<<5)+ord(char)
        self.gtk=value&0x7fffffff
        self.cookies=cookies
        self.skey=fields.get('skey','')
        self.pskey=key
        self.opener=urllib.request.build_opener(NoRedirect())

    def request(self,url,data,post=False):
        # URLs are internal constants only; never accept a URL from the model.
        headers={'Cookie':self.cookies,'User-Agent':'Mozilla/5.0',
                 'Referer':'https://user.qzone.qq.com/'+BOT_QQ,'Origin':'https://user.qzone.qq.com'}
        encoded=urllib.parse.urlencode(data).encode('utf-8')
        if post: headers['Content-Type']='application/x-www-form-urlencoded'
        else: url+='?'+encoded.decode('ascii')
        req=urllib.request.Request(url,data=encoded if post else None,headers=headers)
        with self.opener.open(req,timeout=20) as response: raw=response.read(4*1024*1024+1)
        if len(raw)>4*1024*1024: raise QzoneError('oversize_response')
        result=parse(raw)
        code=result.get('code',result.get('ret',-1))
        if code!=0: raise QzoneError('server_code_'+str(code)[:12])
        return result

    def recent(self):
        result=self.request(LIST,{'uin':BOT_QQ,'ftype':0,'sort':0,'pos':0,'num':10,'replynum':0,
            'g_tk':self.gtk,'code_version':1,'format':'jsonp','callback':'_preloadCallback','not_trunc_con':1})
        return [{'tid':str(r.get('tid','')),'text':self.post_text(r),
                 'visible':r.get('ugc_right',r.get('right')),'at':r.get('created_time',0),
                 'image_count':r.get('pictotal',len(r.get('pic') or []))}
                for r in (result.get('msglist') or []) if isinstance(r,dict)]

    def discussion_posts(self, offset=0, count=10):
        # Read-only own-account discussions, never arbitrary model URLs/accounts.
        if type(offset) is not int or not 0<=offset<=1000 or type(count) is not int or not 1<=count<=20:
            raise QzoneError('invalid_page')
        result=self.request(LIST,{'uin':BOT_QQ,'ftype':0,'sort':0,'pos':offset,'num':count,'replynum':100,
            'g_tk':self.gtk,'code_version':1,'format':'jsonp','callback':'_preloadCallback','not_trunc_con':1})
        rows=[r for r in result.get('msglist',[]) if isinstance(r,dict)]
        return rows, result.get('total')

    @staticmethod
    def post_text(row):
        # Qzone's content field strips newlines. Its typed conlist preserves
        # actual text and turns URLs into separate segments. Never drop unknown
        # segment types or guess the text of an unsupported/truncated response.
        parts=row.get('conlist')
        if isinstance(parts,list) and parts and not row.get('has_more_con'):
            values=[]
            for part in parts:
                if not isinstance(part,dict):break
                if part.get('type')==2 and isinstance(part.get('con'),str):values.append(part['con'])
                elif part.get('type')==1 and isinstance(part.get('url'),str):values.append(part['url'])
                else:break
            else:return ''.join(values)
        return row.get('content','')

    def upload(self,path):
        result=self.request(UPLOAD+'?g_tk='+str(self.gtk),{
            'filename':'image.jpg','uin':BOT_QQ,'skey':self.skey,'zzpaneluin':BOT_QQ,'zzpanelkey':'',
            'p_uin':BOT_QQ,'p_skey':self.pskey,'qzonetoken':'','uploadtype':1,'albumtype':7,'exttype':0,
            'refer':'shuoshuo','output_type':'jsonhtml','charset':'utf-8','output_charset':'utf-8',
            'upload_hd':1,'hd_width':2048,'hd_height':2048,'hd_quality':90,'base64':1,
            'jsonhtml_callback':'callback','picfile':base64.b64encode(path.read_bytes()).decode('ascii'),
            'qzreferrer':'https://user.qzone.qq.com/'+BOT_QQ},post=True)
        value=result.get('data',{})
        if not value.get('lloc'): raise QzoneError('image_upload_unconfirmed')
        bo=value.get('bo','')
        if not bo:
            match=re.search(r'&bo=([^&]+)',value.get('url',''))
            bo=match.group(1) if match else ''
        return value['lloc'],bo

    def publish(self,text,image=None):
        data={'syn_tweet_verson':1,'paramstr':1,'pic_template':'','richtype':'','richval':'',
            'special_url':'','subrichtype':'','pic_bo':'','who':1,'con':text,'feedversion':1,'ver':1,
            'ugc_right':1,'to_sign':0,'hostuin':BOT_QQ,'code_version':1,'format':'fs',
            'qzreferrer':'https://user.qzone.qq.com/'+BOT_QQ}
        if image:
            lloc,bo=self.upload(image)
            data.update(richtype=1,subrichtype=1,richval=','.join(['',lloc,lloc,lloc,'','0','0','','0','0']),pic_bo=bo)
        result=self.request(PUBLISH+'?g_tk='+str(self.gtk),data,post=True)
        tid=str(result.get('t1_tid') or result.get('tid',''))
        if not tid: raise QzoneError('missing_publication_id')
        return tid
