let key=location.hash.slice(1);history.replaceState(null,'',location.pathname);if(key)sessionStorage.setItem('recoveryKey',key);else key=sessionStorage.getItem('recoveryKey')||'';
const launchHelp=document.getElementById('launchHelp');if(key&&location.protocol==='http:')launchHelp.hidden=true;
const $=id=>document.getElementById(id);let previousLevel=null,allowNotify=false,busy=false,panelSession='',unlocked=false,setupRequired=false,accessRole='admin',guestAvailable=false,publicAccess=false;
async function api(path,method='GET',body={}){const r=await fetch('/api/'+path,{method,headers:{'X-Recovery-Key':key,'X-Panel-Session':panelSession,...(method==='POST'?{'Content-Type':'application/json'}:{})},...(method==='POST'?{body:JSON.stringify(body)}:{})});if(!r.ok){let e;try{e=await r.json()}catch{}if(r.status===401)showLogin(false);throw Error(e?.error||'云端请求失败')}return r.json()}
function showLogin(first){
  unlocked=false;panelSession='';setupRequired=first;applyAccessRole('admin');
  $('guestLogin').hidden=first||!guestAvailable;
  $('protectedPanel').hidden=true;$('authPanel').hidden=false;
  $('authTitle').textContent=first?'设置管理密码':'管理密码登录';
  $('authHint').textContent=first?'首次使用，请设置8–128个字符的独立密码。以后打开管理中心需输入此密码。':'请输入你设置的管理密码，验证后才能查看聊天记录、日记或进行恢复操作。';
  $('confirmRow').hidden=!first;$('confirmPassword').required=first;
  $('panelPassword').setAttribute('autocomplete',first?'new-password':'off');
  $('authSubmit').textContent=first?'设置密码并进入':'登录';
  $('panelPassword').value='';$('confirmPassword').value='';
  resetBalance();resetVisitorView();resetMemoryView();++archiveRequest;$('archiveAdmin').hidden=true;$('archiveEntries').replaceChildren();archiveData=null;++chatRequest;++diaryRequest;diaryRows=[];clearMedia();$('chatMessages').replaceChildren();$('diaryEntries').replaceChildren();
  $('refreshChats').disabled=false;$('loadDiary').disabled=false;
  $('olderChats').hidden=true;$('olderDiary').hidden=true;
}
async function prepareLogin(){
  if(location.protocol!=='https:'&&(!key||location.protocol!=='http:')){launchHelp.hidden=false;return}
  try{const d=await api('auth/info');publicAccess=d.public_access===true&&location.protocol==='https:';launchHelp.hidden=publicAccess||Boolean(key);guestAvailable=d.guest_available===true;showLogin(d.setup_required)}catch(e){$('error').textContent=e.message}
}

