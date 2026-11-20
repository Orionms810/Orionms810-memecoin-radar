import asyncio
import time

import pytest

from memebot.config import Settings
from memebot.curves import CurveState
from memebot.demo import FakeData, make_token
from memebot.engine import Engine
from memebot.models import HolderInfo, NewToken, TokenMetadata, Trade
from memebot.storage import Storage
from memebot.trending import TrendSettings, curve_trigger

from .test_engine import RecordingFeed, RecordingNotifier


def curve(real_sol=6.0, progress=20.0, complete=False) -> CurveState:
    v_tok = (1_073_000_000 - progress / 100 * 793_100_000) * 1e6
    v_sol = 30e9 * 1.073e15 / v_tok  # produit constant de la bonding curve
    return CurveState(v_token_raw=int(v_tok), v_sol_lamports=int(v_sol),
                      real_sol_lamports=int(real_sol * 1e9), complete=complete)


def test_curve_state_and_trigger_rules():
    c = curve(progress=20)
    assert c.progress_pct == pytest.approx(20, abs=0.01)
    assert curve(progress=0).market_cap_sol == pytest.approx(27.96, abs=0.01)
    th, now = TrendSettings(), 1000.0
    tok = NewToken(mint="M", name="N", symbol="S", creator="D", sol_amount=1.0, created_at=now - 40)
    assert "5.0 SOL entrés" in curve_trigger(tok, curve(real_sol=6.0), now, th)
    assert curve_trigger(tok, curve(real_sol=4.5), now, th) is None  # 3,5 SOL hors dev
    assert curve_trigger(tok, curve(progress=5), now, th) is None
    assert curve_trigger(tok, curve(complete=True), now, th) is None
    young = NewToken(mint="M", name="N", symbol="S", creator="D", created_at=now - 10)
    assert curve_trigger(young, curve(), now, th) is None  # trop tôt (bundles)
    old = NewToken(mint="M", name="N", symbol="S", creator="D", created_at=now - 400)
    assert curve_trigger(old, curve(), now, th) is None


@pytest.fixture
def setup(tmp_path):
    s = Settings(observe_seconds=0.2, watch_minutes=0.02, db_path=str(tmp_path / "t.db"))
    notifier, feed, data = RecordingNotifier(), RecordingFeed(), FakeData()
    engine = Engine(s, Storage(tmp_path / "t.db"), notifier, data, feed)
    yield engine, notifier, feed, data
    for t in list(engine.tasks):
        t.cancel()


def launch(engine, data, sym, *, age=40.0, state=None):
    tok = make_token(f"Moon {sym}", sym, f"Dev{sym}", 1.5)
    tok.created_at = time.time() - age
    data.holders_by_mint[tok.mint] = HolderInfo(18, 3, "w", 1.5, 20)
    data.meta_by_uri[tok.uri] = TokenMetadata(twitter="https://x.com/m")
    if state is not None:
        data.curves_by_address[tok.bonding_curve] = state
    engine.on_new_token(tok)
    return tok


def pump(engine, tok, n=25, sol=0.3):
    for i in range(n):
        engine.on_trade(Trade(mint=tok.mint, trader=f"{tok.symbol}-b{i}", is_buy=True,
                              sol_amount=sol, token_amount=1e6, new_balance=1e6,
                              market_cap_sol=45 + i))


async def test_token_taking_off_becomes_trend_alert(setup):
    engine, notifier, feed, data = setup
    tok = launch(engine, data, "MOON", state=curve(real_sol=7.0, progress=25))
    await asyncio.sleep(0)
    assert tok.mint in engine.probation and not feed.subs  # observation sans écouter les trades
    await engine.poll_trending()
    assert tok.mint in engine.candidates and tok.mint not in engine.probation
    await asyncio.sleep(0)
    assert tok.mint in feed.subs  # candidat : trades écoutés pendant l'observation
    pump(engine, tok)
    await asyncio.sleep(0.5)
    alert = notifier.sent[-1][0]
    assert "🔥 <b>TOKEN TENDANCE</b>" in alert and "SOL entrés" in alert
    row = engine.storage.candidate_rows(0)[0]
    assert row["narrative"] == "TENDANCE" and row["decision"] == "alerte"


async def test_quota_graduated_and_expiry(setup):
    engine, notifier, feed, data = setup
    engine.s.trend.max_checks_per_hour = 1
    a = launch(engine, data, "AAA", state=curve())
    b = launch(engine, data, "BBB", state=curve())
    g = launch(engine, data, "GRAD", state=curve(complete=True))
    old = launch(engine, data, "OLD", age=500, state=curve())
    await engine.poll_trending()
    promoted = [t for t in (a, b) if t.mint in engine.candidates]
    assert len(promoted) == 1  # quota de 1 analyse par heure
    assert g.mint not in engine.probation  # déjà gradué : abandonné
    assert old.mint not in engine.probation  # période d'observation finie


async def test_dead_token_stays_in_observation(setup):
    engine, notifier, feed, data = setup
    tok = launch(engine, data, "DEAD", state=curve(real_sol=1.6, progress=1))
    await engine.poll_trending()
    assert tok.mint in engine.probation and tok.mint not in engine.candidates


async def test_toggle_and_persistence(setup):
    engine, notifier, feed, data = setup
    launch(engine, data, "ONE", state=curve())
    assert "activée" in await engine.handle_command("/tendance")
    assert "désactivée" in await engine.handle_command("/tendance off")
    assert not engine.probation
    launch(engine, data, "TWO", state=curve())
    assert not engine.probation  # plus d'observation quand c'est coupé
    again = Engine(engine.s, engine.storage, RecordingNotifier(), data, RecordingFeed())
    assert again.trend_enabled is False  # le choix est mémorisé
    assert "activée" in await engine.handle_command("/tendance on")
    assert "Tendance : on" in await engine.handle_command("/status")


async def test_narrative_match_takes_over_probe(setup):
    engine, notifier, feed, data = setup
    tok = launch(engine, data, "CAT", age=5, state=curve())
    assert tok.mint in engine.probation
    await engine.handle_command("/narratif moon")  # un post arrive après le lancement
    cand = engine.candidates[tok.mint]
    assert cand.match.narrative.id != "TENDANCE" and tok.mint not in engine.probation


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
    tok = launch(engine, data, "NOPE", state=curve())
    await engine.poll_trending()
    pump(engine, tok)
    await asyncio.sleep(0.5)
    assert not trader.positions and "TENDANCE" in notifier.sent[-1][0]

    engine.s.trend.trade = True
    tok2 = launch(engine, data, "YES", state=curve())
    await engine.poll_trending()
    pump(engine, tok2)
    await asyncio.sleep(0.5)
    assert tok2.mint in trader.positions
    for t in list(trader.tasks):
        t.cancel()
