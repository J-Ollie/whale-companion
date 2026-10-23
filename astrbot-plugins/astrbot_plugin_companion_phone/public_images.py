"""Public image search; HTTPS downloads pin and validate public addresses."""
import html
import io
import json
import re
import http.client
import ipaddress
import socket
import ssl
import urllib.parse
import urllib.request
from PIL import Image

UA='Mozilla/5.0'
MEDIA_HOSTS={'upload.wikimedia.org','images.pexels.com','images.unsplash.com'}


def search_commons(query):
    if not isinstance(query,str) or not query.strip(): return []
    params={'action':'query','format':'json','generator':'search','gsrsearch':query[:80],
            'gsrnamespace':6,'gsrlimit':6,'prop':'imageinfo','iiprop':'url|extmetadata','iiurlwidth':900}
    request=urllib.request.Request('https://commons.wikimedia.org/w/api.php?'+urllib.parse.urlencode(params),headers={'User-Agent':UA})
    with urllib.request.urlopen(request,timeout=12) as response: raw=response.read(1000000)
    data=json.loads(raw)
    result=[]
    for page in data.get('query',{}).get('pages',{}).values():
        info=(page.get('imageinfo') or [{}])[0]
        meta=info.get('extmetadata',{})
        license=meta.get('LicenseShortName',{}).get('value','')
        if not re.fullmatch(r'CC0|Public domain|CC BY(?:-SA)? [1-4]\.0',license): continue
        url=info.get('thumburl') or info.get('url','')
        if urllib.parse.urlparse(url).hostname!='upload.wikimedia.org': continue
        source=info.get('descriptionurl','')
        if not source.startswith('https://commons.wikimedia.org/wiki/'): continue
        artist=html.unescape(re.sub('<[^>]*>','',meta.get('Artist',{}).get('value','')))[:100]
        result.append({'id':len(result),'title':page.get('title','')[:160],
                       'url':url,'source':source,'license':license,'artist':artist})
    return result[:4]


def search(query):
    if not isinstance(query,str) or not query.strip(): return []
    params=urllib.parse.urlencode({'q':query[:80],'first':1,'count':12})
    request=urllib.request.Request('https://cn.bing.com/images/search?'+params,headers={'User-Agent':UA})
    candidates=[]
    try:
        with urllib.request.urlopen(request,timeout=10) as response: raw=response.read(1500000).decode('utf-8',errors='replace')
        for match in re.findall(r'\bm="([^"]+)"',raw):
            try: entry=json.loads(html.unescape(match))
            except ValueError: continue
            url=entry.get('murl',''); source=entry.get('purl','')
            media=urllib.parse.urlparse(url); page=urllib.parse.urlparse(source)
            if media.scheme!='https' or not media.hostname or media.username or media.port not in (None,443): continue
            if page.scheme!='https' or not page.hostname or page.username or page.port not in (None,443): continue
            try:
                if not ipaddress.ip_address(media.hostname).is_global: continue
            except ValueError: pass
            if any(r['url']==url for r in candidates): continue
            candidates.append({'id':len(candidates),'title':html.unescape(entry.get('t',''))[:160],
                'url':url,'source':source,'license':'来源页标注为准','artist':'原作者见来源页'})
        if candidates: return candidates[:4]
    except Exception: pass
    # Alternative index may be unavailable on some domestic networks.
    try: return search_commons(query)
    except Exception: return []


class FixedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        parsed=urllib.parse.urlparse(newurl)
        if parsed.scheme!='https' or parsed.hostname not in MEDIA_HOSTS: raise ValueError('Image redirect rejected')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


class PublicHTTPS(http.client.HTTPSConnection):
    """Pin DNS to validated public IPs while retaining original TLS hostname."""
    def connect(self):
        addresses=socket.getaddrinfo(self.host,443,type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError('Non-public image host')
        error=None
        for family,kind,proto,_,address in addresses[:4]:
            connection=socket.socket(family,kind,proto); connection.settimeout(12)
            try:
                connection.connect(address)
                self.sock=self._context.wrap_socket(connection,server_hostname=self.host)
                return
            except OSError as exc:
                connection.close(); error=exc
        raise error or ValueError('Image unavailable')


def public_bytes(url):
    for _ in range(4):
        parsed=urllib.parse.urlparse(url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.port not in (None,443):
            raise ValueError('Image URL rejected')
        connection=PublicHTTPS(parsed.hostname,timeout=12,context=ssl.create_default_context())
        try:
            target=urllib.parse.urlunparse(('', '',parsed.path or '/',parsed.params,parsed.query,''))
            connection.request('GET',target,headers={'User-Agent':UA,'Accept':'image/*'})
            response=connection.getresponse()
            if response.status in (301,302,303,307,308):
                url=urllib.parse.urljoin(url,response.getheader('Location',''))
                continue
            if response.status!=200 or not response.getheader('Content-Type','').lower().startswith('image/'):
                raise ValueError('Image response rejected')
            raw=response.read(6*1024*1024+1)
            if len(raw)>6*1024*1024: raise ValueError('Image too large')
            return raw
        finally: connection.close()
    raise ValueError('Too many image redirects')


def download(candidate,path):
    raw=public_bytes(candidate['url'])
    if len(raw)>6*1024*1024: raise ValueError('Image too large')
    with Image.open(io.BytesIO(raw)) as picture:
        if picture.width*picture.height>20000000: raise ValueError('Image dimensions too large')
        picture.thumbnail((1280,1280))
        picture.convert('RGB').save(path,format='JPEG',quality=88)
    path.chmod(0o600)
    # New JPEG carries no EXIF/location data.
    return path