function stamp(t){return t?new Date(t*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'尚未检查'}
// Display-only metrics; status continues to come from the authenticated API.
function renderDetails(d){
  $('backendAccessHelp').hidden=accessRole==='guest'||!publicAccess;$('backendAccessHelp').textContent='服务器后台将在新标签页打开，仍需各自的登录验证。';
  const gateway=d.backend==='snowluma'?'SnowLuma':'NapCat';
  const metrics=[
    ['AstrBot',d.containers?.['qq-companion-astrbot']?.running,'运行中','未知 / 停止','http://127.0.0.1:16185/','AstrBot 管理后台（新标签页）'],
    [gateway,d.containers?.[d.gateway_container||'qq-companion-napcat']?.running,'运行中','未知 / 停止',d.backend==='snowluma'?'http://127.0.0.1:15099/':'http://127.0.0.1:16099/webui',gateway+' 管理后台（新标签页）'],
    ['QQ 登录',d.qq?.isLogin,'后台显示登录','未确认登录',d.backend==='snowluma'?'http://127.0.0.1:16081/vnc.html':'http://127.0.0.1:16099/webui','QQ 登录管理（新标签页）']
  ];
  $('details').replaceChildren(...metrics.map(([label,ok,yes,no,url,linkLabel])=>{
    const item=document.createElement('div');item.className='metric';item.dataset.state=ok?'ok':'warning';
    const info=line('div','','metricInfo');
    info.append(line('span',label,'metricLabel'),line('span',ok?yes:no,'metricValue'));
    item.append(info);
    if(url&&accessRole!=='guest'){
      const jump=line('a','打开','metricJump');
      if(publicAccess)url=label==='AstrBot'?'/manage/astrbot/':label==='QQ 登录'?'/manage/qq/vnc.html?autoconnect=1&resize=scale&path=manage%2Fqq%2Fwebsockify':'/manage/snowluma/';
      jump.href=url;jump.target='_blank';jump.rel='noopener noreferrer';
      jump.setAttribute('aria-label',linkLabel);jump.title=linkLabel;
      if(publicAccess)jump.onclick=async event=>{
        event.preventDefault();if(!unlocked||accessRole!=='admin')return;const session=panelSession;
        const popup=window.open('about:blank','_blank');if(popup)popup.opener=null;
        try{await api('backend-access','POST');if(unlocked&&session===panelSession&&accessRole==='admin'){if(popup)popup.location.replace(new URL(url,location.origin).href);else location.assign(new URL(url,location.origin).href)}else if(popup)popup.close()}
        catch(e){if(popup)popup.close();$('error').textContent=e.message}
      };
      jump.append(line('span','↗','metricArrow'));item.append(jump);
    }
    return item;
  }));
}

function statusText(text){return text==='后台声称在线、连接存在；不代表实际消息一定可达'?'服务连接已建立，实际消息收发情况有待验证。':text||''}
function applyAccessRole(role){
  accessRole=role==='guest'?'guest':'admin';document.body.dataset.access=accessRole;
  const guest=accessRole==='guest',selected=$('book').value;$('checkPhone').hidden=guest;$('showPhoneReconnect').hidden=guest;$('phoneReconnectForm').hidden=true;$('showPhoneReconnect').setAttribute('aria-expanded','false');$('phoneConnectIp').value='';$('phoneConnectPort').value='';$('phoneConnectResult').textContent='';previousPhoneStatus=null;
  resetVisitorView();$('visitorGuest').hidden=!guest;$('visitorAdmin').hidden=guest;$('tabVisitors').querySelector('.navNumber').textContent=guest?'04':'05';
  if(guest){try{$('visitorNickname').value=localStorage.getItem('whaleVisitorNickname')||''}catch{}}
  $('tabDiary').querySelector('.navNumber').textContent=guest?'02':'03';
  $('tabArchive').querySelector('.navNumber').textContent=guest?'03':'04';
  $('archivePanel').querySelector('.eyebrow').textContent=(guest?'03':'04')+' / WHALE ARCHIVE';
  resetVisitorView();resetMemoryView();++archiveRequest;$('archiveAdmin').hidden=true;for(const id of ['archiveEntries','archiveStats','archiveApps','archiveTendencies','archiveNotice'])$(id).replaceChildren();archiveData=null;
  $('diaryPanel').querySelector('.pageHeading .eyebrow').textContent=(guest?'02':'03')+' / DAILY MOMENTS';
  document.querySelector('.overviewHint').textContent=guest?'状态由系统定期更新，消息收发情况请通过测试文字核验。':'云端每分钟检查 · 本页打开时可提醒，关闭后不向外部渠道通知。在线状态不代表实际收发正常。';
  $('archiveMemoryNav').hidden=guest;$('tabChats').hidden=guest;$('recover').hidden=guest;$('notify').hidden=guest;$('recoveryHelp').hidden=guest;$('guestSession').hidden=!guest;
  const publicOption=line('option','日记');publicOption.value='public';
  const options=[publicOption];if(!guest){const privateOption=line('option','私密笔记');privateOption.value='private';options.push(privateOption);}
  $('book').replaceChildren(...options);$('book').value=!guest&&selected==='private'?'private':'public';
  updateDiaryPrivacy();
}
let previousPhoneStatus=null;
function renderPhone(d){
  const p=d.phone||{},rest=['asleep','resting'].includes(p.rhythm?.mode);
  $('phoneStatus').textContent=p.label||'尚未检测';$('phoneStatus').dataset.state=p.status||'unknown';
  $('phoneRhythm').textContent=(p.rhythm?.label||'作息状态暂不可用')+' · '+(rest?'暂不即时回复；连接检测不会打扰休息。':'连接检测不点亮屏幕，也不操作手机应用。');
  $('phoneCheckTime').textContent=p.checked_at?'检测于 '+stamp(p.checked_at)+' · 每分钟自动检查':'等待首次检测';
  if(p.status==='unauthorized')$('phoneRhythm').textContent+=' 请在备用机上确认调试授权。';
  else if(['disconnected','unresponsive','timeout'].includes(p.status))$('phoneRhythm').textContent+=' 请检查备用机网络和无线调试，端口变化时重新连接。';
  $('testHelp').textContent='请用主 QQ 将测试文字发给小鲸鱼（100000002），在此确认是否收到。'+(rest?'当前处于休息时段，只核验消息是否入站，醒后再确认回复。':'');
  if(allowNotify&&accessRole==='admin'&&previousPhoneStatus==='connected'&&p.status&&p.status!=='connected'&&Notification.permission==='granted')new Notification('备用机连接异常',{body:p.label||'无线调试连接需要检查'});
  previousPhoneStatus=p.status||null;
}
$('showPhoneReconnect').onclick=async()=>{
  if(!unlocked||accessRole!=='admin')return;const session=panelSession;
  $('phoneReconnectForm').hidden=false;$('showPhoneReconnect').setAttribute('aria-expanded','true');
  $('phoneConnectResult').textContent='正在读取连接设置…';
  try{const d=await api('phone-settings');if(!unlocked||session!==panelSession||accessRole!=='admin')return;$('phoneConnectIp').value=d.ip||'';$('phoneConnectPort').value=d.port||'';$('phoneConnectResult').textContent='';$('phoneConnectPort').focus()}catch(e){$('phoneConnectResult').textContent=e.message}
};
$('cancelPhoneConnect').onclick=()=>{$('phoneReconnectForm').hidden=true;$('showPhoneReconnect').setAttribute('aria-expanded','false')};
$('phoneReconnectForm').onsubmit=async event=>{
  event.preventDefault();if(!unlocked||accessRole!=='admin')return;const session=panelSession;
  $('phoneConnectSubmit').disabled=true;$('phoneConnectResult').textContent='正在连接并验证备用机，不会点亮屏幕…';
  try{const d=await api('phone-connect','POST',{ip:$('phoneConnectIp').value.trim(),port:$('phoneConnectPort').value});if(unlocked&&session===panelSession&&accessRole==='admin'){renderPhone({phone:d});$('phoneConnectResult').textContent='备用机已连通，新连接设置已保存。'}}catch(e){if(session===panelSession)$('phoneConnectResult').textContent=e.message}
  finally{$('phoneConnectSubmit').disabled=false}
};
$('checkPhone').onclick=async()=>{
  if(!unlocked||accessRole!=='admin')return;const session=panelSession;
  $('checkPhone').disabled=true;$('checkPhone').textContent='检测中…';
  try{const p=await api('phone-check','POST');if(unlocked&&accessRole==='admin'&&session===panelSession)renderPhone({phone:p})}catch(e){$('error').textContent=e.message}
  finally{$('checkPhone').disabled=false;$('checkPhone').textContent='检测手机'}
};
async function status(){if(!unlocked)return;if(!publicAccess&&(!key||location.protocol!=='http:')){ $('badge').textContent='需要从桌面启动';$('summary').textContent='请双击桌面的“小鲸鱼管理中心”';for(const id of ['check','test','recover'])$(id).disabled=true;return;}if(busy)return;busy=true;try{const d=await api('status');$('summary').textContent=statusText(d.summary);$('badge').textContent=({error:'需要处理',warning:'待检查',unverified:'收发待核验',received:'入站已验证',unknown:'检查中'})[d.level]||'检查中';$('badge').dataset.level=d.level;$('time').textContent='最近检查：'+stamp(d.checked_at)+'（北京时间）';renderDetails(d);renderPhone(d);$('operation').textContent=d.operation?.message||'';$('recover').disabled=d.operation?.state==='running'||d.recovery_wait>0;$('cooldown').textContent=d.recovery_wait>0?`恢复按钮冷却剩余 ${Math.ceil(d.recovery_wait/60)} 分钟，防止反复重登`:'';
if(d.challenge){$('challenge').hidden=false;$('testText').textContent=d.challenge.text;$('testResult').textContent=`QQ 接入：${d.challenge.napcat_received?'已收到':'尚未收到'}；聊天服务：${d.challenge.astrbot_received?'已收到':'尚未收到'}。`+(d.challenge.result==='waiting'?'请在两分钟内发送，正在检查':d.challenge.result==='received'?(['asleep','resting'].includes(d.phone?.rhythm?.mode)?'入站链路已验证；当前正在休息，醒后再确认回复':'入站链路已验证，请在 QQ 确认回复'):(['asleep','resting'].includes(d.phone?.rhythm?.mode)&&d.challenge.napcat_received?'QQ 已收到测试文字；当前处于休息时段，尚不能以未即时回复判断断线':'测试窗口已结束，若你确实发送了，则本轮未到达'));}
const eventsScroll=$('events').scrollTop;$('events').replaceChildren(...(d.events||[]).slice().reverse().map(e=>{const li=document.createElement('li');li.textContent=stamp(e.at)+' · '+statusText(e.summary||e.message||({qr_refreshed:'已刷新登录二维码',recovery_requested:'手动恢复接入',monitor_error:'状态检查异常'})[e.kind]||e.kind);return li}));$('events').scrollTop=eventsScroll;if(allowNotify&&previousLevel!==null&&d.level!==previousLevel&&['error','warning'].includes(d.level)&&Notification.permission==='granted')new Notification('小鲸鱼连接异常',{body:d.summary});previousLevel=d.level;document.title=(d.level==='error'?'⚠ ':'')+'小鲸鱼管理中心';$('error').textContent='';}catch(e){$('error').textContent=e.message+(publicAccess?'；请稍后刷新重试':'；请确认本地 SSH 隧道已连接')}finally{busy=false}}
async function action(name){try{await api(name,'POST');await status()}catch(e){$('error').textContent=e.message}}
$('check').onclick=()=>action('check');$('test').onclick=()=>action('test');$('recover').onclick=()=>action('recover');$('copy').onclick=async()=>{try{await navigator.clipboard.writeText($('testText').textContent);$('copy').textContent='已复制'}catch{$('error').textContent='请手动选中并复制测试文字'}};$('notify').onclick=async()=>{if(!('Notification'in window)){ $('error').textContent='当前浏览器不支持通知';return;}allowNotify=await Notification.requestPermission()==='granted';$('notify').textContent=allowNotify?'已开启本页提醒':'未授权通知';};setInterval(()=>{if(unlocked)status()},5000);

let chatBefore=0,diaryBefore=0,chatRequest=0,diaryRequest=0,diaryRows=[];
const mediaUrls=new Set();
function clearMedia(){for(const url of mediaUrls)URL.revokeObjectURL(url);mediaUrls.clear()}
function line(tag,text,cls=''){const el=document.createElement(tag);el.textContent=text;el.className=cls;return el}
async function showPanel(name){
  if(accessRole==='guest'&&name==='Chats')name='Connection';
  for(const [tab,panel] of [['Connection','connectionPanel'],['Chats','chatsPanel'],['Diary','diaryPanel'],['Archive','archivePanel'],['Visitors','visitorsPanel']]){
    const selected=tab===name;$('tab'+tab).setAttribute('aria-selected',String(selected));$('tab'+tab).classList.toggle('secondary',!selected);$(panel).hidden=!selected;$('tab'+tab).tabIndex=selected?0:-1;
  }
  $('error').textContent='';
  if(name==='Visitors'&&accessRole==='admin')await loadVisitors();
  if(name==='Chats')await loadChats();
  if(name==='Archive')await loadArchive();
  if(name==='Diary'){updateDiaryPrivacy();if($('book').value==='public')await loadDiary();}
}
async function cachedImage(name,target,request){
  try{
    if(!/^[a-f0-9]{64}\.(png|jpg|gif|webp)$/.test(name))return;
    const r=await fetch('/api/chat-media/'+name,{headers:{'X-Recovery-Key':key,'X-Panel-Session':panelSession}});
    if(!r.ok)throw Error();const b=await r.blob();if(request!==chatRequest)return;
    const url=URL.createObjectURL(b);mediaUrls.add(url);
    const img=document.createElement('img');img.src=url;img.alt='聊天图片或表情包';img.loading='lazy';img.className='chatImage';target.replaceChildren(img);
  }catch{target.textContent='[图片未缓存或无法读取]'}
}
async function loadChats(older=false){
  if(accessRole==='guest')return;
  const request=++chatRequest,qq=$('contact').value;
  if(!older){chatBefore=0;clearMedia();$('chatMessages').replaceChildren()}
  $('refreshChats').disabled=true;
  try{
    const params=new URLSearchParams({qq,before:String(older?chatBefore:0)});
    const d=await api('chats?'+params);if(request!==chatRequest)return;
    const options=[line('option','请选择好友')];options[0].value='';
    for(const c of d.contacts){const o=line('option',(c.name||'好友')+' · '+c.qq+'（'+c.count+'条）');o.value=c.qq;options.push(o)}
    $('contact').replaceChildren(...options);$('contact').value=qq;
    $('chatNotice').textContent=d.notice;
    const entries=[];
    for(const row of d.messages){
      const card=document.createElement('article');card.className='chatRow '+(row.direction==='out'?'outgoing':'incoming');
      card.append(line('p',(row.direction==='out'?'小鲸鱼':'好友')+' · '+stamp(row.at),'entryMeta'));
      for(const part of row.segments){
        if(part.type==='text')card.append(line('div',part.text,'messageText'));
        else if(part.type==='image'){
          const target=line('div','['+(part.label||'图片')+']');card.append(target);
          if(part.media)cachedImage(part.media,target,request);
        }else card.append(line('div','['+(({face:'QQ表情',record:'语音',video:'视频',file:'文件',reply:'引用',forward:'转发',mface:'表情包'})[part.type]||'非文字消息')+(part.label?' '+part.label:'')+']','hint'));
      }
      entries.push(card);
    }
    if(older)$('chatMessages').prepend(...entries);else $('chatMessages').replaceChildren(...(entries.length?entries:[line('p',qq?'暂无保存的消息':d.contacts.length?'选择好友后查看聊天内容':'暂未记录到好友私聊。启用后的消息会出现在这里。','hint')]));
    chatBefore=d.next_before||0;$('olderChats').hidden=!d.has_more;$('error').textContent='';
  }catch(e){$('error').textContent=e.message}finally{if(request===chatRequest)$('refreshChats').disabled=false}
}
function updateDiaryPrivacy(){
  const privateBook=accessRole!=='guest'&&$('book').value==='private';
  $('diaryPanel').dataset.book=privateBook?'private':'public';
  $('diaryPrivateBadge').hidden=!privateBook;
  $('diaryPrivacyNote').textContent=privateBook?'私密内容单独保存，不会混入「日记」；点击“查看 / 刷新”后读取。':'以前的日记与随手感想已按日期归入日记；同一天的内容放在一起，查看不会触发发布。';
}
function renderDiaryRows(book){
  const groups=new Map();
  for(const row of diaryRows){const day=row.day||stamp(row.at).split(' ')[0];if(!groups.has(day))groups.set(day,[]);groups.get(day).push(row);}
  const cards=[];
  for(const [day,rows] of groups){
    const card=document.createElement('article');card.className='diaryEntry diaryDay';
    const header=line('div','','diaryDayHeader');header.append(line('h3',day+' · '+(book==='private'?'私密笔记':'日记')));
    if(rows.length>1)header.append(line('span',rows.length+' 则记录','diaryDayCount'));
    card.append(header);
    for(const row of rows.slice().sort((a,b)=>(a.at||0)-(b.at||0))){
      const part=line('div','','diaryPart');
      const time=row.at?new Date(row.at*1000).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'时间未记录';
      part.append(line('p',(row.kind==='no_entry'?'未写日记的说明 · ':'')+time+
        (row.retrospective?(row.kind==='no_entry'?' · 后来补录，不是当时想法':' · 事后补写，并非当晚原稿'):'')+(row.updated_at?' · 更新于 '+stamp(row.updated_at):''),'entryMeta'));
      part.append(line('p',row.text,'diaryText'));card.append(part);
    }
    cards.push(card);
  }
  $('diaryEntries').replaceChildren(...(cards.length?cards:[line('p',book==='private'?'这里留给不想公开的感受与念头，暂时还没有笔记。':'当天日记会在晚间回顾，平淡的一天也可以留几句话。','hint')]));
}
async function loadDiary(older=false){
  const request=++diaryRequest,book=accessRole==='guest'?'public':$('book').value;
  const scroll=$('diaryEntries').scrollTop;
  if(!older){diaryBefore=0;diaryRows=[];$('diaryEntries').replaceChildren()}
  updateDiaryPrivacy();$('loadDiary').disabled=true;
  try{
    const d=await api('diaries?'+new URLSearchParams({book,before:String(older?diaryBefore:0)}));
    if(request!==diaryRequest||book!==$('book').value)return;
    $('diarySchedule').textContent=d.schedule;
    diaryRows=older?diaryRows.concat(d.entries):d.entries;
    renderDiaryRows(book);
    $('diaryEntries').scrollTop=older?scroll:0;
    diaryBefore=d.next_before;$('olderDiary').hidden=!d.has_more;$('error').textContent='';
  }catch(e){$('error').textContent=e.message}finally{if(request===diaryRequest)$('loadDiary').disabled=false}
}
$('tabConnection').onclick=()=>showPanel('Connection');$('tabChats').onclick=()=>showPanel('Chats');$('tabDiary').onclick=()=>showPanel('Diary');$('tabArchive').onclick=()=>showPanel('Archive');$('tabVisitors').onclick=()=>showPanel('Visitors');
$('contact').onchange=()=>loadChats();$('refreshChats').onclick=()=>loadChats();$('olderChats').onclick=()=>loadChats(true);
$('loadDiary').onclick=()=>loadDiary();$('olderDiary').onclick=()=>loadDiary(true);
$('book').onchange=()=>{++diaryRequest;diaryBefore=0;diaryRows=[];updateDiaryPrivacy();$('diarySchedule').textContent='';$('diaryEntries').replaceChildren(line('p','点击“查看 / 刷新”读取所选日记本','hint'));$('olderDiary').hidden=true;$('loadDiary').disabled=false;};
window.addEventListener('beforeunload',clearMedia);

let archiveRequest=0,archiveData=null,memoryRequest=0,memoryOffset=0,memoryMore=false;
function durationLabel(seconds){if(seconds==null)return '时长未记录';const n=Math.round(seconds);return n<60?n+' 秒':Math.floor(n/60)+' 分 '+n%60+' 秒'}
function renderArchiveActivity(){
  const a=archiveData?.activity;if(accessRole==='guest'||!a)return;
  const filter=$('archiveApp').value,kind=$('archiveKind').value,rows=a.entries.filter(r=>(!filter||r.app===filter)&&(!kind||(r.kind||'browse')===kind));
  $('archiveEntries').replaceChildren(...(rows.length?rows.map(r=>{
    const item=line('article','','archiveEvent'),top=line('div','','archiveEventTop');
    top.append(line('time',new Date(r.at*1000).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})),line('strong',r.app),line('span',r.title||'手机浏览','archiveEventType'));
    if((r.kind||'browse')==='browse')top.append(line('span',durationLabel(r.duration_seconds),'archiveDuration'));
    const badges=line('div','','archiveEventBadges');badges.append(line('span',r.origin||'触发方式未记录'));
    if(r.result)badges.append(line('span',r.result));if(r.audience)badges.append(line('span',r.audience,'archiveAudience'));
    item.append(top,badges,line('p',r.note));
    if((r.kind||'browse')==='browse')item.append(line('small','记录完成 · '+(r.views??'—')+' 个有效画面 / '+(r.observations??'—')+' 次观察'));
    else if(r.kind==='qzone')item.append(line('small','发布尝试时间 · '+(r.has_image?'含配图':'未附图')+' · '+(r.author||'创作来源未记录')));
    else if(r.kind==='phone_attempt')item.append(line('small','检查时间 · 访问尝试不等于已完成使用'));
    else if(r.kind==='reading'||r.kind==='phone_action')item.append(line('small','实际工具回执'+(r.count>1?' · 合并 '+r.count+' 次':'')+' · 不推算浏览时长'));
    return item;
  }):[line('p','这一天还没有保存的活动记录。','archiveEmpty')]));
}
async function loadArchive(quiet=false){
  if(!unlocked)return;const request=++archiveRequest,guest=accessRole==='guest';
  if(!quiet){$('refreshArchive').disabled=true;$('archiveUpdated').textContent='正在读取档案…'}
  try{
    const day=guest?'':$('archiveDay').value;
    const d=await api('archive'+(day?'?day='+encodeURIComponent(day):''));
    if(request!==archiveRequest||!unlocked)return;
    archiveData=d;$('archiveName').textContent=d.profile.name;$('archiveState').textContent=d.current.label;
    $('archiveVoiceLabel').textContent=d.current.is_leave_note?'休息前留的话':'此刻的小声话';
    $('archiveStateNote').textContent=d.current.stale?'状态更新后，再听听它想说什么。':d.current.note||'它还没有留下一句话，晚些时候再来看看。';
    $('archiveStateNote').classList.toggle('isWaiting',d.current.voice_source!=='self_written'||d.current.stale);
    $('archiveMascot').src=['asleep','resting'].includes(d.current.state)?'/companion-winter-sleep.webp':'/companion-maid-awake.webp';
    $('archiveTraits').textContent=d.profile.traits;$('archiveTraitsKind').textContent=d.profile.traits_kind;
    $('archiveRoutinePeriods').replaceChildren(...(d.routine.periods||[]).map(period=>{
      const row=line('div'),times=line('dd');row.append(line('dt',period.label),times);
      if(period.windows?.length){for(const window of period.windows){const next=window.end<=window.start?'（次日）':'';times.append(line('span',window.start+' — '+window.end+next));}}
      else times.append(line('span','尚未安排','routinePending'));
      return row;
    }));
    $('archiveRoutineNote').textContent=d.routine.note;
    $('archiveUpdated').textContent='状态记录：'+stamp(d.current.updated_at)+' · 每 30 秒更新';
    $('archiveAdmin').hidden=guest||!d.activity;
    if(!guest&&d.activity){
      const a=d.activity;$('archiveDay').replaceChildren(...a.dates.map(date=>{const o=line('option',date);o.value=date;return o}));
      if(!a.dates.includes(a.day)){$('archiveDay').append(new Option(a.day,a.day))}$('archiveDay').value=a.day;
      const previous=$('archiveApp').value,all=line('option','全部应用');all.value='';
      $('archiveApp').replaceChildren(all,...a.apps.map(app=>{const o=line('option',app.name);o.value=app.name;return o}));
      $('archiveApp').value=a.apps.some(x=>x.name===previous)?previous:'';
      $('archiveStats').replaceChildren(...[['保存的活动',a.total+' 次'],['涉及平台',a.apps.length+' 个'],['已记录时长',a.missing_duration===a.total?'未记录':durationLabel(a.known_duration_seconds)],['空间发布回读',(a.qzone_count||0)+' 次']].map(([label,value])=>{const card=line('div');card.append(line('span',label),line('strong',value));return card}));
      $('archiveApps').replaceChildren(...(a.apps.length?a.apps.map(app=>{const row=line('div','','archiveAppRow'),p=line('p');p.append(line('span',app.name),line('span',app.count+' 次'));const bar=document.createElement('progress');bar.max=a.total;bar.value=app.count;bar.setAttribute('aria-label',app.name+'访问次数');row.append(p,bar);return row}):[line('p','暂无访问记录。','archiveFine')]));
      $('archiveTendencies').replaceChildren(line('h4','此刻的倾向'),...Object.entries({energy:'精力',curiosity:'好奇心',social:'社交意愿'}).map(([key,label])=>{const value=d.tendencies?.[key],row=line('div','','archiveAppRow');row.append(line('p',label+' · '+(value==null?'暂无记录':Math.round(value*100)+'%')));if(value!=null){const bar=document.createElement('progress');bar.max=1;bar.value=value;bar.setAttribute('aria-label',label);row.append(bar)}return row}),line('p','作息模型的模拟值，用于安排活动。','archiveFine'));
      $('archiveNotice').textContent=a.notice;renderArchiveActivity();
    }else{$('archiveEntries').replaceChildren();$('archiveStats').replaceChildren();$('archiveApps').replaceChildren();$('archiveTendencies').replaceChildren()}
  }catch(e){if(request===archiveRequest)$('archiveUpdated').textContent='档案读取失败：'+e.message}
  finally{if(request===archiveRequest)$('refreshArchive').disabled=false}
}
$('refreshArchive').onclick=()=>!$('archiveMemoryPanel').hidden?loadMemories(memoryOffset):loadArchive();$('archiveDay').onchange=()=>loadArchive();$('archiveApp').onchange=renderArchiveActivity;$('archiveKind').onchange=renderArchiveActivity;
setInterval(()=>{if(unlocked&&!$('archivePanel').hidden)loadArchive(true)},30000);

