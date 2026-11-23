import asyncio
import json
import unittest

from live.push_transport import PushConnection


class FakeSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.incoming.get()
        if value is None:
            raise StopAsyncIteration
        return json.dumps(value)

    async def send(self, text):
        packet = json.loads(text)
        # A notification arrives between request and matching response.
        await self.incoming.put({'method': 3001, 'dataType': 'party-event'})
        await self.incoming.put({'method': packet['method'], 'tranId': packet['tranId'], 'returnCode': 0})

    async def close(self):
        await self.incoming.put(None)


class PushTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_interleaved_event_is_not_lost_during_request(self):
        connection = PushConnection('fake', '123')
        connection.ws = FakeSocket()
        connection._reader = asyncio.create_task(connection._read())
        try:
            result = await connection.request(lambda t: json.dumps({'method': 1003, 'tranId': t}))
            self.assertEqual(result['returnCode'], 0)
            self.assertEqual((await connection.receive(.1))['dataType'], 'party-event')
        finally:
            await connection.close()

    async def test_disconnect_wakes_event_consumer(self):
        connection = PushConnection('fake', '123')
        connection.ws = FakeSocket()
        connection._reader = asyncio.create_task(connection._read())
        await connection.ws.close()
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            await connection.receive(.5)
        await connection.close()

    async def test_overflow_fails_instead_of_silently_losing_signaling(self):
        connection = PushConnection('fake', '123')
        connection.ws = FakeSocket()
        connection.events = asyncio.Queue(maxsize=1)
        connection._reader = asyncio.create_task(connection._read())
        for _ in range(2):
            await connection.ws.incoming.put({'method': 3001})
        await connection._reader
        with self.assertRaisesRegex(RuntimeError, 'QueueFull'):
            await connection.receive(.5)
        await connection.close()
