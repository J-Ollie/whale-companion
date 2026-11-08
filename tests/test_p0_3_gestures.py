"""Gesture boundaries with mocked transport; no real device or network."""
import tempfile,time,unittest
from pathlib import Path
from unittest.mock import AsyncMock,Mock
from astrbot_plugin_companion_phone.phone import Phone,PhoneError
from astrbot_plugin_companion_phone.native_transport import NativeTransport

class GestureTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.p=Phone(Path(self.tmp.name));self.p.ready=Mock();self.p._assert_foreground=AsyncMock();self.p._shell=AsyncMock();self.p._reserve_action=Mock();self.p.screen=(1080,2400,time.monotonic())
 async def test_duration_boundaries_and_no_dispatch(self):
  for n in (99,1501,2000,True,100.5):
   with self.subTest(n=n):
    with self.assertRaisesRegex(PhoneError,'100–1500'):await self.p.swipe(0,0,1000,1000,n)
    self.p._shell.assert_not_awaited();self.p._reserve_action.assert_not_called()
  for n in (100,1500):
   self.p.screen=(1080,2400,time.monotonic());await self.p.swipe(0,0,1000,1000,n)
   self.assertEqual(self.p._shell.await_args.args,('input','swipe',0,0,1079,2399,n))
 async def test_coordinates_and_long_press(self):
  for x in (-1,1001,True,0.5):
   with self.assertRaises(PhoneError):await self.p.tap(x,500)
  self.p._shell.assert_not_awaited()
  await self.p.long_press(1000,0);self.assertEqual(self.p._shell.await_args.args,('input','swipe',1079,0,1079,0,900))
 async def test_native_wire_shape_and_direction(self):
  e=Mock();e.status={'foreground':'app'};e.rpc=AsyncMock(return_value={'ok':True});n=NativeTransport(e)
  await n.shell(('input','swipe','1','20','3','4','1500'))
  e.rpc.assert_awaited_once_with('swipe',{'x1':1,'y1':20,'x2':3,'y2':4,'duration':1500,'package':'app'})
 def test_android_source_contract(self):
  s=(Path(__file__).parent/'native-phone/src/com/whalecompanion/agent/AccessService.java').read_text(encoding='utf8')
  for fragment in ('x<0||y<0||x>=m.widthPixels||y>=m.heightPixels','op.equals("tap")?70:args.getInt("duration")','duration<50||duration>1500','throw new Exception("invalid_duration")','safeTarget(x,y)','if(x!=x2||y!=y2)path.lineTo(x2,y2)'):
   self.assertIn(fragment,s)

if __name__=='__main__':unittest.main()