function resetMemoryView(){
  ++memoryRequest;memoryOffset=0;memoryMore=false;
  $('archiveDaily').hidden=false;$('archiveMemoryPanel').hidden=true;$('archiveMemoryNav').hidden=true;
  $('archiveDailyTab').setAttribute('aria-pressed','true');$('archiveMemoryTab').setAttribute('aria-pressed','false');
  $('memoryEntries').replaceChildren();$('memoryPage').textContent='';$('memoryNotice').textContent='请选择条件查询记忆。';
  $('memoryQuery').value='';$('memoryKind').value='';$('memoryTerm').value='';$('memoryState').value='active';
  const o=line('option','管理员');o.value='';$('memoryPerson').replaceChildren(o);
}
function memoryPersonLabel(qq){return qq==='100000001'?'管理员':'QQ '+qq}
async function loadMemories(offset=0){
  if(!unlocked||accessRole==='guest')return;
  const request=++memoryRequest;
  const args=new URLSearchParams({qq:$('memoryPerson').value,kind:$('memoryKind').value,term:$('memoryTerm').value,state:$('memoryState').value,q:$('memoryQuery').value.trim(),before:String(offset)});
  $('memorySearch').disabled=true;$('memoryPrevious').disabled=true;$('memoryNext').disabled=true;
  $('memoryEntries').replaceChildren();$('memoryNotice').textContent='正在查询已保存的记忆…';
  try{
    const d=await api('memories?'+args.toString());
    if(request!==memoryRequest||!unlocked||accessRole==='guest')return;
    memoryOffset=d.offset;memoryMore=d.has_more;$('archiveScroll').scrollTop=0;
    const contacts=d.contacts.some(c=>c.qq===d.qq)?d.contacts:[{qq:d.qq,count:0},...d.contacts];
    $('memoryPerson').replaceChildren(...contacts.map(c=>{const o=line('option',memoryPersonLabel(c.qq)+' · '+c.count+' 条');o.value=c.qq;return o}));$('memoryPerson').value=d.qq;
    $('memoryNotice').textContent=d.available?'找到 '+d.total+' 条符合条件的记忆。'+d.notice:'尚未找到已保存的记忆库。';
    const labels={fact:'事实与偏好',impression:'相处印象',growth:'成长倾向',episode:'共同经历',continuity:'近期话题'};
    const recentRecall=(d.recall_audit||[])[0];
    if(recentRecall)$('memoryNotice').textContent+=' 最近回忆：'+stamp(recentRecall.at)+'，'+(recentRecall.operation==='read'?'核对原文':'检索')+' '+recentRecall.matched_count+' 条。';
    $('memoryEntries').replaceChildren(...(d.entries.length?d.entries.map(r=>{
      const item=line('article','','memoryEntry'),top=line('div','','memoryEntryTop');
      const types={semantic:'稳定知识',plan:'计划与约定',state:'短时状态',working:'当前话题',episode:'情景记忆',impression:'主观印象',self_model:'自我档案'};
      const states={past_due:'已过时，完成情况待确认',historical:'历史记录',cancelled:'已取消',completed:'本人说已完成',conflicted:'存在冲突，待核对'};
      top.append(line('span',labels[r.kind]||'其他记忆','archiveTag'),line('span',types[r.memory_type]||(r.expires==null?'长期保存':'限期记录'),'archiveTag'));
      if(r.pinned)top.append(line('span','手动固定','memoryPinned'));if(states[r.status])top.append(line('span',states[r.status],'memoryExpired'));
      else if(r.expired)top.append(line('span','旧期限已到','memoryExpired'));
      if(r.review_due)top.append(line('span','需重新核对','archiveTag'));
      item.append(top,line('h4',r.topic),line('p',r.text));
      const meta=line('div','','memoryMeta');meta.append(line('span','更新于 '+stamp(r.updated)),line('span','建立于 '+stamp(r.created)),line('span',r.evidence_count+' 条依据'));
      if(r.expires!=null)meta.append(line('span','到期 '+stamp(r.expires)));
      if(r.valid_to!=null)meta.append(line('span','适用至 '+stamp(r.valid_to)));
      if(r.source_kind)meta.append(line('span',({self_report:'本人陈述',manual:'人工固定',conversation:'对话记录',inference:'推断或感想'})[r.source_kind]||'待核对来源'));
      item.append(meta);
      if(r.evidence_count){
        const button=line('button','查看原文依据','secondary'),evidence=line('div','','memoryEvidence');evidence.hidden=true;
        button.type='button';button.onclick=async()=>{
          if(!evidence.hidden){evidence.hidden=true;return}
          button.disabled=true;
          try{const proof=await api('memory-evidence?'+new URLSearchParams({qq:d.qq,id:r.id}));
            if(request!==memoryRequest||!unlocked||accessRole==='guest')return;
            evidence.replaceChildren(...proof.sources.map(s=>line('p',(s.role==='user'?'对方':'小鲸鱼')+' · '+stamp(s.at)+'\n'+s.text)),line('p',proof.notice,'hint'));evidence.hidden=false;
          }catch(e){if(request===memoryRequest){evidence.textContent='依据读取失败：'+e.message;evidence.hidden=false}}
          finally{button.disabled=false}
        };item.append(button,evidence);
      }
      return item;
    }):[line('p','没有符合当前条件的记忆。','archiveEmpty')]));
    $('memoryPage').textContent=d.total?'第 '+(d.offset+1)+'–'+(d.offset+d.entries.length)+' 条 / 共 '+d.total+' 条':'共 0 条';
    $('memoryPrevious').disabled=d.offset===0;$('memoryNext').disabled=!d.has_more;
  }catch(e){if(request===memoryRequest)$('memoryNotice').textContent='记忆查询失败：'+e.message}
  finally{if(request===memoryRequest)$('memorySearch').disabled=false}
}
$('archiveDailyTab').onclick=()=>{$('archiveDaily').hidden=false;$('archiveMemoryPanel').hidden=true;$('archiveDailyTab').setAttribute('aria-pressed','true');$('archiveMemoryTab').setAttribute('aria-pressed','false')};
$('archiveMemoryTab').onclick=()=>{if(accessRole==='guest')return;$('archiveDaily').hidden=true;$('archiveMemoryPanel').hidden=false;$('archiveDailyTab').setAttribute('aria-pressed','false');$('archiveMemoryTab').setAttribute('aria-pressed','true');loadMemories()};
$('memoryForm').onsubmit=event=>{event.preventDefault();loadMemories()};
$('memoryPrevious').onclick=()=>loadMemories(Math.max(0,memoryOffset-30));$('memoryNext').onclick=()=>{if(memoryMore)loadMemories(memoryOffset+30)};

