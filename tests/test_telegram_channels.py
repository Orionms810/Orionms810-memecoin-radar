import asyncio
from types import SimpleNamespace

import pytest

from memebot.sources.telegram_channels import (
    ChannelCfg,
    TelegramChannelsSource,
    message_to_event,
    normalize_handle,
)
from memebot.storage import Storage


def test_normalize_handle():
    assert normalize_handle("@ElonTweets") == "elontweets"
    assert normalize_handle("https://t.me/ElonTweets/123") == "elontweets"
    assert normalize_handle("t.me/elon_tweets?x=1") == "elon_tweets"
    assert normalize_handle("@a") is None  # trop court
    assert normalize_handle("pas valide !") is None


def test_message_to_event():
    cfg = ChannelCfg(username="elontweets", label="Elon (via Telegram)", weight=0.9,
                     aliases=("elon",), chat_id=-1001)
    ev = message_to_event(cfg, 42, "  Elon Musk: DOGE to MARS  ")
    assert ev.source == "telegram" and ev.source_name == "Elon (via Telegram)"
    assert ev.text == "Elon Musk: DOGE to MARS" and ev.weight == 0.9
    assert ev.url == "https://t.me/elontweets/42" and ev.event_id == "tg:-1001:42"
    assert ev.announce is False and ev.aliases == ("elon",)
    assert message_to_event(cfg, 43, "   ") is None  # photo sans texte


@pytest.fixture
def source(tmp_path):
    events = []

    async def emit(ev):
        events.append(ev)

    storage = Storage(tmp_path / "t.db")
    src = TelegramChannelsSource(1, "hash", str(tmp_path / "s"),
                                 [ChannelCfg(username="fromconfig", weight=1.0)], emit, storage)
    src.events = events
    return src


async def test_channel_commands_offline(source, tmp_path):
    assert "fromconfig" in await source.handle_command("/chaines", "")
    assert "Usage" in await source.handle_command("/chaine", "")
    assert "poids" in await source.handle_command("/chaine", "@elontweets abc")
    reply = await source.handle_command("/chaine", "https://t.me/ElonTweets 0.9")
    assert "ajoutée" in reply and "dès que la connexion" in reply
    assert "déjà suivie" in await source.handle_command("/chaine", "@elontweets")
    assert "config.toml" in await source.handle_command("/retirer", "@fromconfig")

    # persistance : une nouvelle instance retrouve la chaîne ajoutée
    again = TelegramChannelsSource(1, "hash", "s", [], source.emit, source.storage)
    assert again.channels["elontweets"].weight == 0.9

    assert "retirée" in await source.handle_command("/retirer", "@elontweets")
    assert "introuvable" in await source.handle_command("/retirer", "@elontweets")
    assert await source.handle_command("/autre", "") is None


async def test_only_followed_channels_are_read(source):
    cfg = source.channels["fromconfig"]
    cfg.chat_id = -100123
    source.by_chat[cfg.chat_id] = cfg

    def msg(chat_id, text, mid=7):
        return SimpleNamespace(chat_id=chat_id, raw_text=text, message=SimpleNamespace(id=mid))

    await source._on_message(msg(555, "message privé à ne pas lire"))
    await source._on_message(msg(-100123, "NEW: Elon tweeted COVFEFE"))
    assert len(source.events) == 1 and source.events[0].text == "NEW: Elon tweeted COVFEFE"


async def test_resolve_with_fake_telethon_client(source):
    pytest.importorskip("telethon")
    from telethon.tl.types import Channel, ChatPhotoEmpty, User

    def channel(cid, title):
        return Channel(id=cid, title=title, photo=ChatPhotoEmpty(), date=None, broadcast=True,
                       access_hash=1)

    class FakeClient:
        def __init__(self):
            self.joined = []

        async def get_entity(self, username):
            if username == "missing":
                raise ValueError("No user has that username")
            if username == "someperson":
                return User(id=9)
            return channel(777, "Elon Tweets")

        async def __call__(self, request):
            self.joined.append(request)

    source.client, source.connected = FakeClient(), True
    reply = await source.handle_command("/chaine", "@elontweets 0.9")
    assert "Elon Tweets" in reply and "Je lis" in reply and len(source.client.joined) == 1
    assert source.by_chat[-1000000000777].username == "elontweets"
    assert "introuvable" in await source.handle_command("/chaine", "@missing")
    assert "pas une chaîne" in await source.handle_command("/chaine", "@someperson")
    assert "connecté" in await source.handle_command("/chaines", "")


async def test_engine_routes_channel_commands_and_does_not_spam(tmp_path):
    from memebot.config import Settings
    from memebot.demo import FakeData
    from memebot.engine import Engine

    from .test_engine import RecordingFeed, RecordingNotifier

    s = Settings(db_path=str(tmp_path / "e.db"))
    storage, notifier = Storage(tmp_path / "e.db"), RecordingNotifier()
    engine = Engine(s, storage, notifier, FakeData(), RecordingFeed())
    assert "TG_API_ID" in await engine.handle_command("/chaines")

    src = TelegramChannelsSource(1, "h", "s", [], engine.on_event, storage)
    engine.tg_channels = src
    assert "Aucune chaîne" in await engine.handle_command("/chaines")

    cfg = ChannelCfg(username="elontweets", weight=1.0, chat_id=-1, label="Elon")
    src.by_chat[-1] = cfg
    await src._on_message(SimpleNamespace(chat_id=-1, raw_text="Buy $ROCKET now",
                                          message=SimpleNamespace(id=1)))
    await asyncio.sleep(0)
    assert engine.book.live() and "rocket" in engine.book.live()[0].keywords
    assert notifier.sent == []  # poids 1.0 mais announce=False : pas de message à chaque post
