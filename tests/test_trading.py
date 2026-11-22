import asyncio
import base64
import json
import time

import httpx
import pytest
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from memebot.models import PUMP_TOTAL_SUPPLY, HolderInfo, NewToken, RiskReport, Trade, TradeStats
from memebot.report import TokenReport
from memebot.storage import Storage
from memebot.trading.executor import LiveExecutor, PaperExecutor, load_keypair
from memebot.trading.guard import RiskGuard
from memebot.trading.settings import TradingSettings
from memebot.trading.trader import Trader

from .test_engine import RecordingFeed, RecordingNotifier


# ------------------------------------------------------------------ guard
def test_position_sizing_for_tiny_account():
    g = RiskGuard(TradingSettings())
    assert g.size(0.05) == pytest.approx((0.05 - 0.008) * 0.35)
    assert g.size(0.015) == 0.005  # minimum si la réserve reste couverte
    assert g.size(0.010) is None  # trop petit : on ne trade plus
    assert RiskGuard(TradingSettings(max_position_sol=0.1)).size(10) == 0.1


def test_circuit_breakers():
    ts = TradingSettings(max_open_positions=1)
    g = RiskGuard(ts)
    now = 1_800_000_000.0
    g.update_equity(1.0, now)
    assert g.can_open(1.0, 0, now) == (True, "")
    assert not g.can_open(1.0, 1, now)[0]  # déjà une position
    assert "jour" in g.can_open(0.6, 0, now)[1]  # -40 % sur la journée
    g.update_equity(0.6, now + 86400)  # nouveau jour : nouvelle référence
    assert g.can_open(0.6, 0, now + 86400)[0]

    for _ in range(2):
        assert g.record_close(-0.1, now) is None
    assert "pause" in g.record_close(-0.1, now)
    assert not g.can_open(0.6, 0, now + 60)[0]
    assert g.can_open(0.6, 0, now + 3 * 3600)[0]

    assert "perte de 60%" in g.update_equity(0.3, now + 86400)
    assert "arrêté" in g.can_open(0.3, 0, now + 86400)[1]
    g.resume(0.3)
    assert g.can_open(0.3, 0, now + 86400)[0]


async def test_paper_executor_applies_fees():
    ex = PaperExecutor(lambda m: 1e-7, fee_pct=2, tx_fee_sol=0.001, latency=0)
    buy = await ex.buy("M", 0.1)
    assert buy.ok and buy.tokens == pytest.approx((0.1 * 0.98 - 0.001) / 1e-7)
    sell = await ex.sell("M", buy.tokens, 1.0)
    assert sell.sol == pytest.approx(0.097 * 0.98 - 0.001)
    assert not (await PaperExecutor(lambda m: None, fee_pct=1, tx_fee_sol=0, latency=0).buy("M", 1)).ok


# ----------------------------------------------------------------- trader
def report(safety=80, momentum=60, critical=False):
    tok = NewToken(mint="MintPos", name="Nugget", symbol="NUG", creator="Dev",
                   market_cap_sol=30.0)
    return tok, TokenReport(token=tok, risk=RiskReport(safety, [], critical), momentum=momentum)


@pytest.fixture
def trader(tmp_path):
    ts = TradingSettings(paper_latency_seconds=0, fee_pct=0, tx_fee_sol=0)
    storage = Storage(tmp_path / "t.db")
    t = Trader(ts, None, storage, RecordingNotifier(), feed=RecordingFeed())
    t.ex = PaperExecutor(t.price_of, fee_pct=0, tx_fee_sol=0, latency=0)
    yield t
    for task in list(t.tasks):
        task.cancel()