$('authForm').onsubmit=async event=>{
  event.preventDefault();$('authError').textContent='';$('panelPassword').removeAttribute('aria-invalid');$('confirmPassword').removeAttribute('aria-invalid');
  const password=$('panelPassword').value,confirm=$('confirmPassword').value;
  if(setupRequired&&password!==confirm){$('authError').textContent='两次输入的密码不一致';$('confirmPassword').setAttribute('aria-invalid','true');return}
  $('authSubmit').disabled=true;$('authSubmit').setAttribute('aria-busy','true');$('authSubmit').textContent=setupRequired?'正在设置':'正在登录';
  try{
    const d=await api(setupRequired?'auth/setup':'auth/login','POST',{password,...(setupRequired?{confirm}:{})});
    panelSession=d.session;unlocked=true;applyAccessRole(d.role||'admin');
    $('panelPassword').value='';$('confirmPassword').value='';
    $('authPanel').hidden=true;$('protectedPanel').hidden=false;$('error').textContent='';
    startBalance();await showPanel('Connection');await status();
  }catch(e){$('authError').textContent=e.message;$('panelPassword').setAttribute('aria-invalid','true');$('panelPassword').value='';$('confirmPassword').value=''}
  finally{$('authSubmit').disabled=false;$('authSubmit').removeAttribute('aria-busy');$('authSubmit').textContent=setupRequired?'设置密码并进入':'登录'}
};
$('guestLogin').onclick=async()=>{
  $('guestLogin').disabled=true;$('authError').textContent='';
  try{
    const d=await api('auth/guest','POST');panelSession=d.session;unlocked=true;applyAccessRole('guest');await recordSiteVisit();
    $('panelPassword').value='';$('confirmPassword').value='';$('authPanel').hidden=true;$('protectedPanel').hidden=false;
    await showPanel('Connection');await status();
  }catch(e){$('authError').textContent=e.message}finally{$('guestLogin').disabled=false}
};
$('adminLogin').onclick=()=>showLogin(false);
let balanceTimer=null,balanceRequest=0,balanceLoading=false,billingRequest=0,billingLoading=false,billingImportReport=null;
let riceConnectionId='',riceConnectRequest=0;
function resetBalance(){
  clearInterval(balanceTimer);balanceTimer=null;++balanceRequest;balanceLoading=false;++billingRequest;billingLoading=false;
  resetRiceConnection();resetRiceImport();
  $('whaleTodayCost').textContent='—';$('accountTodayCost').textContent='—';$('riceLedgerDay').textContent='北京时间';$('riceLedgerNote').textContent='用餐记录还没接上，今天吃了多少先不乱报。';
  $('balanceWidget').hidden=true;$('balanceWidget').open=false;document.body.classList.remove('hasBalance');
  $('balanceValue').textContent='—';$('balanceRows').replaceChildren();$('balanceState').textContent='';$('balanceTime').textContent='每分钟更新';$('refreshBalance').disabled=false;
  $('balanceWhaleLine').textContent='';$('balanceVoiceNote').textContent='';
}
function startBalance(){
  resetBalance();if(!unlocked||accessRole!=='admin')return;
  $('balanceWidget').hidden=false;document.body.classList.add('hasBalance');loadBalance();loadBilling();
  balanceTimer=setInterval(()=>{if(!document.hidden){loadBalance();loadBilling()}},60000);
}
function balanceAmount(row,field='total_balance'){
  return (row.currency==='CNY'?'¥':'$')+Number(row[field]).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:4});
}
async function loadBalance(){
  if(balanceLoading||!unlocked||accessRole!=='admin')return;
  balanceLoading=true;const request=++balanceRequest;$('refreshBalance').disabled=true;
  try{
    const d=await api('balance');if(request!==balanceRequest||!unlocked||accessRole!=='admin')return;
    const ok=d.status==='ok';$('balanceWidget').dataset.state=ok?(d.is_available?'ok':'empty'):'error';
    $('balanceWidget').dataset.tone=d.voice?.tone||'unknown';
    $('balanceWhaleLine').textContent=d.voice?.text||'饭缸暂时看不清，等会儿再看看';
    $('balanceVoiceNote').textContent=d.voice?.notice||'按余额切换的页面文案，不是实时聊天。';
    $('balanceRows').replaceChildren();
    if(ok){
      $('balanceValue').textContent=d.balance_infos.map(r=>balanceAmount(r)).join(' / ');
      $('balanceState').textContent=d.voice?.label||(d.is_available?'余粮可用':'暂无可用余粮');
      for(const row of d.balance_infos){
        const card=line('div','','balanceCurrency');card.append(line('span',row.currency==='CNY'?'人民币 · CNY':'美元 · USD','balanceCurrencyName'),line('strong',balanceAmount(row),'balanceTotal'));
        const split=line('div','','balanceSplit');split.append(line('span','充值余额 '+balanceAmount(row,'topped_up_balance')),line('span','赠金 '+balanceAmount(row,'granted_balance')));card.append(split);$('balanceRows').append(card);
      }
      $('balanceTime').textContent='更新于 '+new Date(d.checked_at*1000).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})+' · 每分钟更新';
    }else{
      $('balanceValue').textContent='暂不可用';
      $('balanceState').textContent=({unconfigured:'尚未配置官方 DeepSeek API 密钥。',multiple_accounts:'检测到多个 DeepSeek 密钥，需要先指定查询账户。',invalid_key:'密钥无效或权限不足，请检查服务器配置。',rate_limited:'查询较频繁，请稍后重试。'})[d.code]||'暂时无法查询余额，请稍后重试。';
      $('balanceTime').textContent='每分钟重试';
    }
  }catch(e){if(request===balanceRequest&&unlocked&&accessRole==='admin'){$('balanceValue').textContent='暂不可用';$('balanceState').textContent='余粮查询失败，请稍后重试。';$('balanceWhaleLine').textContent='我没看清饭缸，不代表已经没米了。等会儿再看看';$('balanceRows').replaceChildren()}}
  finally{if(request===balanceRequest){balanceLoading=false;$('refreshBalance').disabled=false}}
}
function riceAmount(row,field){
  const value=Number(row[field]),symbol=row.currency==='CNY'?'¥':'US$';
  return value>0&&value<0.000001?'< '+symbol+'0.000001':symbol+value.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:6});
}
async function loadBilling(){
  if(billingLoading||!unlocked||accessRole!=='admin')return;
  billingLoading=true;const request=++billingRequest;
  try{
    const d=await api('usage');if(request!==billingRequest||!unlocked||accessRole!=='admin')return;
    $('riceLedgerDay').textContent=(d.day||'')+' · 北京时间';
    $('riceDisconnect').hidden=d.connected!==true;$('riceImport').hidden=true;
    $('riceAutoSummary').textContent=d.connected===true?'重新连接 / 更换官网授权':'连接官网自动记账';
    if(d.status==='ok'&&d.costs?.length){
      $('whaleTodayCost').textContent=d.costs.map(r=>riceAmount(r,'whale_cost')).join(' / ');
      $('accountTodayCost').textContent=d.costs.map(r=>riceAmount(r,'account_cost')).join(' / ');
      const time=d.checked_at?new Date(d.checked_at*1000).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'';
      $('riceLedgerNote').textContent=d.message+(time?' 更新于 '+time+'。':'');
    }else{
      $('whaleTodayCost').textContent='—';$('accountTodayCost').textContent='—';
      $('riceLedgerNote').textContent=d.message||'今天的用餐记录暂时没翻开，不能当作一粒米都没吃。';
    }
  }catch(e){if(request===billingRequest&&unlocked&&accessRole==='admin'){$('whaleTodayCost').textContent='—';$('accountTodayCost').textContent='—';$('riceLedgerNote').textContent='今天的用餐记录暂时没翻开，不能当作一粒米都没吃。'}}
  finally{if(request===billingRequest)billingLoading=false}
}
function resetRiceConnection(){
  ++riceConnectRequest;riceConnectionId='';$('riceAutoSetup').open=false;$('ricePlatformToken').value='';
  $('riceKeyForm').hidden=true;$('riceConnectedKey').replaceChildren();$('riceConnectConfirm').checked=false;
  $('riceConnectStatus').textContent='';$('riceConnectSubmit').disabled=false;$('riceSelectSubmit').disabled=false;
  $('riceDisconnect').hidden=true;$('riceDisconnect').disabled=false;$('riceImport').hidden=true;
  $('riceAutoSummary').textContent='连接官网自动记账';
}
$('riceConnectForm').onsubmit=async event=>{
  event.preventDefault();if(!unlocked||accessRole!=='admin')return;
  const request=++riceConnectRequest,token=$('ricePlatformToken').value;
  $('ricePlatformToken').value='';$('riceConnectSubmit').disabled=true;$('riceKeyForm').hidden=true;
  $('riceConnectStatus').textContent='正在验证官网授权…';riceConnectionId='';
  try{
    const d=await api('usage/connect','POST',{token});
    if(request!==riceConnectRequest||!unlocked||accessRole!=='admin')return;
    riceConnectionId=d.connection_id;
    $('riceConnectedKey').replaceChildren(new Option('请选择小鲸鱼的专用 Key',''),...d.keys.map(k=>new Option(k.name+(k.match===true?' · 与现用 Key 匹配':' · 请核对名称'),k.id)));
    const matched=d.keys.filter(k=>k.match===true);if(matched.length===1)$('riceConnectedKey').value=matched[0].id;
    $('riceConnectConfirm').checked=false;$('riceKeyForm').hidden=false;
    $('riceConnectStatus').textContent='官网授权已验证，请在3分钟内确认小鲸鱼使用的 Key。';
  }catch(e){if(request===riceConnectRequest)$('riceConnectStatus').textContent=e.message}
  finally{if(request===riceConnectRequest)$('riceConnectSubmit').disabled=false}
};
$('riceKeyForm').onsubmit=async event=>{
  event.preventDefault();if(!riceConnectionId||!unlocked||accessRole!=='admin')return;
  const request=riceConnectRequest;$('riceSelectSubmit').disabled=true;$('riceConnectStatus').textContent='正在收好饭票并读取今日账单…';
  try{
    await api('usage/select','POST',{connection_id:riceConnectionId,key_id:$('riceConnectedKey').value,confirmed:$('riceConnectConfirm').checked});
    if(request!==riceConnectRequest||!unlocked||accessRole!=='admin')return;
    resetRiceConnection();++billingRequest;billingLoading=false;await loadBilling();
  }catch(e){if(request===riceConnectRequest)$('riceConnectStatus').textContent=e.message}
  finally{if(request===riceConnectRequest)$('riceSelectSubmit').disabled=false}
};
$('riceDisconnect').onclick=async()=>{
  if(!unlocked||accessRole!=='admin')return;
  const request=riceConnectRequest;$('riceDisconnect').disabled=true;
  try{
    await api('usage/disconnect','POST');if(request!==riceConnectRequest||!unlocked||accessRole!=='admin')return;
    resetRiceConnection();++billingRequest;billingLoading=false;await loadBilling();
  }catch(e){if(request===riceConnectRequest)$('riceConnectStatus').textContent=e.message}
  finally{if(request===riceConnectRequest)$('riceDisconnect').disabled=false}
};
function resetRiceImport(){
  billingImportReport=null;$('riceImport').open=false;$('riceImportFile').value='';$('riceImportConfirm').checked=false;
  $('riceImportKey').replaceChildren(new Option('先选择费用数据文件',''));$('riceImportKey').disabled=true;
  $('riceImportSubmit').disabled=true;$('riceImportError').textContent='';
}
$('riceImportFile').onchange=async()=>{
  billingImportReport=null;$('riceImportSubmit').disabled=true;$('riceImportKey').disabled=true;$('riceImportError').textContent='';
  const request=billingRequest,file=$('riceImportFile').files[0];if(!file)return;
  try{
    if(file.size>2*1024*1024)throw Error('文件超过2MB，请缩小到今天的费用数据。');
    const value=JSON.parse(await file.text());
    if(request!==billingRequest||!unlocked||accessRole!=='admin')return;
    const data=value?.data?.biz_data;if(value.code!==0||value.data.biz_code!==0||!Array.isArray(data?.data))throw Error('请使用官网成功返回的费用明细 JSON。');
    const names=new Map();
    const groups=data.data.map(group=>({currency:group.currency,series:group.series.map(row=>{
      const key=row.api_key,tracking=key?.tracking_id,name=String(key?.name||'未命名 Key');
      if(typeof tracking==='string')names.set(tracking,/sk-|token|bearer|https?:|\d{5,}/i.test(name)?'所选 Key':name.slice(0,60));
      return {api_key:typeof tracking==='string'?{tracking_id:tracking,name:names.get(tracking)}:null,model:row.model,buckets:row.buckets.map(bucket=>({time:bucket.time,cost:bucket.cost}))};
    })}));
    if(!names.size)throw Error('数据中没有可选择的 Key，请检查导出的范围。');
    billingImportReport={code:0,data:{biz_code:0,biz_data:{start:data.start,end:data.end,bucket:data.bucket,data:groups}}};
    $('riceImportKey').replaceChildren(new Option('请选择小鲸鱼的专用 Key',''),...Array.from(names,([id,name])=>new Option(name,id)));
    $('riceImportKey').disabled=false;$('riceImportSubmit').disabled=false;
  }catch(e){if(request===billingRequest)$('riceImportError').textContent=e instanceof SyntaxError?'文件不是有效 JSON。':e.message}
};
$('riceImportForm').onsubmit=async event=>{
  event.preventDefault();if(!billingImportReport||!unlocked||accessRole!=='admin')return;
  const request=billingRequest;$('riceImportSubmit').disabled=true;$('riceImportError').textContent='';
  try{
    await api('usage/import','POST',{report:billingImportReport,key_id:$('riceImportKey').value,confirmed_all_keys:$('riceImportConfirm').checked});
    if(request!==billingRequest||!unlocked||accessRole!=='admin')return;
    resetRiceImport();await loadBilling();
  }catch(e){if(request===billingRequest){$('riceImportError').textContent=e.message;$('riceImportSubmit').disabled=false}}
};
$('refreshBalance').onclick=()=>{loadBalance();loadBilling()};
document.addEventListener('click',event=>{if(!$('balanceWidget').contains(event.target))$('balanceWidget').open=false});
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&$('balanceWidget').open){$('balanceWidget').open=false;$('balanceWidget').querySelector('summary').focus()}});
const navigationTabs=['Connection','Chats','Diary','Archive','Visitors'];
for(const name of navigationTabs){
  $('tab'+name).addEventListener('keydown',event=>{
    const visibleTabs=navigationTabs.filter(n=>!$('tab'+n).hidden);
    let index=visibleTabs.indexOf(name);
    if(['ArrowRight','ArrowDown'].includes(event.key))index=(index+1)%visibleTabs.length;
    else if(['ArrowLeft','ArrowUp'].includes(event.key))index=(index+visibleTabs.length-1)%visibleTabs.length;
    else if(event.key==='Home')index=0;
    else if(event.key==='End')index=visibleTabs.length-1;
    else return;
    event.preventDefault();$('tab'+visibleTabs[index]).focus();showPanel(visibleTabs[index]);
  });
}
const compactNavigation=window.matchMedia('(max-width:820px)');
function updateNavigationAxis(){document.querySelector('[role="tablist"]').setAttribute('aria-orientation',compactNavigation.matches?'horizontal':'vertical')}
compactNavigation.addEventListener('change',updateNavigationAxis);updateNavigationAxis();

