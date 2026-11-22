"""Sorties « moonbag » : paliers, trailing large, lecture des vendeurs, graduation,
et suivi du prix après la vente."""

import asyncio
import time

import pytest

from memebot.curves import CurveState
from memebot.models import PUMP_TOTAL_SUPPLY, NewToken, RiskReport, Trade
from memebot.report import TokenReport
from memebot.storage import Storage
from memebot.trading.paper import PaperExecutor
from memebot.trading.settings import TradingSettings
from memebot.trading.trader import Trader

from .test_engine import RecordingFeed, RecordingNotifier


# Mêmes aides que test_trading.py, sans dépendre de solders (installation légère)
def report(safety=80, momentum=60):
    tok = NewToken(mint="MintPos", name="Nugget", symbol="NUG", creator="Dev", market_cap_sol=30.0)
    return tok, TokenReport(token=tok, risk=RiskReport(safety, [], False), momentum=momentum)


@pytest.fixture
def trader(tmp_path):
    t = Trader(TradingSettings(paper_latency_seconds=0, fee_pct=0, tx_fee_sol=0), None,
               Storage(tmp_path / "t.db"), RecordingNotifier(), feed=RecordingFeed())
    t.ex = PaperExecutor(t.price_of, fee_pct=0, tx_fee_sol=0, latency=0)
    yield t
    for task in list(t.tasks):
        task.cancel()


def tick(trader, mcap):
    trader.on_trade(Trade(mint="MintPos", trader="someone", is_buy=True, sol_amount=0.1,
                          token_amount=1, new_balance=None, market_cap_sol=mcap))


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def curve(mcap: float, complete: bool = False) -> CurveState:
    return CurveState(v_token_raw=10**15, v_sol_lamports=int(mcap * 1e9),
                      real_sol_lamports=int(5e9), complete=complete)


def deal(trader, mcap, *, buy, sol, who="other", mint="MintPos"):
    trader.on_trade(Trade(mint=mint, trader=who, is_buy=buy, sol_amount=sol, token_amount=1,
                          new_balance=None, market_cap_sol=mcap))


async def buy_at_30(trader, bc="BC1"):
    tok, rep = report()
    tok.bonding_curve = bc
    assert await trader.on_signal(tok, rep, None) is None
    return trader.positions["MintPos"]


async def test_second_tier_then_moonbag_keeps_running(trader):
    pos = await buy_at_30(trader)
    full = pos.tokens
    assert "à x5 vente de 50% du reste" in trader.notifier.sent[-1][0]
    tick(trader, 60)  # x2 : la moitié est vendue
    await settle()
    assert pos.tp_done and "déjà encaissé" in trader.notifier.sent[-1][0]
    tick(trader, 150)  # x5 : 2e palier, la moitié du reste
    await settle()
    assert pos.tp2_done and pos.tokens == pytest.approx(full / 4)
    tick(trader, 600)  # x20 : rien à faire, ça court
    await settle()
    assert "MintPos" in trader.positions and pos.peak_gain_pct == pytest.approx(1900)
    tick(trader, 400)  # -33 % depuis le plus haut : on garde
    await settle()
    assert "MintPos" in trader.positions


async def test_exit_when_sellers_take_over(trader):
    pos = await buy_at_30(trader)
    tick(trader, 60)
    await settle()
    tick(trader, 100)  # plus haut à x3,3
    deal(trader, 85, buy=False, sol=1.5)  # -15 % : pas encore assez bas
    await settle()
    assert "MintPos" in trader.positions
    deal(trader, 78, buy=False, sol=1.0)  # -22 %, 2,5 SOL vendus contre 0,2 achetés
    await settle()
    assert "MintPos" not in trader.positions
    assert "les vendeurs prennent le dessus" in trader.notifier.sent[-1][0]
    assert pos.realized_sol > pos.entry_sol


async def test_dip_with_strong_buyers_is_held(trader):
    await buy_at_30(trader)
    tick(trader, 60)
    await settle()
    deal(trader, 100, buy=True, sol=4.0)  # gros achats
    deal(trader, 78, buy=False, sol=3.0)  # -22 % mais les acheteurs dominent encore
    await settle()
    assert "MintPos" in trader.positions


async def test_own_sells_do_not_count_as_pressure(trader):
    trader.own_wallet = "ME"
    pos = await buy_at_30(trader)
    deal(trader, 30, buy=False, sol=5.0, who="ME")
    assert trader.sell_pressure(pos) == (0.0, 0.0)
    deal(trader, 30, buy=False, sol=0.4)
    assert trader.sell_pressure(pos) == (0.0, 0.4)


async def test_reversal_only_for_moonbag(trader):
    await buy_at_30(trader)
    tick(trader, 39)  # +30 %, avant x2
    deal(trader, 31, buy=False, sol=3.0)  # -20 % mais pas encore de prise de profit
    await settle()
    assert "MintPos" in trader.positions  # la protection d'avant x2 gère (trailing armé à +40 %)