def tick(trader, mcap, *, seller=None):
    trader.on_trade(Trade(mint="MintPos", trader=seller or "someone", is_buy=seller is None,
                          sol_amount=0.1, token_amount=1, new_balance=None, market_cap_sol=mcap))


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_rejections(trader):
    tok, rep = report(safety=50)
    assert "sécurité" in await trader.on_signal(tok, rep, None)
    tok, rep = report(momentum=5)
    assert "momentum" in await trader.on_signal(tok, rep, None)
    tok, rep = report()
    stats = TradeStats.for_token(tok)
    stats.last_mcap = 500
    assert "trop tard" in await trader.on_signal(tok, rep, stats)
    tok.created_at = time.time() - 3600
    assert "trop vieux" in await trader.on_signal(tok, rep, None)
    tok, rep = report()
    tok.initial_buy = 60_000_000  # le dev détient 6 %
    assert "le dev détient 6.0%" in await trader.on_signal(tok, rep, None)
    tok, rep = report()
    rep.holders = HolderInfo(35, 8, "w", 1, 20)
    assert "top 10 holders 35%" in await trader.on_signal(tok, rep, None)
    assert trader.positions == {}


async def test_take_profit_then_trailing_stop(trader):
    tok, rep = report()
    assert await trader.on_signal(tok, rep, None) is None
    pos = trader.positions["MintPos"]
    stake = pos.entry_sol
    assert stake == pytest.approx((0.05 - 0.008) * 0.35)
    assert trader.cash == pytest.approx(0.05 - stake)
    assert "MintPos" in trader.feed.subs and "ACHAT" in trader.notifier.sent[-1][0]

    tick(trader, 60)  # x2 -> vente de la moitié
    await settle()
    assert pos.tp_done and pos.tokens == pytest.approx(pos.entry_sol / (30 / PUMP_TOTAL_SUPPLY) / 2)
    assert "VENTE PARTIELLE" in trader.notifier.sent[-1][0]

    tick(trader, 120)  # x4
    tick(trader, 80)  # -33 % depuis le plus haut -> trailing stop
    await settle()
    assert "MintPos" not in trader.positions
    last = trader.notifier.sent[-1][0]
    assert "trailing stop" in last and "+" in last
    expected_back = stake / 2 * 2 + stake / 2 * (80 / 30)
    assert trader.cash == pytest.approx(0.05 - stake + expected_back)
    stats = trader.storage.trade_stats("paper")
    assert stats["count"] == 1 and stats["wins"] == 1


async def test_stop_loss_and_dev_sell(trader):
    tok, rep = report()
    await trader.on_signal(tok, rep, None)
    tick(trader, 21)  # -30 %
    await settle()
    assert not trader.positions and "stop-loss" in trader.notifier.sent[-1][0]

    await trader.on_signal(tok, rep, None)
    tick(trader, 31, seller="Dev")
    await settle()
    assert not trader.positions and "le dev vend" in trader.notifier.sent[-1][0]
    assert trader.storage.trade_stats("paper")["count"] == 2


async def test_time_stop_and_stale_exit(trader):
    tok, rep = report()
    await trader.on_signal(tok, rep, None)
    pos = trader.positions["MintPos"]
    assert trader.exit_reason(pos) is None
    assert "décollage" in trader.exit_reason(pos, now=time.time() + 16 * 60)[1]
    pos.opened_at = time.time()
    pos.last_trade_at = time.time() - 6 * 60
    assert "aucun échange" in trader.exit_reason(pos)[1]


async def test_one_position_at_a_time_and_tiny_capital(trader):
    tok, rep = report()
    await trader.on_signal(tok, rep, None)
    tok2 = NewToken(mint="Other", name="X", symbol="X", creator="D2", market_cap_sol=30)
    rep2 = TokenReport(token=tok2, risk=RiskReport(90, [], False), momentum=80)
    assert "maximum" in await trader.on_signal(tok2, rep2, None)
    await trader.handle_command("/vendre", "tout")
    await settle()
    trader.cash = 0.009
    assert "arrêté" in await trader.on_signal(tok2, rep2, None)  # -82 % : coupe-circuit
    await trader.handle_command("/trading", "on")
    assert await trader.on_signal(tok2, rep2, None) == "capital trop faible"
    assert "Capital trop faible" in trader.notifier.sent[-1][0]


