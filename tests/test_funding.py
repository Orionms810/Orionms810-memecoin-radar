import asyncio
import sqlite3
import time

from memebot.funding import FundingInfo, FundingTracer, WalletOrigin, find_funder, summarize
from memebot.models import NewToken, Trade, TradeStats
from memebot.risk import assess_risk
from memebot.storage import Storage


def transfer(source, dest, inner=False):
    ix = {"program": "system", "parsed": {"type": "transfer",
                                          "info": {"source": source, "destination": dest,
                                                   "lamports": 10_000_000}}}
    if inner:
        return {"transaction": {"message": {"instructions": []}},
                "meta": {"innerInstructions": [{"instructions": [ix]}]}}
    return {"transaction": {"message": {"instructions": [ix]}}, "meta": {}}


def test_find_funder():
    assert find_funder(transfer("F", "W"), "W") == "F"
    assert find_funder(transfer("F", "W", inner=True), "W") == "F"
    assert find_funder(transfer("W", "X"), "W") is None  # le wallet envoie, il ne reçoit pas
    create = {"transaction": {"message": {"instructions": [{"program": "system", "parsed": {
        "type": "createAccount", "info": {"source": "F2", "newAccount": "W"}}}]}}}
    assert find_funder(create, "W") == "F2"
    assert find_funder(None, "W") is None and find_funder({"bad": 1}, "W") is None


class FakeChain:
    """C = créateur financé par F ; B1-B3 financés par F (faux acheteurs) ;
    B4 financé par une plateforme d'échange ; B5 = vieux wallet ; B6 = RPC en erreur."""

    funders = {"C": "F", "B1": "F", "B2": "F", "B3": "F", "B4": "EXCH"}

    def __init__(self):
        self.calls = []

    async def rpc(self, method, params):
        self.calls.append((method, params[0]))
        await asyncio.sleep(0)
        if method == "getSignaturesForAddress":
            wallet = params[0]
            if wallet == "B6":
                return None
            n = 25 if wallet == "B5" else 2
            return [{"signature": f"{wallet}-sig{i}", "blockTime": time.time() - 600}
                    for i in range(n)]
        if method == "getTransaction":
            wallet = params[0].split("-")[0]
            return transfer(self.funders[wallet], wallet)
        if method == "getBalance":
            return {"value": 900_000 * 10**9 if params[0] == "EXCH" else 2 * 10**9}
        raise AssertionError(method)


async def test_tracer_detects_creator_linked_buyers_and_ignores_exchanges():
    chain = FakeChain()
    tracer = FundingTracer(chain.rpc, min_interval=0)
    info = await tracer.analyze(["B1", "B2", "B3", "B4", "B5", "B6", "C", "B1"], creator="C")
    assert info.analyzed == 5  # B6 en erreur, C et le doublon B1 exclus
    assert info.fresh == 4  # B5 est un vieux wallet
    assert info.linked_to_creator == 3 and info.max_shared == 3 and info.shared_funder == "F"
    assert info.shared_is_creator_source
    assert ("getTransaction", "B5-sig24") not in chain.calls  # pas de remontée pour un vieux wallet

    n_calls = len(chain.calls)
    again = await tracer.analyze(["B1", "B2"], creator="C")
    assert again.linked_to_creator == 2
    assert len(chain.calls) == n_calls  # tout vient du cache : aucun appel RPC en plus


async def test_tracer_without_suspicious_links():
    chain = FakeChain()
    chain.funders = {"C": "FC", "B1": "X1", "B2": "X2", "B3": "X3", "B4": "EXCH"}
    info = await FundingTracer(chain.rpc, min_interval=0).analyze(["B1", "B2", "B3", "B4"], creator="C")
    assert info.linked_to_creator == 0 and info.max_shared == 1
    assert await FundingTracer(chain.rpc, min_interval=0).analyze(["C"], creator="C") is None


def test_summarize_ignores_exchange_funders():
    buyers = [WalletOrigin(f"B{i}", True, "EXCH", 60) for i in range(5)]
    info = summarize(buyers, WalletOrigin("C", True, "EXCH", 60), {"EXCH"})
    assert info.max_shared == 0 and info.linked_to_creator == 0 and info.fresh == 5