async def test_moonbag_does_not_block_new_trades(trader):
    await buy_at_30(trader)
    other = NewToken(mint="Mint2", name="Two", symbol="TWO", creator="Dev2", market_cap_sol=30.0)
    rep2 = TokenReport(token=other, risk=RiskReport(80, [], False), momentum=60)
    assert "maximum" in await trader.on_signal(other, rep2, None)  # 1 position max
    tick(trader, 60)  # x2 : mise récupérée -> moonbag
    await settle()
    assert await trader.on_signal(other, rep2, None) is None
    assert set(trader.positions) == {"MintPos", "Mint2"}
    assert "dont 1 moonbag" in await trader.status_text()
    text = trader.positions_text()
    assert "moonbag" in text and "protection serrée" in text


async def test_missed_trades_and_graduation(trader):
    pos = await buy_at_30(trader, bc="BCX")
    states = {"BCX": curve(45)}

    async def fetch(addrs):
        return {a: states[a] for a in addrs if a in states}

    async def dex(mint):
        return 120 / PUMP_TOTAL_SUPPLY  # x4 sur PumpSwap

    trader.fetch_curves, trader.dex_price = fetch, dex
    pos.last_trade_at = time.time() - 30  # silence : des trades ont pu nous échapper
    await trader.refresh_prices()
    assert pos.last_price == pytest.approx(45 / PUMP_TOTAL_SUPPLY) and not pos.graduated

    states["BCX"] = curve(410, complete=True)
    pos.last_trade_at = time.time() - 30
    trader._last_curve_check = trader._last_dex_check = 0
    await trader.refresh_prices()
    await settle()
    assert pos.graduated and pos.last_price == pytest.approx(120 / PUMP_TOTAL_SUPPLY)
    assert any("a gradué" in m[0] for m in trader.notifier.sent)
    pos.last_trade_at = time.time() - 3600  # plus de trades pump.fun : normal après graduation
    pos.tp_done = True
    pos.peak_price = pos.last_price
    assert trader.exit_reason(pos) is None


async def test_graduation_seen_in_trade(trader):
    pos = await buy_at_30(trader)
    trader.on_trade(Trade(mint="MintPos", trader="x", is_buy=True, sol_amount=1, token_amount=1,
                          new_balance=None, market_cap_sol=55, v_tokens=279_000_000))
    await settle()
    assert pos.graduated


async def test_after_sale_tracking_and_report(trader):
    await buy_at_30(trader, bc="BCA")
    tick(trader, 22)  # -27 % -> stop-loss
    await settle()
    assert "MintPos" not in trader.positions and len(trader.after) == 1
    states = {"BCA": curve(120)}

    async def fetch(addrs):
        return {a: states[a] for a in addrs if a in states}

    trader.fetch_curves = fetch
    await trader.watch_after()
    assert "suivi en cours" in trader.exits_text()
    states["BCA"] = curve(40)
    await trader.watch_after(time.time() + 2 * 3600 + 5)
    assert not trader.after
    row = trader.storage.trade_rows("paper")[0]
    assert row["exit_x"] == pytest.approx(22 / 30, abs=1e-3)
    assert row["after_max_x"] == pytest.approx(4.0) and row["after_end_x"] == pytest.approx(40 / 30, abs=1e-3)
    text = trader.exits_text()
    assert "sorti trop tôt" in text and "1 trop tôt" in text
    assert await trader.handle_command("/sorties", "") == text


async def test_after_watch_survives_restart(trader):
    await buy_at_30(trader, bc="BCR")
    tick(trader, 22)
    await settle()
    again = Trader(trader.ts, None, trader.storage, trader.notifier)
    assert len(again.after) == 1 and next(iter(again.after.values())).bonding_curve == "BCR"


async def test_engine_commands_and_dex_price(tmp_path):
    from memebot.config import Settings
    from memebot.demo import FakeData
    from memebot.engine import HELP, Engine
    from memebot.models import DexInfo
    class FileNotifier(RecordingNotifier):
        files = []

        async def send_document(self, filename, content, caption=""):
            self.files.append((filename, content))
            return True

    class Data(FakeData):
        async def dexscreener(self, mint):
            return DexInfo("pumpswap", "P", "N", "S", 0.0003, 300_000, 1, 1, 1, 1, 1, 1, 1, None)

    storage = Storage(tmp_path / "e.db")
    engine = Engine(Settings(db_path=str(tmp_path / "e.db")), storage, FileNotifier(), Data(),
                    RecordingFeed())
    engine.trader = Trader(TradingSettings(), None, storage, engine.notifier)
    assert "/sorties" in HELP and "/export trades" in HELP
    assert "Aucune vente suivie" in await engine.handle_command("/sorties")
    assert "Aucun trade" in await engine.handle_command("/export trades")
    storage.add_trade(mode="paper", mint="M", symbol="S", name="N", opened_at=1, entry_sol=0.01,
                      exit_sol=0.02, pnl_sol=0.01, reason="test", exit_x=2.0, peak_x=3.0)
    assert await engine.handle_command("/export trades") is None
    name, content = FileNotifier.files[-1]
    assert name.startswith("trades-") and b"exit_x" in content
    engine.sol_usd = 150.0
    assert await engine.dex_price_sol("M") == pytest.approx(0.0003 / 150)
    for task in list(engine.tasks):
        task.cancel()
