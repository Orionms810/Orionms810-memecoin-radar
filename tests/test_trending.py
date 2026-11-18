import asyncio
import time

import pytest

from memebot.config import Settings
from memebot.demo import FakeData, make_token
from memebot.engine import Engine
from memebot.models import HolderInfo, TokenMetadata, Trade, TradeStats
from memebot.storage import Storage
from memebot.trending import TrendSettings, trend_trigger

from .test_engine import RecordingFeed, RecordingNotifier

V_20PCT = 1_073_000_000 - 0.2 * 793_100_000  # bonding curve à 20 %


def stats_with(buyers=25, buy_sol=6.0, sell_sol=0.5, progress_v=V_20PCT, age=40.0, now=1000.0):
    s = TradeStats(created_at=now - age, creator="DEV", last_v_tokens=progress_v)
    s.buyers = {f"b{i}" for i in range(buyers)}
    s.buy_sol, s.sell_sol = buy_sol, sell_sol
    return s


def test_trend_trigger_rules():
    th = TrendSettings()
    assert "25 acheteurs" in trend_trigger(stats_with(), 1000.0, th)
    assert trend_trigger(stats_with(age=10), 1000.0, th) is None  # trop tôt (bundles)
    assert trend_trigger(stats_with(age=400), 1000.0, th) is None  # fenêtre finie
    assert trend_trigger(stats_with(buyers=5), 1000.0, th) is None
    assert trend_trigger(stats_with(buy_sol=1.0), 1000.0, th) is None
    assert trend_trigger(stats_with(sell_sol=4.0), 1000.0, th) is None  # trop de ventes
    assert trend_trigger(stats_with(progress_v=1_070_000_000), 1000.0, th) is None
    s = stats_with()
    s.creator_sold = True
    assert trend_trigger(s, 1000.0, th) is None


@pytest.fixture
def setup(tmp_path):
    s = Settings(observe_seconds=0.1, watch_minutes=0.02, db_path=str(tmp_path / "t.db"))
    notifier, feed, data = RecordingNotifier(), RecordingFeed(), FakeData()
    engine = Engine(s, Storage(tmp_path / "t.db"), notifier, data, feed)
    yield engine, notifier, feed, data
    for t in list(engine.tasks):
        t.cancel()


def launch(engine, data, sym, *, age=40.0, creator=None):
    tok = make_token(f"Moon {sym}", sym, creator or f"Dev{sym}", 1.5)
    tok.created_at = time.time() - age
    data.holders_by_mint[tok.mint] = HolderInfo(18, 3, "w", 1.5, 20)
    data.meta_by_uri[tok.uri] = TokenMetadata(twitter="https://x.com/m")
    engine.on_new_token(tok)
    return tok


def pump(engine, tok, n=25, sol=0.3):
    for i in range(n):
        engine.on_trade(Trade(mint=tok.mint, trader=f"{tok.symbol}-b{i}", is_buy=True,
                              sol_amount=sol, token_amount=1e6, new_balance=1e6,
                              market_cap_sol=40 + i, v_tokens=V_20PCT))


async def test_token_taking_off_becomes_trend_alert(setup):
    engine, notifier, feed, data = setup
    tok = launch(engine, data, "MOON")
    await asyncio.sleep(0)  # l'abonnement aux trades se fait en tâche de fond
    assert tok.mint in engine.probation and tok.mint in feed.subs
    pump(engine, tok)
    assert tok.mint in engine.candidates and tok.mint not in engine.probation
    assert engine.candidates[tok.mint].stats.buys == 25  # garde ce qui a été observé
    await asyncio.sleep(3.3)
    alert = notifier.sent[-1][0]
    assert "🔥 <b>TOKEN TENDANCE</b>" in alert and "Décolle tout seul" in alert
    assert "narratif" not in alert.split("\n")[0]
    row = engine.storage.candidate_rows()[0]
    assert row["narrative"] == "TENDANCE" and row["decision"] == "alerte"


async def test_quota_and_dev_selling(setup):
    engine, notifier, feed, data = setup
    engine.s.trend.max_checks_per_hour = 1
    a, b = launch(engine, data, "AAA"), launch(engine, data, "BBB")
    pump(engine, a)
    pump(engine, b)
    assert a.mint in engine.candidates and b.mint not in engine.candidates  # quota de 1/h

    c = launch(engine, data, "CCC")
    engine.on_trade(Trade(mint=c.mint, trader=c.creator, is_buy=False, sol_amount=1,
                          token_amount=1, new_balance=0, market_cap_sol=30))
    await asyncio.sleep(0)
    assert c.mint not in engine.probation and c.mint not in feed.subs


async def test_probes_expire_and_release_subscription(setup):
    engine, notifier, feed, data = setup
    tok = launch(engine, data, "OLD", age=500)
    assert tok.mint in engine.probation
    task = asyncio.ensure_future(engine.run_trending(every=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    assert tok.mint not in engine.probation and tok.mint not in feed.subs


async def test_toggle_and_persistence(setup, tmp_path):
    engine, notifier, feed, data = setup
    launch(engine, data, "ONE")
    assert "activée" in await engine.handle_command("/tendance")
    assert "désactivée" in await engine.handle_command("/tendance off")
    await asyncio.sleep(0)
    assert not engine.probation and not feed.subs
    launch(engine, data, "TWO")
    assert not engine.probation  # plus d'observation quand c'est coupé
    again = Engine(engine.s, engine.storage, RecordingNotifier(), data, RecordingFeed())
    assert again.trend_enabled is False  # le choix est mémorisé
    assert "activée" in await engine.handle_command("/tendance on")
    assert "Tendance : on" in await engine.handle_command("/status")


async def test_narrative_match_reuses_probe_stats(setup):
    engine, notifier, feed, data = setup
    tok = launch(engine, data, "CAT", age=5)
    pump(engine, tok, n=5)
    assert tok.mint in engine.probation
    await engine.handle_command("/narratif moon")  # un post arrive après le lancement
    cand = engine.candidates[tok.mint]
    assert cand.match.narrative.id != "TENDANCE" and cand.stats.buys == 5


async def test_trader_buys_trend_tokens_only_if_allowed(setup):
    from memebot.trading.paper import PaperExecutor
    from memebot.trading.settings import TradingSettings
    from memebot.trading.trader import Trader

    engine, notifier, feed, data = setup
    trader = Trader(TradingSettings(paper_latency_seconds=0), None, engine.storage, notifier,
                    feed=feed)
    trader.ex = PaperExecutor(trader.price_of, fee_pct=1.75, tx_fee_sol=0.0003, latency=0)
    engine.trader = trader
    engine.s.trend.trade = False
    tok = launch(engine, data, "NOPE")
    pump(engine, tok)
    await asyncio.sleep(3.3)
    assert not trader.positions and "TENDANCE" in notifier.sent[-1][0]

    engine.s.trend.trade = True
    tok2 = launch(engine, data, "YES")
    pump(engine, tok2)
    await asyncio.sleep(3.3)
    assert tok2.mint in trader.positions
    for t in list(trader.tasks):
        t.cancel()
