"""App scope, photos, animation and outbound limits; no live phone or QQ I/O."""
import asyncio,io,json,os,sys,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from datetime import datetime
sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from PIL import Image
from astrbot_plugin_companion_phone.media import MediaLibrary
from astrbot_plugin_companion_phone.outbound import Outbound
from astrbot_plugin_companion_phone.phone import Phone,PhoneError
from astrbot_plugin_companion_phone.autonomy import AutonomousBrowser,TZ
from astrbot_plugin_companion_phone.access import enabled_apps


def png(color='blue'):
    stream=io.BytesIO();Image.new('RGB',(80,80),color).save(stream,format='PNG');return stream.getvalue()


class MediaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.phone=Phone(self.tmp.name);self.phone._connect=AsyncMock();self.phone._shell=AsyncMock(return_value='')
        self.cfg={'album_share_scope':'all'}
        self.lib=MediaLibrary(self.tmp.name,self.phone,self.cfg)

    def test_private_crop_owner_only_not_public(self):
        row=self.lib.ingest(png(),origin='private_screen')
        self.lib.asset(row['id'],share=True)
        with self.assertRaises(PhoneError):self.lib.asset(row['id'],share=True,public=True)

    def test_full_album_scope_rechecked_when_changed(self):
        row=self.lib.ingest(png(),origin='album',bucket='Camera')
        self.lib.asset(row['id'],share=True,public=True)
        self.cfg['album_share_scope']='shared_album'
        with self.assertRaises(PhoneError):self.lib.asset(row['id'],share=True)

    def test_animated_sticker_preserved_and_post_flattens(self):
        out=io.BytesIO();frames=[Image.new('RGB',(60,60),c) for c in ('red','blue')]
        frames[0].save(out,format='GIF',save_all=True,append_images=frames[1:],duration=120,loop=0)
        row=self.lib.ingest(out.getvalue(),origin='web')
        self.assertTrue(row['animated'])
        _,path=self.lib.asset(row['id'])
        with Image.open(path) as image:self.assertEqual(image.n_frames,2)
        target=Path(self.tmp.name)/'post.jpg';self.lib.post_image(row['id'],target)
        with Image.open(target) as image:self.assertEqual(image.format,'JPEG')

    def test_dedup_does_not_promote_private_image(self):
        first=self.lib.ingest(png(),origin='private_screen')
        second=self.lib.ingest(png(),origin='public_screen')
        self.assertEqual(first['id'],second['id'])
        self.assertFalse(self.lib.shareable(second,public=True))

    async def test_no_raw_path_url_or_unlisted_album_reads(self):
        for ref in ('/etc/passwd','https://example.com/a.jpg','album:123'):
            with self.assertRaises(PhoneError):await self.lib.materialize(ref)
        self.phone._connect.assert_not_called()

    async def test_all_album_pagination_can_reach_older_other_album_photo(self):
        self.phone._shell.return_value='\n'.join(
            f'Row: {i} _id={i+1}, bucket_display_name=Camera, _size=1200, date_added={100-i}'
            for i in range(15))
        first=await self.lib.album()
        second=await self.lib.album(12)
        self.assertEqual(len(first),12)
        self.assertEqual(len(second),3)
        self.assertEqual(self.lib.album_total,15)
        self.assertEqual(second[-1]['id'],'album:15')
        self.assertFalse(set(r['id'] for r in first)&set(r['id'] for r in second))
        self.phone._run=AsyncMock(return_value=png())
        row=await self.lib.materialize('album:15')
        self.assertTrue(self.lib.shareable(row,public=True))
        self.cfg['album_share_scope']='disabled'
        with self.assertRaises(PhoneError):await self.lib.album(12)

    async def test_gallery_scope_and_oversize_filtered(self):
        self.phone._shell.return_value='Row: 0 _id=12, bucket_display_name=Camera, _size=1200, date_added=100\nRow: 1 _id=13, bucket_display_name=Camera, _size=99999999, date_added=101'
        result=await self.lib.album()
        self.assertEqual([r['id'] for r in result],['album:12'])
        self.phone._run=AsyncMock(return_value=png())
        row=await self.lib.materialize('album:12')
        self.assertEqual(row['origin'],'album')
        self.assertIn('content://media/external/images/media/12',self.phone._run.call_args.args[0])

    async def test_album_security_error_is_not_empty_gallery(self):
        self.phone._shell.return_value='java.lang.SecurityException: Permission Denial'
        with self.assertRaises(PhoneError):await self.lib.album()

    async def test_shared_album_filter_is_enforced(self):
        self.cfg['album_share_scope']='shared_album'
        self.phone._shell.return_value='Row: 0 _id=12, bucket_display_name=Camera, _size=1200, date_added=100'
        self.assertEqual(await self.lib.album(),[])
        self.assertIn("bucket_display_name='小鲸鱼可分享'",self.phone._shell.call_args.args)

    def test_library_quota_preserves_existing_files(self):
        row=self.lib.ingest(png(),origin='web')
        self.lib.index['assets'].update({str(n):{'at':0} for n in range(2000)})
        with self.assertRaises(PhoneError):self.lib.ingest(png('red'),origin='web')
        self.assertTrue(self.lib.asset(row['id'])[1].exists())

    def test_corrupt_library_does_not_reset_index(self):
        self.lib.path.write_text('broken')
        with self.assertRaises(PhoneError):MediaLibrary(self.tmp.name,self.phone,self.cfg)
        self.assertEqual(self.lib.path.read_text(),'broken')


class OutboundTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.send=AsyncMock(return_value=12345)
        self.lib=MediaLibrary(self.root,Phone(self.root),{'album_share_scope':'all'})
        self.o=Outbound(self.root,self.lib,self.send,lambda:True)
        self.clock=patch('time.time',return_value=datetime(2026,10,20,14,tzinfo=TZ).timestamp());self.clock.start();self.addCleanup(self.clock.stop)

    async def test_three_proactive_shares_max_even_after_restart(self):
        day='2026-10-20'
        self.o.state['shares']=[{'day':day,'at':time.time()-8000,'result':'sent'}]*3;self.o.save()
        other=Outbound(self.root,self.lib,self.send,lambda:True)
        result=await other.share('这个有意思','real-source')
        self.assertEqual(result['result'],'not_due');self.send.assert_not_called()

    async def test_busy_sleep_pause_prevent_sharing_but_reply_faces_work(self):
        self.o.notify_allowed=lambda:False
        self.assertEqual((await self.o.share('有趣','source'))['result'],'not_due')
        self.o.state['paused']=True
        self.assertEqual((await self.o.share('有趣','source'))['result'],'paused')
        self.assertEqual((await self.o.face(0,request_id='msg1'))['result'],'sent')

    async def test_failed_send_counted_and_not_retried(self):
        self.send.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError):await self.o.share('看这个','source')
        self.assertEqual(self.o.used(),1);self.assertEqual(self.o.state['shares'][0]['result'],'uncertain')
        self.assertEqual((await self.o.share('看这个','source'))['result'],'not_due')
        self.send.assert_called_once()

    async def test_dedup_chat_request_and_owner_image_encoding(self):
        row=self.lib.ingest(png(),origin='web')
        result=await self.o.image(row['id'],'这张',request_id='incoming-1')
        self.assertEqual(result['result'],'sent')
        self.assertTrue(self.send.call_args.args[0][-1]['data']['file'].startswith('base64://'))
        self.assertEqual((await self.o.face(13,request_id='incoming-1'))['result'],'duplicate')
        self.send.assert_called_once()

    async def test_share_caption_and_image_are_one_receipted_message(self):
        row=self.lib.ingest(png(),origin='web')
        result=await self.o.share('看着舒服','source-1',row['id'])
        self.assertEqual(result['used_today'],1)
        self.assertEqual([r['type'] for r in self.send.call_args.args[0]],['text','image'])
        self.assertEqual(self.o.state['shares'][-1]['caption'],'看着舒服')


class AppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.phone=Phone(self.tmp.name)
        self.cfg={'rhythm_enabled':False,'autonomous_apps':['tv.danmaku.bili'],'installed_app_names':{'tv.danmaku.bili':'哔哩哔哩'},'autonomous_like_consent':True}
        self.a=AutonomousBrowser(self.phone,self.cfg,AsyncMock(),lambda:'')
        self.phone._assert_foreground=AsyncMock();self.phone.target_info=AsyncMock(return_value={'text':'点赞'})
        self.phone.tap=AsyncMock();self.phone.long_press=AsyncMock()

    def test_only_verified_named_apps_enabled(self):
        self.cfg['autonomous_apps']+=['com.bad.app']
        self.assertEqual(enabled_apps(self.cfg),{'tv.danmaku.bili':'哔哩哔哩'})

    async def test_forbidden_post_message_follow_actions_blocked(self):
        for label in ('发送','发布','发表评论','关注','购买','删除'):
            self.phone.target_info.return_value={'text':label}
            self.assertFalse(await self.a.interactive_action('tv.danmaku.bili',{'action':'tap','intent':'navigate','x':100,'y':100}))
        self.phone.tap.assert_not_called()

    async def test_like_is_opt_in_not_already_liked_and_reserved(self):
        action={'action':'like','x':100,'y':100,'like_state':'liked'}
        self.assertFalse(await self.a.interactive_action('tv.danmaku.bili',action))
        action['like_state']='not_liked'
        self.assertTrue(await self.a.interactive_action('tv.danmaku.bili',action))
        self.assertEqual(self.a.state['likes_used'],1)
        self.cfg['autonomous_like_consent']=False
        self.assertFalse(await self.a.interactive_action('tv.danmaku.bili',action))

    async def test_refresh_and_search_supported_in_allowed_foreground(self):
        self.phone.swipe=AsyncMock()
        self.assertTrue(await self.a.interactive_action('tv.danmaku.bili',{'action':'refresh'}))
        self.phone.swipe.assert_awaited_once()
        self.phone._input_field=AsyncMock(return_value={'text':'搜索','resource-id':'search_input'})
        self.phone.type_ascii=AsyncMock()
        self.assertTrue(await self.a.interactive_action('tv.danmaku.bili',{'action':'input','text':'鲸鱼'}))
        self.phone.type_ascii.assert_awaited_once()

    async def test_comment_field_not_usable_as_autonomous_search(self):
        self.phone._input_field=AsyncMock(return_value={'text':'说点什么','resource-id':'comment_edit'})
        self.phone.type_ascii=AsyncMock()
        self.assertFalse(await self.a.interactive_action('tv.danmaku.bili',{'action':'input','text':'你好'}))
        self.phone.type_ascii.assert_not_called()


if __name__=='__main__':unittest.main()
