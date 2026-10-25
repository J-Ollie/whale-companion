"""Local Unix socket for the authenticated site's narrow connection action."""
import asyncio
import json
import os
from .phone import PhoneError


class ConnectionControl:
    def __init__(self,phone):
        self.phone=phone;self.path=phone.root/'connection-control.sock';self.server=None

    async def start(self):
        self.path.unlink(missing_ok=True)
        self.server=await asyncio.start_unix_server(self.handle,path=str(self.path),limit=2048)
        os.chmod(self.path,0o600)

    async def close(self):
        if self.server:
            self.server.close();await self.server.wait_closed()
        self.path.unlink(missing_ok=True)

    async def reconnect(self,serial):
        self.phone.validate_serial(serial)
        if self.phone.lock.locked():return {'ok':False,'code':'busy'}
        async with self.phone.lock:
            old=self.phone.state['serial'];self.phone.state['serial']=serial
            try:
                # No port discovery, wake or app actions, including during sleep.
                await self.phone._connect_once()
                if await self.phone._shell('getprop','ro.product.model')!='V2046A':
                    raise PhoneError('wrong_phone')
                if await self.phone._shell('echo','whale-link-ok')!='whale-link-ok':
                    raise PhoneError('echo_failed')
                self.phone._save();self.phone.screen=None
            except BaseException:
                self.phone.state['serial']=old
                raise
        return {'ok':True,'code':'connected'}

    async def handle(self,reader,writer):
        reply={'ok':False,'code':'invalid_request'}
        try:
            raw=await asyncio.wait_for(reader.readline(),3)
            if len(raw)>1024:raise ValueError()
            data=json.loads(raw)
            if not isinstance(data,dict) or data.get('action')!='connect':raise ValueError()
            serial=data.get('serial')
            if not isinstance(serial,str):raise ValueError()
            reply=await asyncio.wait_for(self.reconnect(serial),30)
        except asyncio.TimeoutError:reply={'ok':False,'code':'timeout'}
        except PhoneError:reply={'ok':False,'code':'connection_failed'}
        except Exception:pass
        try:
            writer.write(json.dumps(reply).encode()+b'\n');await writer.drain()
        finally:
            writer.close();await writer.wait_closed()
