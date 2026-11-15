"""Checks TelegramService.stream's 1MiB alignment logic against a fake Telethon client."""
import asyncio
from types import SimpleNamespace

from app.telegram import CHUNK, TelegramService

DATA = bytes((i * 7) % 251 for i in range(int(CHUNK * 3.5)))


class FakeClient:
    async def iter_download(self, media, offset=0, request_size=CHUNK):
        assert offset % CHUNK == 0
        for pos in range(offset, len(DATA), request_size):
            yield DATA[pos:pos + request_size]


def make():
    svc = TelegramService(1, "x", "/tmp/unused")
    svc.client = FakeClient()
    svc.authorized = True

    async def get_message(*a, **k):
        return SimpleNamespace(media=object())

    svc.get_message = get_message
    return svc


def collect(svc, start, end):
    async def run():
        return b"".join([c async for c in svc.stream(1, 1, start, end)])
    return asyncio.run(run())


def test_ranges():
    svc = make()
    for start, end in [(0, 10), (5, CHUNK + 5), (CHUNK - 1, CHUNK), (CHUNK * 2 + 123, len(DATA) - 1), (0, len(DATA) - 1)]:
        assert collect(svc, start, end) == DATA[start:end + 1], (start, end)