def flags_for(funding):
    r = assess_risk(dev_buy_pct=None, stats=None, holders=None, rugcheck=None, creator=None,
                    metadata=None, funding=funding)
    return r, [(lvl, txt) for lvl, txt in r.flags]


def test_risk_flags_for_funding():
    r, flags = flags_for(FundingInfo(8, 8, 6, "F", 6, shared_is_creator_source=True))
    assert any(lvl == "critical" and "créateur" in t for lvl, t in flags)
    assert r.critical and r.verdict[1] == "DANGER"  # éliminatoire
    assert not any("bundle" in t for _, t in flags)  # pas compté deux fois
    assert any("wallets tout neufs" in t for _, t in flags) and r.safety == 50

    r, flags = flags_for(FundingInfo(8, 1, 2, "F", 2, shared_is_creator_source=True))
    assert not r.critical and any(lvl == "danger" and "créateur" in t for lvl, t in flags)

    r, flags = flags_for(FundingInfo(8, 2, 4, "F", 0))
    assert any(lvl == "danger" and "bundle" in t for lvl, t in flags) and r.safety == 75

    r, flags = flags_for(FundingInfo(8, 1, 2, "F", 0))
    assert any(lvl == "warn" and "même wallet" in t for lvl, t in flags)

    r, flags = flags_for(FundingInfo(6, 1, 1, "F", 0))
    assert flags == [("ok", "Premiers acheteurs : financements indépendants (6 vérifiés)")]
    assert r.safety == 100


def test_first_buyers_are_ordered_and_exclude_creator():
    tok = NewToken(mint="M", name="N", symbol="S", creator="DEV")
    stats = TradeStats.for_token(tok)
    for trader in ("A", "DEV", "B", "A", "C"):
        stats.apply(Trade(mint="M", trader=trader, is_buy=True, sol_amount=0.1, token_amount=1,
                          new_balance=None, market_cap_sol=30))
    assert stats.first_buyers == ["A", "B", "C"]


def test_storage_migrates_old_candidates_table(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE candidates(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL, "
               "mint TEXT, symbol TEXT)")
    db.execute("INSERT INTO candidates(created_at, mint, symbol) VALUES (1, 'M', 'OLD')")
    db.commit()
    db.close()
    storage = Storage(path)
    cols = {r[1] for r in storage.db.execute("PRAGMA table_info(candidates)")}
    assert {"funding_shared", "funding_creator", "funding_fresh_pct", "funding_checked"} <= cols
    assert storage.candidate_rows()[0]["symbol"] == "OLD"  # rien de perdu
    Storage(path)  # rouvrir une base déjà migrée ne casse rien


async def test_engine_rejects_fake_buyers_and_journals_it(tmp_path):
    from memebot.config import Settings
    from memebot.demo import FakeData, make_token
    from memebot.engine import Engine
    from memebot.models import HolderInfo, NewsEvent

    from .test_engine import RecordingFeed, RecordingNotifier, pump

    s = Settings(observe_seconds=0.1, db_path=str(tmp_path / "e.db"))
    data = FakeData()
    engine = Engine(s, Storage(tmp_path / "e.db"), RecordingNotifier(), data, RecordingFeed())
    await engine.on_event(NewsEvent(source="manual", source_name="moi", event_id="m",
                                    text="nugget", weight=1.0))
    tok = make_token("Nugget", "NUGGET", "DevBundle", 2.0)
    data.holders_by_mint[tok.mint] = HolderInfo(15, 3, "w", 2.0, 20)
    data.funding_by_creator["DevBundle"] = FundingInfo(8, 8, 7, "DevBundle", 7,
                                                       shared_is_creator_source=True)
    engine.on_new_token(tok)
    pump(engine, tok, n=12)
    await asyncio.sleep(0.3)
    row = engine.storage.candidate_rows()[0]
    assert row["decision"] == "refus_securite" and "faux acheteurs" in row["detail"]
    assert row["funding_creator"] == 7 and row["funding_fresh_pct"] == 100
    for task in list(engine.tasks):
        task.cancel()


async def test_rate_limit_spaces_requests():
    stamps = []

    async def rpc(method, params):
        stamps.append(time.monotonic())
        return {"value": 0}

    tracer = FundingTracer(rpc, min_interval=0.05)
    await asyncio.gather(*(tracer.is_exchange(f"F{i}") for i in range(5)))
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(stamps) == 5 and min(gaps) >= 0.045
