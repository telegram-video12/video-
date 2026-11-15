import asyncio
from types import SimpleNamespace

from app.telegram import TelegramService


class FakeClient:
    def __init__(self):
        self.calls = []

    def is_connected(self):
        return True

    async def get_input_entity(self, chat_id):
        return chat_id

    async def get_messages(self, entity, ids):
        self.calls.append(list(ids))
        return [None if i == 3 else SimpleNamespace(id=i) for i in ids]

    async def download_media(self, msg, file=None, thumb=None):
        return b"jpg%d" % msg.id


def test_thumbnails_share_one_telegram_request():
    tg = TelegramService.__new__(TelegramService)
    tg._msg_cache = {}
    tg.client = FakeClient()
    tg._require = lambda: tg.client

    async def go():
        return await asyncio.gather(*(tg.thumbnail(-100, i) for i in range(1, 9)))

    out = asyncio.run(go())
    assert tg.client.calls == [[1, 2, 3, 4, 5, 6, 7, 8]]  # one request for the whole page
    assert out[0] == b"jpg1" and out[2] is None  # a deleted message just has no picture
