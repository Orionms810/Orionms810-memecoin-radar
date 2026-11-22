"""Snipes : CA posté par un compte de confiance, créateurs suivis, créateurs qui ont
déjà réussi, et /snipe."""

import asyncio
import hashlib
import time

import pytest

from memebot.chainfeed import b58encode
from memebot.config import Settings
from memebot.curves import CurveState
from memebot.demo import FakeData, make_token
from memebot.engine import HELP, Engine
from memebot.models import DexInfo, NewsEvent
from memebot.storage import Storage
from memebot.trading.paper import PaperExecutor
from memebot.trading.settings import TradingSettings
from memebot.trading.trader import Trader
from memebot.vip import bonding_curve_address, extract_mints, is_pubkey

from .test_engine import RecordingFeed, RecordingNotifier


def addr(seed: str) -> str:
    return b58encode(hashlib.sha256(seed.encode()).digest())


def curve(mcap: float, complete: bool = False) -> CurveState:
    return CurveState(v_token_raw=10**15, v_sol_lamports=int(mcap * 1e9),
                      real_sol_lamports=int(5e9), complete=complete)


def test_addresses_and_bonding_curve():
    # Adresse de courbe vérifiée avec la librairie officielle (solders)
    assert bonding_curve_address("9Sffa4yWra4XwLnnrRgz3tzDfH8J87o9j1xfomARpump") == \
        "9mckwxy8S4QSEN3Wr6YwBhFRFjf3KJARjxBU7q4FqobV"
    text = ("Wallet EYc1JWBgQyKb4Sx6BgF2ShavoxZRexJcsYVk8bt6FFQz · CA : "
            "https://pump.fun/coin/9Sffa4yWra4XwLnnrRgz3tzDfH8J87o9j1xfomARpump !")
    assert extract_mints(text) == ["9Sffa4yWra4XwLnnrRgz3tzDfH8J87o9j1xfomARpump",
                                   "EYc1JWBgQyKb4Sx6BgF2ShavoxZRexJcsYVk8bt6FFQz"]
    assert extract_mints("Elon tweete DOGE to the moon 2026") == []
    assert is_pubkey(addr("x")) and not is_pubkey("abc") and not is_pubkey("0OIl" * 9)


class Data(FakeData):
    dex: DexInfo | None = None

    async def dexscreener(self, mint):
        return self.dex


@pytest.fixture
def env(tmp_path):
    storage = Storage(tmp_path / "s.db")
    notifier, feed, data = RecordingNotifier(), RecordingFeed(), Data()
    engine = Engine(Settings(db_path=str(tmp_path / "s.db"), observe_seconds=0.2,
                             watch_minutes=0.02), storage, notifier, data, feed)
    trader = Trader(TradingSettings(paper_latency_seconds=0), None, storage, notifier, feed=feed)
    trader.ex = PaperExecutor(trader.price_of, fee_pct=1.75, tx_fee_sol=0.0003, latency=0)
    engine.trader = trader
    engine.sol_usd = 150.0
    yield engine, notifier, data, trader
    for t in list(engine.tasks) + list(trader.tasks):
        t.cancel()


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)


def post(n, text, channel="elonposts"):
    return NewsEvent(source="telegram", source_name="Elon (posts)", event_id=f"tg:{channel}:{n}",
                     text=text, url=f"https://t.me/{channel}/{n}", weight=0.9)


async def test_trusted_channel_ca_is_bought_at_once(env):
    engine, notifier, data, trader = env
    mint = addr("elon-coin")
    data.curves_by_address[bonding_curve_address(mint)] = curve(35)
    await engine.on_event(post(1, f"lol {mint}"))
    await settle()
    assert not trader.positions  # chaîne pas encore de confiance : on ignore les adresses

    reply = await engine.handle_command("/confiance @ElonPosts")
    assert "compte de confiance" in reply and "UNIQUEMENT" in reply
    await engine.on_event(post(2, f"new coin {mint}"))
    await settle()
    pos = trader.positions[mint]
    assert pos.source == "CA posté par Elon (posts)" and not pos.graduated
    assert pos.bonding_curve == bonding_curve_address(mint)
    texts = [m[0] for m in notifier.sent]
    assert any("SNIPE" in t and "CA posté par" in t for t in texts)
    assert any("ACHAT" in t and "⚡ CA posté par" in t for t in texts)

    await engine.on_event(post(3, f"again {mint}"))  # même adresse : un seul achat
    await settle()
    assert sum("SNIPE" in t for t, _ in notifier.sent) == 1
    assert "@elonposts" in await engine.handle_command("/confiance")
    assert "n'est plus" in await engine.handle_command("/confiance retirer @elonposts")
    assert not engine.trusted


async def test_untrusted_and_non_pump_addresses(env):
    engine, notifier, data, trader = env
    await engine.handle_command("/confiance @elonposts")
    other = addr("not-pump")  # pas de bonding curve pump.fun
    await engine.on_event(post(1, f"look {other}"))
    await settle()
    assert not trader.positions
    assert "pas un token pump.fun" in notifier.sent[-1][0]
    mint = addr("from-elsewhere")
    data.curves_by_address[bonding_curve_address(mint)] = curve(35)
    await engine.on_event(post(1, f"buy {mint}", channel="callschannel"))
    await settle()
    assert not trader.positions