async def test_state_survives_restart(trader, tmp_path):
    tok, rep = report()
    await trader.on_signal(tok, rep, None)
    t2 = Trader(trader.ts, None, trader.storage, RecordingNotifier())
    assert "MintPos" in t2.positions and t2.cash == pytest.approx(trader.cash)
    assert t2.positions["MintPos"].entry_sol == trader.positions["MintPos"].entry_sol


async def test_trading_commands(trader):
    assert "papier" in await trader.handle_command("/trading", "")
    assert "désactivé" in await trader.handle_command("/trading", "off")
    tok, rep = report()
    assert await trader.on_signal(tok, rep, None) == "trading désactivé"
    assert "activé" in await trader.handle_command("/trading", "on")
    await trader.on_signal(tok, rep, None)
    assert "$NUG" in await trader.handle_command("/positions", "")
    assert "lancée" in await trader.handle_command("/vendre", "$nug")
    await settle()
    assert "NUG" in await trader.handle_command("/journal", "")
    assert await trader.handle_command("/inconnue", "") is None


# ------------------------------------------------------------ mode réel
def test_load_keypair_formats():
    kp = Keypair()
    assert load_keypair(str(kp)).pubkey() == kp.pubkey()
    assert load_keypair(json.dumps(list(bytes(kp)))).pubkey() == kp.pubkey()


def unsigned_tx_for(payer: Pubkey) -> bytes:
    """Ce que renvoie PumpPortal : une transaction non signée."""
    ix = Instruction(Pubkey.default(), b"", [])
    msg = MessageV0.try_compile(payer, [ix], [], Hash.default())
    return bytes(VersionedTransaction.populate(msg, [Signature.default()]))


class FakeChain:
    """Simule PumpPortal + le RPC Solana."""

    def __init__(self, kp: Keypair, payer: Pubkey | None = None):
        self.kp = kp
        self.payer = payer or kp.pubkey()
        self.sol, self.tokens = 0.05, 0.0
        self.sent: list[VersionedTransaction] = []
        self.forms: list[dict] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.host == "pumpportal.fun":
            form = dict(x.split("=") for x in req.content.decode().split("&"))
            self.forms.append(form)
            return httpx.Response(200, content=unsigned_tx_for(self.payer))
        body = json.loads(req.content)
        m, p = body["method"], body["params"]
        if m == "getBalance":
            return ok({"value": int(self.sol * 1e9)})
        if m == "getTokenAccountsByOwner":
            return ok({"value": [{"account": {"data": {"parsed": {"info": {
                "tokenAmount": {"uiAmountString": str(self.tokens)}}}}}}]})
        if m == "sendTransaction":
            tx = VersionedTransaction.from_bytes(base64.b64decode(p[0]))
            assert tx.verify_with_results() == [True]  # bien signée avec notre clé
            self.sent.append(tx)
            form = self.forms[-1]
            if form["action"] == "buy":
                self.sol -= float(form["amount"]) + 0.0003
                self.tokens += 1_000_000
            else:
                self.sol += 0.02
                self.tokens = 0.0
            return ok(str(tx.signatures[0]))
        if m == "getSignatureStatuses":
            return ok({"value": [{"confirmationStatus": "confirmed", "err": None}]})
        return httpx.Response(400)


def ok(result):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


