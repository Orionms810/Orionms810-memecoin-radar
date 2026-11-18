import asyncio
import json
import time

import httpx

from memebot.creator import build_history, coin_outcome, summarize
from memebot.data import LiveData
from memebot.models import CreatorInfo, NewToken
from memebot.risk import assess_risk
from memebot.storage import Storage

NOW = 1_800_000_000.0


def test_coin_outcome():
    old = (NOW - 7200) * 1000  # millisecondes, comme l'API pump.fun
    g = coin_outcome({"mint": "A", "complete": True, "usd_market_cap": 90000,
                      "created_timestamp": old}, NOW, 150)
    assert g.graduated and g.dead is None and g.mcap_usd == 90000
    d = coin_outcome({"mint": "B", "complete": False, "market_cap": 29,
                      "created_timestamp": old}, NOW, 150)
    assert d.dead is True and d.graduated is False
    young = coin_outcome({"mint": "C", "complete": False, "market_cap": 29,
                          "created_timestamp": NOW * 1000 - 60_000}, NOW, 150)
    assert young.dead is None  # moins d'une heure : on ne juge pas
    via_usd = coin_outcome({"mint": "D", "complete": False, "usd_market_cap": 9000,
                            "created_timestamp": old}, NOW, 150)
    assert via_usd.dead is False  # 9000 $ / 150 = 60 SOL : a un peu décollé
    assert coin_outcome({"name": "sans mint"}, NOW, 150) is None


def test_build_history_merges_sources():
    outcomes = [
        {"mint": "L1", "creator_sold_30": 1, "mcap_eval": 40, "mcap_30": 10, "max_mcap_30": 45},
        {"mint": "L2", "creator_sold_30": 0, "mcap_eval": 40, "mcap_30": 120, "max_mcap_30": 200},
    ]
    api = [{"mint": "A1", "complete": True, "usd_market_cap": 120000},
           {"mint": "L2", "complete": False, "market_cap": 120,
            "created_timestamp": (NOW - 7200) * 1000},
           {"mint": "CUR", "complete": False}]
    past = build_history(current_mint="CUR", local_mints=["L1", "L2", "L3", "CUR"],
                         outcomes=outcomes, api_coins=api, exits={"L3": True, "A1": True},
                         sol_usd=150, now=NOW)
    assert set(past) == {"L1", "L2", "L3", "A1"}  # le token actuel est exclu
    assert past["L1"].exited and past["L1"].dead  # journal : dev a vendu, -75 %
    assert past["L2"].dead is False and not past["L2"].exited
    assert past["L3"].exited  # vu par le RPC
    assert past["A1"].exited and past["A1"].graduated  # vendu après un succès : pas un rug
    s = summarize(past)
    assert s == {"known_previous": 4, "dev_exited": 2, "dead": 1, "graduated": 1,
                 "best_mcap_usd": 120000}


def test_exit_after_graduation_is_not_a_rug():
    past = build_history(current_mint="X", local_mints=["G"], outcomes=[],
                         api_coins=[{"mint": "G", "complete": True}], exits={"G": True},
                         sol_usd=150, now=NOW)
    assert summarize(past)["dev_exited"] == 0 and summarize(past)["graduated"] == 1


def risk_for(**kw):
    info = CreatorInfo(launches_24h=0, **kw)
    r = assess_risk(dev_buy_pct=None, stats=None, holders=None, rugcheck=None, creator=info,
                    metadata=None)
    return r, r.flags


def test_risk_rules_for_history():
    r, flags = risk_for(known_previous=3, dev_exited=2)
    assert r.critical and any(lvl == "critical" and "récidiviste" in t for lvl, t in flags)
    r, flags = risk_for(known_previous=1, dev_exited=1)
    assert not r.critical and r.safety == 80
    r, flags = risk_for(known_previous=4, dead=4)
    assert any(lvl == "warn" and "morts sans décoller" in t for lvl, t in flags)
    r, flags = risk_for(known_previous=4, dead=3, graduated=1, best_mcap_usd=250_000)
    assert ("ok", "Le créateur a déjà fait graduer 1 token(s) (meilleur : 250k$)") in flags
    assert not any("morts" in t for _, t in flags) and r.safety == 100


