import time

import httpx

from memebot.journal import (
    ALERT,
    REJECT_MOMENTUM,
    REJECT_SAFETY,
    CandidateJournal,
    format_analysis,
    to_csv,
)
from memebot.models import NewToken, RiskReport, Trade, TradeStats
from memebot.report import TokenReport
from memebot.storage import Storage
from memebot.telegram import Notifier


def token(sym="NUG", mcap=30.0):
    return NewToken(mint=f"Mint{sym}", name=sym, symbol=sym, creator=f"Dev{sym}",
                    initial_buy=20_000_000, market_cap_sol=mcap, created_at=time.time() - 25)


def report(tok, safety=80, momentum=50):
    return TokenReport(token=tok, risk=RiskReport(safety, [], False), momentum=momentum)


def trade(mint, mcap, trader="x", buy=True):
    return Trade(mint=mint, trader=trader, is_buy=buy, sol_amount=1, token_amount=1,
                 new_balance=None, market_cap_sol=mcap)


async def test_checkpoints_and_release(tmp_path):
    storage = Storage(tmp_path / "j.db")
    released = []
    j = CandidateJournal(storage)

    async def release(mint):
        released.append(mint)
    j.release = release

    tok = token()
    stats = TradeStats.for_token(tok)
    stats.last_mcap = 40.0
    t0 = time.time()
    row_id = j.record(token=tok, report=report(tok), stats=stats, decision=ALERT,
                      detail="alerte", narrative_id="N1", source="@trump", match_score=0.9, now=t0)
    assert j.holds(tok.mint)
    j.on_trade(trade(tok.mint, 80))
    j.on_trade(trade(tok.mint, 60))
    await j.tick(t0 + 301)
    row = storage.candidate_rows()[0]
    assert row["mcap_eval"] == 40 and row["mcap_5"] == 60 and row["mcap_15"] is None
    assert row["dev_buy_pct"] == 2.0 and row["age_s"] >= 25

    j.on_trade(trade(tok.mint, 20, trader=tok.creator, buy=False))
    await j.tick(t0 + 1801)
    row = storage.candidate_rows()[0]
    assert row["mcap_15"] == 20 and row["mcap_30"] == 20 and row["max_mcap_30"] == 80
    assert row["creator_sold_30"] == 1 and not j.holds(tok.mint) and released == [tok.mint]

    j.set_trade(row_id, True, "acheté")
    assert storage.candidate_rows()[0]["traded"] == 1


async def test_tracking_limit(tmp_path):
    j = CandidateJournal(Storage(tmp_path / "j.db"), max_tracked=1)
    for sym in ("A", "B"):
        tok = token(sym)
        j.record(token=tok, report=report(tok), stats=None, decision=ALERT, detail="")
    assert list(j.tracked) == ["MintA"]  # B enregistré mais pas suivi
    assert len(j.storage.candidate_rows()) == 2


def make_rows(tmp_path):
    storage = Storage(tmp_path / "a.db")
    j = CandidateJournal(storage)
    t0 = time.time()
    spec = [(ALERT, 85, 70, 60), (ALERT, 75, 40, 15), (REJECT_SAFETY, 20, 50, 3),
            (REJECT_SAFETY, 30, 60, 10), (REJECT_MOMENTUM, 70, 5, 28)]
    for i, (decision, safety, momentum, end) in enumerate(spec):
        tok = token(f"T{i}")
        j.record(token=tok, report=report(tok, safety, momentum), stats=None,
                 decision=decision, detail="", now=t0)
        j.on_trade(trade(tok.mint, end))
    return storage, j, t0


async def test_analysis_and_csv(tmp_path):
    storage, j, t0 = make_rows(tmp_path)
    assert "Pas encore assez" in format_analysis(storage.candidate_rows())
    await j.tick(t0 + 1801)
    text = format_analysis(storage.candidate_rows())
    assert "Alertés 🎯 narratif</b> (2) : x1.25" in text  # médiane de x2.0 et x0.5
    assert "Refusés (sécurité)</b> (2)" in text and "Refusés (momentum)</b> (1)" in text
    assert "sécurité 80-100 : x2.00" in text
    assert "Meilleur au plus haut : $T0" in text

    data = to_csv(storage.candidate_rows())
    lines = data.decode("utf-8-sig").splitlines()
    assert lines[0].startswith("id;created_at;mint") and len(lines) == 6


async def test_export_command_sends_file(tmp_path):
    from memebot.config import Settings
    from memebot.demo import FakeData
    from memebot.engine import Engine

    from .test_engine import RecordingFeed, RecordingNotifier

    class FileNotifier(RecordingNotifier):
        files = []

        async def send_document(self, filename, content, caption=""):
            self.files.append((filename, content))
            return True

    storage, j, t0 = make_rows(tmp_path)
    engine = Engine(Settings(), storage, FileNotifier(), FakeData(), RecordingFeed())
    assert await engine.handle_command("/export") is None
    name, content = FileNotifier.files[0]
    assert name.endswith(".csv") and b"refus_securite" in content
    assert "Analyse des candidats" in await engine.handle_command("/analyse")


async def test_notifier_send_document():
    seen = {}

    def handler(req: httpx.Request):
        seen["path"] = req.url.path
        seen["body"] = req.content
        return httpx.Response(200, json={"ok": True, "result": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        n = Notifier(http, "TOKEN", "123", console=False)
        assert await n.send_document("x.csv", b"a;b\n1;2", "legende")
    assert seen["path"].endswith("/sendDocument")
    assert b'filename="x.csv"' in seen["body"] and b"legende" in seen["body"]
    assert await Notifier(None, None, None, console=False).send_document("x.csv", b"") is False