async def test_live_executor_signs_locally_and_measures_fills():
    kp = Keypair()
    chain = FakeChain(kp)
    async with httpx.AsyncClient(transport=httpx.MockTransport(chain.handler)) as http:
        ex = LiveExecutor(http, "https://rpc", kp, slippage_pct=15, priority_fee_sol=0.0003)
        buy = await ex.buy("MintX", 0.0147)
        assert buy.ok, buy.error
        assert buy.tokens == 1_000_000 and buy.sol == pytest.approx(0.015)
        assert chain.forms[0]["denominatedInSol"] == "true" and chain.forms[0]["slippage"] == "15"
        sell = await ex.sell("MintX", buy.tokens, 1.0)
        assert sell.ok and sell.sol == pytest.approx(0.02) and sell.tokens == 1_000_000
        assert chain.forms[1]["amount"] == "100%25"  # « 100% » encodé dans le formulaire
        assert len(chain.sent) == 2


async def test_live_executor_refuses_tx_for_another_wallet():
    kp = Keypair()
    chain = FakeChain(kp, payer=Keypair().pubkey())
    async with httpx.AsyncClient(transport=httpx.MockTransport(chain.handler)) as http:
        ex = LiveExecutor(http, "https://rpc", kp, slippage_pct=15, priority_fee_sol=0.0003)
        fill = await ex.buy("MintX", 0.01)
    assert not fill.ok and "autre wallet" in fill.error and chain.sent == []


def test_close_account_transaction_is_well_formed():
    kp = Keypair()
    ex = LiveExecutor(None, "https://rpc", kp, slippage_pct=1, priority_fee_sol=0)
    mint = str(Keypair().pubkey())
    tx = ex.build_close_account_tx(mint, Pubkey.from_string(
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"), Hash.default())
    assert tx.verify_with_results() == [True]
    ix = tx.message.instructions[0]
    assert bytes(ix.data) == bytes([9]) and len(ix.accounts) == 3


async def test_engine_alert_triggers_paper_trade(tmp_path):
    from memebot.config import Settings
    from memebot.demo import FakeData, make_token
    from memebot.engine import Engine
    from memebot.models import HolderInfo, NewsEvent, TokenMetadata

    from .test_engine import pump

    s = Settings(observe_seconds=0.1, watch_minutes=0.02, db_path=str(tmp_path / "e.db"))
    storage, notifier, feed, data = Storage(tmp_path / "e.db"), RecordingNotifier(), RecordingFeed(), FakeData()
    engine = Engine(s, storage, notifier, data, feed)
    t = Trader(TradingSettings(paper_latency_seconds=0), None, storage, notifier, feed=feed)
    t.ex = PaperExecutor(t.price_of, fee_pct=1.75, tx_fee_sol=0.0003, latency=0)
    t.release = engine.release_mint
    engine.trader = t

    await engine.on_event(NewsEvent(source="manual", source_name="moi", event_id="m1",
                                    text="nugget", weight=1.0))
    tok = make_token("Nugget", "NUGGET", "DevN", 2.0)
    data.holders_by_mint[tok.mint] = HolderInfo(15, 3, "w", 2.0, 20)
    data.meta_by_uri[tok.uri] = TokenMetadata(twitter="https://x.com/n")
    engine.on_new_token(tok)
    mcap = pump(engine, tok, n=20)
    await asyncio.sleep(0.3)
    assert tok.mint in t.positions
    assert any("ACHAT" in m for m, _ in notifier.sent)

    await asyncio.sleep(1.5)  # fin du suivi du radar : l'abonnement doit rester (position ouverte)
    assert tok.mint not in engine.candidates and tok.mint in feed.subs

    engine.on_trade(Trade(mint=tok.mint, trader="DevN", is_buy=False, sol_amount=1,
                          token_amount=1, new_balance=0, market_cap_sol=mcap * 0.8))
    await asyncio.sleep(0.1)
    assert tok.mint not in t.positions
    assert "le dev vend" in notifier.sent[-1][0]
    row = storage.candidate_rows()[0]
    assert row["traded"] == 1 and row["trade_detail"] == "acheté"
    await engine.journal.tick(time.time() + 1801)
    assert tok.mint not in feed.subs
    for task in list(engine.tasks) + list(t.tasks):
        task.cancel()