let visitorRequest=0,visitorBefore=0,visitorSubmission=null;
function resetVisitorView(){++visitorRequest;visitorBefore=0;visitorSubmission=null;$('visitorNotes').replaceChildren();$('visitorVisits').replaceChildren();$('visitorStats').textContent='';$('visitorMessage').value='';$('visitorFeedback').textContent='';$('visitorAdminError').textContent='';$('olderVisitorNotes').hidden=true;}
async function recordSiteVisit(){try{await api('visit','POST',{})}catch{/* Browsing remains usable if visit recording fails. */}}
$('saveVisitorName').onclick=async()=>{const b=$('saveVisitorName');b.disabled=true;try{const d=await api('visitor-profile','POST',{nickname:$('visitorNickname').value});try{localStorage.setItem('whaleVisitorNickname',d.nickname)}catch{}$('visitorFeedback').textContent=d.nickname?'称呼已保存，仍属于未经验证的自填昵称。':'已保存为匿名访客。'}catch(e){$('visitorFeedback').textContent=e.message}finally{b.disabled=false}};
$('visitorForm').onsubmit=async event=>{event.preventDefault();const nickname=$('visitorNickname').value,message=$('visitorMessage').value;const b=$('sendVisitorMessage');b.disabled=true;try{if(!visitorSubmission||visitorSubmission.nickname!==nickname||visitorSubmission.message!==message)visitorSubmission={nickname,message,submission_id:crypto.randomUUID()};const d=await api('guestbook','POST',visitorSubmission);$('visitorFeedback').textContent='留言已收好，仅小鲸鱼和管理员可读。';$('visitorMessage').value='';visitorSubmission=null;try{localStorage.setItem('whaleVisitorNickname',nickname)}catch{}}catch(e){$('visitorFeedback').textContent=e.message}finally{b.disabled=false}};
async function loadVisitors(before=0){if(accessRole!=='admin'||!unlocked)return;const request=++visitorRequest;$('visitorAdminError').textContent='';try{const d=await api('visitors?before='+before);if(request!==visitorRequest||accessRole!=='admin'||!unlocked)return;$('visitorStats').textContent=`今日 ${d.stats.views} 次网站来访 · ${d.stats.today_notes} 条留言`;if(!before)$('visitorNotes').replaceChildren();for(const n of d.messages){const card=line('article','','visitorCard');card.append(line('h4',n.nickname?'自称 '+n.nickname+' · 身份未验证':'匿名访客'),line('p',n.message,'visitorText'),line('p',stamp(n.at),'hint'));$('visitorNotes').append(card)}if(!before&&!d.messages.length)$('visitorNotes').append(line('p','还没有留言，等一句不期而遇的问候。','hint'));visitorBefore=d.next_before;$('olderVisitorNotes').hidden=!visitorBefore;$('visitorVisits').replaceChildren(...d.visits.map(v=>line('p',stamp(v.at)+' · '+(v.nickname?'自称 '+v.nickname+'（未验证）':'匿名访客'),'visitorVisit')));if(!d.visits.length)$('visitorVisits').append(line('p','启用后有访客浏览时，记录会出现在这里。','hint'));}catch(e){if(request===visitorRequest)$('visitorAdminError').textContent=e.message}}
$('refreshVisitors').onclick=()=>loadVisitors();$('olderVisitorNotes').onclick=()=>loadVisitors(visitorBefore);

prepareLogin();
