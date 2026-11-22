import json
import random
import time

import pytest

from memebot.census import CensusSettings, TokenCensus
from memebot.curves import CurveState
from memebot.models import NewToken, Trade, TradeStats
from memebot.storage import Storage

T0 = 1_800_000_000.0


def curve(mcap_lamports=60_000_000_000, real_sol=5.0, complete=False):
    return CurveState(v_token_raw=10**15, v_sol_lamports=mcap_lamports,
                      real_sol_lamports=int(real_sol * 1e9), complete=complete)


def tok(sym="CAT", bc="BC1"):
    return NewToken(mint=f"Mint{sym}", name=sym, symbol=sym, creator="Dev", bonding_curve=bc,
                    initial_buy=30_000_000, sol_amount=1.0, market_cap_sol=29.0, created_at=T0)


def test_census_follows_a_token_and_writes_one_row(tmp_path):
    storage = Storage(tmp_path / "c.db")
    c = TokenCensus(storage)
    assert c.add(tok(), narrative="N3", creator_24h=2, creator_prev=5)
    assert not c.add(NewToken(mint="X", name="x", symbol="x", creator="d"))  # sans courbe
    assert c.due(T0 + 15) == ["BC1"]  # jeune : relevé à chaque passage
    for age, mcap in ((15, 40), (30, 70), (45, 55)):
        c.record({"BC1": curve(mcap * 10**9)}, T0 + age)
    assert c.due(T0 + 200) == []  # entre 3 min et 5 min : rien à relever
    assert c.due(T0 + 301) == ["BC1"]
    c.record({"BC1": curve(30 * 10**9)}, T0 + 301)
    c.record({"BC1": curve(20 * 10**9)}, T0 + 901)
    assert storage.census_count() == 0  # une seule écriture, à la fin
    c.record({"BC1": curve(10 * 10**9)}, T0 + 1801)
    rows = storage.census_rows()
    assert len(rows) == 1 and not c.tracked
    r = rows[0]
    assert (r["mcap_5m"], r["mcap_15m"], r["mcap_30m"]) == (30, 20, 10)
    assert r["max_mcap"] == 70 and r["graduated"] == 0 and r["points"] == 6
    assert json.loads(r["path"])[1] == [30, 5.0, 70]  # [âge, SOL entrés, market cap]
    assert r["narrative"] == "N3" and r["creator_24h"] == 2 and r["dev_buy_pct"] == 3.0


def test_graduation_stale_sampling_and_limits(tmp_path):
    storage = Storage(tmp_path / "c.db")
    c = TokenCensus(storage)
    c.add(tok("GRAD", "BCG"))
    c.add(tok("LOST", "BCL"))
    c.record({"BCG": curve(400 * 10**9, complete=True)}, T0 + 60)
    row = storage.census_rows()[0]
    assert row["symbol"] == "GRAD" and row["graduated"] == 1 and row["mcap_30m"] == 400
    c.record({}, T0 + 2200)  # plus jamais de réponse RPC : clos quand même
    assert storage.census_count() == 2 and not c.tracked

    none = TokenCensus(storage, CensusSettings(sample_pct=0), rng=random.Random(1))
    assert not none.add(tok("S", "BCS"))
    full = TokenCensus(storage, CensusSettings(max_tracked=1))
    assert full.add(tok("A", "B1")) and not full.add(tok("B", "B2"))
    off = TokenCensus(storage, CensusSettings(enabled=False))
    assert not off.add(tok("C", "B3"))


def test_flow_features():
    stats = TradeStats(created_at=0, creator="Dev")
    assert stats.flow_features()["flow_trades"] == 0
    seq = [(1, "a", True, 0.05), (2, "b", True, 0.5), (3, "c", True, 0.05), (4, "a", False, 0.3),
           (8, "d", True, 1.0), (9, "e", True, 2.0), (10, "b", False, 0.8)]
    for ts, w, is_buy, sol in seq:
        stats.apply(Trade(mint="M", trader=w, is_buy=is_buy, sol_amount=sol, token_amount=1,
                          new_balance=None, market_cap_sol=30, ts=ts))
    f = stats.flow_features()
    assert f["flow_trades"] == 7 and f["flow_max_sell"] == 0.8
    assert f["flow_avg_buy"] == pytest.approx(3.6 / 5, abs=1e-3)
    assert f["flow_small_pct"] == 40.0  # 2 achats sur 5 sous 0,1 SOL
    assert f["flow_accel"] == pytest.approx(3.0 / 0.6, abs=1e-3)  # les achats accélèrent
    assert f["flow_early_sellers"] == 2 and f["flow_roundtrip"] == 2  # a et b revendent


async def test_engine_census_status_and_export(tmp_path):
    from memebot.config import Settings
    from memebot.demo import FakeData, make_token
    from memebot.engine import Engine

    from .test_engine import RecordingFeed, RecordingNotifier

    class FileNotifier(RecordingNotifier):
        files = []

        async def send_document(self, filename, content, caption=""):
            self.files.append((filename, content))
            return True

    data = FakeData()
    engine = Engine(Settings(db_path=str(tmp_path / "e.db")), Storage(tmp_path / "e.db"),
                    FileNotifier(), data, RecordingFeed())
    engine.set_trend(False)  # le recensement marche même sans la tendance
    t = make_token("Moon", "MOON", "DevM", 2.0)
    t.created_at = time.time()
    data.curves_by_address[t.bonding_curve] = curve(50 * 10**9)
    engine.on_new_token(t)
    assert t.bonding_curve in engine.census.tracked
    await engine.poll_curves(t.created_at + 15)
    await engine.poll_curves(t.created_at + 1801)
    assert engine.storage.census_count() == 1
    assert "Recensement : 1 tokens enregistrés" in await engine.handle_command("/status")
    assert "/export recensement" in await engine.handle_command("/aide")
    assert await engine.handle_command("/export recensement 1") is None
    name, content = FileNotifier.files[-1]
    assert name.startswith("recensement-") and b"MOON" in content and b"path" in content
    for task in list(engine.tasks):
        task.cancel()
