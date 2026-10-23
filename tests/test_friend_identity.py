"""Fixed read-only UID lookup: wire fixtures, errors and identity binding."""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0,os.environ.get('COMPANION_TEST_PLUGINS','/AstrBot/data/plugins'))
from astrbot_plugin_companion_friends.identity import request,decode,resolve,fields,one,COMMAND

UID='u_1234567890'
# Independent wire fixture: OIDB FE1/2 -> body -> profile UID + UIN=10001.
PROFILE=b'\x08\xe1\x1f\x10\x02\x22\x13\x0a\x11\x0a\x0c'+UID.encode()+b'\x18\x91\x4e'


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_by_uid_without_friend_list_or_acceptance(self):
        call=AsyncMock(return_value=PROFILE.hex())
        result=await resolve(call,UID)
        self.assertEqual(result['qq'],'10001')
        self.assertEqual(result['source'],'qq_uid_profile')
        call.assert_awaited_once()
        args=call.await_args
        self.assertEqual(args.args,('send_packet',))
        self.assertEqual(args.kwargs['cmd'],COMMAND)
        self.assertTrue(args.kwargs['rsp'])
        envelope=fields(bytes.fromhex(args.kwargs['data']))
        self.assertEqual(one(envelope,1,0),0xfe1)
        self.assertEqual(one(envelope,2,0),2)
        self.assertEqual(one(envelope,12,0,0),0)
        self.assertEqual(one(fields(one(envelope,4,2)),1,2).decode(),UID)

    async def test_rejects_mismatched_uid_even_with_plausible_number(self):
        with self.assertRaisesRegex(ValueError,'profile_uid_mismatch'):
            decode('u_0987654321',PROFILE.hex())

    async def test_rejects_missing_or_invalid_account(self):
        for ending in [b'\x18\x00',b'\x18\x01']:
            malformed=PROFILE[:-3]+ending
            with self.assertRaises(ValueError):decode(UID,malformed.hex())

    async def test_rejects_wrong_service_or_rejected_lookup(self):
        for raw in [PROFILE.replace(b'\x10\x02',b'\x10\x03',1),b'\x18\x01'+PROFILE]:
            with self.assertRaises(ValueError):decode(UID,raw.hex())

    async def test_rejects_malformed_or_oversized_reply(self):
        for raw in ['not-hex','f',PROFILE[:-1].hex(),'00','aa'*262145]:
            with self.assertRaises(ValueError):decode(UID,raw)

    async def test_invalid_handles_never_reach_gateway(self):
        call=AsyncMock()
        for uid in ['',None,'0','ordinary-request','u_x','u_1234567890\n']:
            with self.assertRaises(ValueError):await resolve(call,uid)
        call.assert_not_called()

    async def test_failed_query_does_not_guess_or_retry(self):
        call=AsyncMock(side_effect=asyncio.TimeoutError())
        with self.assertRaises(asyncio.TimeoutError):await resolve(call,UID)
        self.assertEqual(call.await_count,1)


if __name__=='__main__':unittest.main()
