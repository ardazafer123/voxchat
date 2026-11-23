"""Authenticated push notification transport.

One reader routes replies and notifications; events arriving during a request
are retained. A lost connection fails the session instead of silently losing
signaling. Callers must leave their membership before reconnecting.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
import re
import ssl

from websockets.asyncio.client import connect

from live.transport import get_url
from psclient import push

BOOTSTRAP = ('https://mobile-pushcl.np.communication.playstation.net/np/serveraddr'
             '?keepAliveStatusType=6&fields=keepAliveStatus&version=3.0')


def tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    # Some push hosts omit this public intermediate from their TLS chain.
    # Keep hostname and chain verification enabled.
    intermediate = Path(__file__).with_name('certs') / 'comodo-rsa-dv.pem'
    if intermediate.exists():
        context.load_verify_locations(cafile=str(intermediate))
    return context


def bootstrap(token: str) -> dict:
    response = get_url(BOOTSTRAP, token)
    if response.status != 200 or not isinstance(response.body, dict):
        raise RuntimeError(f'Push bootstrap failed: HTTP {response.status}')
    data = response.body
    if not re.fullmatch(r'[a-z0-9-]+-pushcl\.np\.communication\.playstation\.net', data.get('fqdn', '')):
        raise RuntimeError('Unexpected push server address')
    return data


class PushConnection:
    def __init__(self, token: str, account_id: str):
        self.token = token
        self.account_id = account_id
        self.events = asyncio.Queue(maxsize=256)
        self._pending = {}
        self._counter = 0
        self._reader = None
        self._heartbeat = None
        self.ws = None
        self.keepalive = {'clientKeepAliveInterval': 10000, 'clientKeepAliveTimeout': 40000}

    async def __aenter__(self):
        data = await asyncio.to_thread(bootstrap, self.token)
        headers = {
            'Authorization': 'Bearer ' + self.token,
            'X-PSN-RECONNECTION': 'false', 'X-PSN-OS-VER': '14',
            'X-PSN-KEEP-ALIVE-STATUS-TYPE': '6', 'X-PSN-PROTOCOL-VERSION': '3.0',
            'X-PSN-APP-TYPE': 'MOBILE_APP.PSAPP', 'X-PSN-APP-VER': '26.9.2',
        }
        self.ws = await connect(push.socket_url(data['fqdn']), additional_headers=headers,
                                subprotocols=['np-pushpacket'], compression=None,
                                user_agent_header=None, ssl=tls_context(),
                                ping_interval=None, open_timeout=20, close_timeout=3,
                                max_size=1 << 20, proxy=None)
        self._reader = asyncio.create_task(self._read())
        try:
            await self.request(lambda t: push.change_user_status('3.0', t, self.account_id, self.token))
            reply = await self.request(lambda t: push.change_keep_alive_status_type('3.0', t, 3))
            self.keepalive.update(reply.get('keepAliveStatus', {}))
            self._heartbeat = asyncio.create_task(self._keepalive())
            return self
        except BaseException:
            await self.close()
            raise

    async def request(self, builder):
        self._counter += 1
        transaction = str(self._counter)
        future = asyncio.get_running_loop().create_future()
        self._pending[transaction] = future
        try:
            await self.ws.send(builder(transaction))
            reply = await asyncio.wait_for(future, 20)
            if reply.get('returnCode') != 0:
                raise RuntimeError(f"Push request rejected: code {reply.get('returnCode')}")
            return reply
        finally:
            self._pending.pop(transaction, None)

    async def _read(self):
        error = RuntimeError('Push connection closed')
        try:
            async for raw in self.ws:
                packet = json.loads(raw)
                transaction = str(packet.get('tranId', ''))
                if 'returnCode' in packet and transaction in self._pending:
                    future = self._pending[transaction]
                    if not future.done():
                        future.set_result(packet)
                elif packet.get('method') == 3001:
                    self.events.put_nowait(packet)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never forward raw WebSocket frames/headers into diagnostics.
            error = RuntimeError(f'Push reader failed: {type(exc).__name__}')
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            if self.events.full():
                self.events.get_nowait()
            self.events.put_nowait(error)

    async def _keepalive(self):
        try:
            while True:
                interval = max(1, min(60, self.keepalive['clientKeepAliveInterval'] / 1000))
                await asyncio.sleep(interval)
                pong = await self.ws.ping()
                await asyncio.wait_for(pong, min(30, self.keepalive['clientKeepAliveTimeout'] / 1000))
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.ws.close()

    async def receive(self, timeout=None):
        item = await asyncio.wait_for(self.events.get(), timeout)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        for task in (self._heartbeat, self._reader):
            if task:
                task.cancel()
        for task in (self._heartbeat, self._reader):
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self.ws:
            await self.ws.close()

    async def __aexit__(self, *args):
        await self.close()