async def test_graduated_token_uses_dexscreener(env):
    engine, notifier, data, trader = env
    mint = addr("graduated")
    data.curves_by_address[bonding_curve_address(mint)] = curve(410, complete=True)
    data.dex = DexInfo("pumpswap", "P", "Big", "BIG", 0.0002, 200_000, 1, 1, 1, 1, 1, 1, 1, None)
    assert await engine.handle_command(f"/snipe {mint}") is None
    await settle()
    pos = trader.positions[mint]
    assert pos.graduated and pos.symbol == "BIG"
    assert pos.last_price == pytest.approx(200_000 / 150 / 1e9)
    assert mint not in engine.feed.subs  # plus de trades pump.fun à écouter


async def test_snipe_limits(env):
    engine, notifier, data, trader = env
    mint = addr("too-big")
    data.curves_by_address[bonding_curve_address(mint)] = curve(400)
    engine.s.snipe.max_mcap_sol = 300
    await engine.handle_command(f"/snipe {mint}")
    await settle()
    assert not trader.positions and "Pas d'achat" in notifier.sent[-1][0]
    engine.s.snipe.trade = False  # alerte seule
    mint2 = addr("alert-only")
    data.curves_by_address[bonding_curve_address(mint2)] = curve(35)
    await engine.handle_command(f"/snipe {mint2}")
    await settle()
    assert not trader.positions and "SNIPE" in notifier.sent[-1][0]
    assert "achat non" in await engine.handle_command("/status")
    assert "Usage" in await engine.handle_command("/snipe hello")


async def test_watched_creator_launch_is_bought_even_during_pause(env):
    engine, notifier, data, trader = env
    wallet = addr("famous-dev")
    reply = await engine.handle_command(f"/createur {wallet} Streamer Bob")
    assert "Streamer Bob" in reply
    assert "Streamer Bob" in await engine.handle_command("/createurs")
    trader.guard.state.cooldown_until = time.time() + 3600  # pause après 3 pertes
    tok = make_token("Bob Coin", "BOB", wallet, 2.0)
    tok.created_at = time.time()
    engine.on_new_token(tok)
    await settle()
    pos = trader.positions[tok.mint]
    assert pos.source == "nouveau token de Streamer Bob (créateur suivi)"
    other = make_token("Other", "OTH", "someone", 2.0)
    other.created_at = time.time()
    engine.on_new_token(other)  # créateur inconnu : pas de snipe
    await settle()
    assert other.mint not in trader.positions
    assert "n'est plus suivi" in await engine.handle_command(f"/createur retirer {wallet}")
    assert "Usage" in await engine.handle_command("/createur pas-un-wallet")


async def test_snipe_takes_extra_slot(env):
    engine, notifier, data, trader = env
    a, b, c = addr("a"), addr("b"), addr("c")
    for m in (a, b, c):
        data.curves_by_address[bonding_curve_address(m)] = curve(35)
    for m in (a, b):
        await engine.handle_command(f"/snipe {m}")
        await settle()
    assert set(trader.positions) == {a, b}  # 1 position normale + 1 place pour les snipes
    await engine.handle_command(f"/snipe {c}")
    await settle()
    assert c not in trader.positions and "maximum" in notifier.sent[-1][0]


async def test_proven_creator_is_analysed_not_bought(env):
    engine, notifier, data, trader = env
    wallet = addr("proven")
    engine.storage.census_add({"mint": addr("old"), "created_at": time.time() - 86400,
                               "creator": wallet, "graduated": 1})
    assert engine.storage.creator_graduations(wallet) == 1
    tok = make_token("Moon Again", "MOON2", wallet, 2.0)
    tok.created_at = time.time()
    engine.on_new_token(tok)
    cand = engine.candidates[tok.mint]
    assert cand.match.narrative is engine.creator_narrative
    from memebot.demo import buys
    buys(engine, tok, 25, 0.3, 30, 0.4)
    await asyncio.sleep(0.4)
    alert = next(t for t, _ in notifier.sent if "CRÉATEUR QUI A DÉJÀ RÉUSSI" in t)
    assert "1 ancien(s) token(s) gradué(s)" in alert
    assert not trader.positions  # observé et mesuré, pas acheté (proven_trade = false)
    row = engine.storage.candidate_rows(0)[0]
    assert row["narrative"] == "CREATEUR"


def test_help_and_settings():
    assert "/confiance" in HELP and "/createur" in HELP and "/snipe" in HELP
    s = Settings()
    assert s.snipe.trade and s.snipe.max_mcap_sol == 2000 and not s.snipe.proven_trade


async def test_proven_creator_checks_are_capped(env):
    engine, notifier, data, trader = env
    engine.s.snipe.proven_max_per_hour = 1
    wallet = addr("serial-winner")
    engine.storage.census_add({"mint": addr("old2"), "created_at": 1.0, "creator": wallet,
                               "graduated": 1})
    t1, t2 = make_token("One", "ONE", wallet, 2.0), make_token("Two", "TWO", wallet, 2.0)
    engine.on_new_token(t1)
    engine.on_new_token(t2)
    assert t1.mint in engine.candidates and t2.mint not in engine.candidates