async def test_live_data_creator_exits_and_coins():
    calls = []

    def handler(req: httpx.Request):
        if "pump.fun" in req.url.host:
            return httpx.Response(200, json={"coins": [{"mint": "A"}, "bruit", {"mint": "B"}]})
        body = json.loads(req.content)
        mint = body["params"][1]["mint"]
        calls.append(mint)
        if mint == "ERR":
            return httpx.Response(200, json={"error": {"message": "rate limited"}})
        amount = "0" if mint in ("SOLD", "CLOSED") else "1500000"
        value = [] if mint == "CLOSED" else [{"account": {"data": {"parsed": {"info": {
            "tokenAmount": {"uiAmountString": amount}}}}}}]
        return httpx.Response(200, json={"result": {"value": value}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        data = LiveData(http, "https://rpc")
        exits = await data.creator_exits("DEV", ["SOLD", "HOLD", "CLOSED", "ERR"])
        assert exits == {"SOLD": True, "HOLD": False, "CLOSED": True}  # ERR : pas de conclusion
        await data.creator_exits("DEV", ["SOLD"])
        assert calls.count("SOLD") == 1  # sortie mémorisée : pas de nouvel appel
        assert [c["mint"] for c in await data.creator_coins("DEV")] == ["A", "B"]


def test_storage_creator_queries(tmp_path):
    s = Storage(tmp_path / "c.db")
    for i, mint in enumerate(["OLD", "MID", "CUR"]):
        s.add_token(NewToken(mint=mint, name=mint, symbol=mint, creator="DEV",
                             created_at=time.time() - 1000 + i))
    s.add_token(NewToken(mint="OTHER", name="o", symbol="o", creator="X"))
    assert [r["mint"] for r in s.creator_tokens("DEV", "CUR")] == ["MID", "OLD"]
    s.add_candidate({"mint": "MID", "creator_sold_30": 1, "mcap_eval": 30})
    assert [dict(r)["creator_sold_30"] for r in s.candidate_outcomes(["MID", "OLD"])] == [1]
    assert s.candidate_outcomes([]) == []


async def test_engine_rejects_serial_rugger(tmp_path):
    from memebot.config import Settings
    from memebot.demo import FakeData, make_token
    from memebot.engine import Engine
    from memebot.models import HolderInfo, NewsEvent

    from .test_engine import RecordingFeed, RecordingNotifier, pump

    s = Settings(observe_seconds=0.1, db_path=str(tmp_path / "e.db"))
    data = FakeData()
    engine = Engine(s, Storage(tmp_path / "e.db"), RecordingNotifier(), data, RecordingFeed())
    for i in range(2):  # 2 anciens tokens, lancés il y a 2 jours, vidés par le dev
        old = make_token(f"Old {i}", f"OLD{i}", "Rugger", 5.0)
        old.created_at = time.time() - 2 * 86400
        engine.storage.add_token(old)
        data.exited.add(("Rugger", old.mint))
    await engine.on_event(NewsEvent(source="manual", source_name="moi", event_id="m",
                                    text="nugget", weight=1.0))
    tok = make_token("Nugget", "NUGGET", "Rugger", 2.0)
    data.holders_by_mint[tok.mint] = HolderInfo(15, 3, "w", 2.0, 20)
    engine.on_new_token(tok)
    pump(engine, tok, n=12)
    await asyncio.sleep(0.3)
    row = engine.storage.candidate_rows()[0]
    assert row["decision"] == "refus_securite" and "récidiviste" in row["detail"]
    assert row["creator"] == "Rugger" and row["creator_prev"] == 2
    assert row["creator_exited_prev"] == 2
    for task in list(engine.tasks):
        task.cancel()
