import asyncio
import time

import pytest

from memebot.config import Settings
from memebot.demo import FakeData, FakeFeed, make_token
from memebot.engine import Engine
from memebot.models import HolderInfo, NewsEvent, TokenMetadata, Trade
from memebot.storage import Storage
from memebot.telegram import Notifier


class RecordingNotifier(Notifier):
    def __init__(self):
        super().__init__(console=False)
        self.sent: list[tuple[str, int | None]] = []

    async def send(self, text, *, reply_to=None, force=False):
        if self.muted and not force:
            return None
        self.sent.append((text, reply_to))
        return len(self.sent)


class RecordingFeed(FakeFeed):
    def __init__(self):
        self.subs: set[str] = set()

    async def subscribe_trades(self, mints):
        self.subs.update(mints)

    async def unsubscribe_trades(self, mints):
        self.subs.difference_update(mints)


@pytest.fixture
def setup(tmp_path):
    settings = Settings(observe_seconds=0.2, watch_minutes=0.01, min_momentum=10,
                        db_path=str(tmp_path / "t.db"))
    notifier, feed, data = RecordingNotifier(), RecordingFeed(), FakeData()
    engine = Engine(settings, Storage(tmp_path / "t.db"), notifier, data, feed)
    yield engine, notifier, feed, data
    for t in list(engine.tasks):
        t.cancel()


def trump_event(text="Meet my hamster NUGGET!"):
    return NewsEvent(source="x", source_name="@realDonaldTrump", event_id=f"x:{time.time()}",
                     text=text, weight=1.0, aliases=("trump",))


def pump(engine, token, n=12, sol=0.5):
    mcap = token.market_cap_sol
    for i in range(n):
        mcap += 1
        engine.on_trade(Trade(mint=token.mint, trader=f"buyer{i}", is_buy=True,
                              sol_amount=sol, token_amount=1e6, new_balance=1e6,
                              market_cap_sol=mcap, ts=token.created_at + 5))
    return mcap


async def test_full_flow_alert_then_dev_sells(setup):
    engine, notifier, feed, data = setup
    narr = await engine.on_event(trump_event())
    assert narr and "nugget" in narr.keywords
    assert "Nouveau narratif" in notifier.sent[0][0]

    tok = make_token("Nugget", "NUGGET", "DevGood", 2.0)
    data.holders_by_mint[tok.mint] = HolderInfo(15, 3, "w", 2.0, 20)
    data.meta_by_uri[tok.uri] = TokenMetadata(twitter="https://x.com/n")
    engine.on_new_token(tok)
    await asyncio.sleep(0)
    assert tok.mint in engine.candidates and tok.mint in feed.subs
    mcap = pump(engine, tok)
    await asyncio.sleep(0.4)

    alert = notifier.sent[-1][0]
    assert "TOKEN LIÉ À UN NARRATIF" in alert and "$NUGGET" in alert and tok.mint in alert
    alert_msg_id = len(notifier.sent)
    assert engine.candidates[tok.mint].stage == "watching"

    engine.on_trade(Trade(mint=tok.mint, trader="DevGood", is_buy=False, sol_amount=2,
                          token_amount=2e7, new_balance=0, market_cap_sol=mcap * 0.3))
    await asyncio.sleep(0.05)
    warnings = [t for t, reply in notifier.sent if "ALERTE" in t]
    assert any("créateur vend" in w for w in warnings)
    assert any("Chute de -70%" in w for w in warnings)
    assert all(reply == alert_msg_id for t, reply in notifier.sent if "ALERTE" in t)

    await asyncio.sleep(1.0)  # fin du suivi (0.6 s)
    assert tok.mint not in engine.candidates
    assert tok.mint not in feed.subs  # le journal lit la courbe : plus besoin des trades
    await engine.journal.tick(time.time() + 1801)
    row = engine.storage.candidate_rows()[0]
    assert row["decision"] == "alerte" and row["creator_sold_30"] == 1
    assert row["mcap_30"] == pytest.approx(mcap * 0.3)
    stats = engine.storage.paper_stats()
    assert stats["closed"] == 1 and stats["rugs"] == 1 and stats["median_end_x"] < 1
    check = await engine.handle_command(f"/check {tok.mint}")
    assert "DANGER" in check and "créateur a déjà vendu" in check


async def test_dangerous_and_dead_tokens_are_not_alerted(setup):
    engine, notifier, feed, data = setup
    await engine.on_event(trump_event())
    n_before = len(notifier.sent)

    rug = make_token("NUGGET", "NUGG", "SerialDev", 30.0)
    data.holders_by_mint[rug.mint] = HolderInfo(60, 30, "SerialDev", 30, 20)
    dead = make_token("Nugget Dead", "NUGD", "DevDead", 1.0)
    engine.on_new_token(rug)
    engine.on_new_token(dead)
    pump(engine, rug)
    await asyncio.sleep(0.4)
    assert len(notifier.sent) == n_before
    assert not engine.candidates
    decisions = {r["symbol"]: r["decision"] for r in engine.storage.candidate_rows()}
    assert decisions == {"NUGG": "refus_securite", "NUGD": "refus_momentum"}
    await engine.journal.tick(time.time() + 1801)
    assert not feed.subs  # suivi terminé : plus d'abonnement


async def test_retro_match_tokens_created_before_the_post(setup):
    engine, notifier, feed, data = setup
    tok = make_token("Covfefe", "COVF", "Dev", 1.0)
    engine.on_new_token(tok)
    assert not engine.candidates
    await engine.handle_command("/narratif covfefe")
    assert tok.mint in engine.candidates


async def test_max_alerts_per_narrative(setup):
    engine, notifier, feed, data = setup
    engine.s.max_alerts_per_narrative = 1
    await engine.on_event(trump_event())
    toks = [make_token(f"Nugget {i}", f"NUG{i}", f"Dev{i}", 1.0) for i in range(3)]
    for t in toks:
        engine.on_new_token(t)
        pump(engine, t)
    await asyncio.sleep(0.4)
    alerts = [t for t, _ in notifier.sent if "TOKEN LIÉ" in t]
    assert len(alerts) == 1
    assert "2 autre(s) token(s)" in alerts[0]


async def test_commands(setup):
    engine, notifier, feed, data = setup
    assert "/check" in await engine.handle_command("/aide")
    assert "Usage" in await engine.handle_command("/check pas-une-adresse")
    assert "Aucun narratif" in await engine.handle_command("/narratifs")
    reply = await engine.handle_command("/narratif big burger, $BURG")
    assert "N1" in reply and "bigburger" in reply
    assert "N1" in await engine.handle_command("/narratifs")
    assert "supprimé" in await engine.handle_command("/supprimer n1")
    assert "pause" in await engine.handle_command("/pause")
    assert notifier.muted
    await engine.handle_command("/reprendre")
    assert not notifier.muted
    assert "Aucune alerte clôturée" in await engine.handle_command("/bilan")
    assert "Status" in await engine.handle_command("/status@MonBot")
    assert "inconnue" in await engine.handle_command("/nimportequoi")


async def test_duplicate_events_are_ignored(setup):
    engine, notifier, feed, data = setup
    ev = trump_event()
    assert await engine.on_event(ev) is not None
    assert await engine.on_event(ev) is None


class FakeAI:
    def __init__(self, result):
        self.result = result
        self.reviews = 0

    async def analyze_event(self, event):
        return self.result

    async def review_alert(self, report):
        self.reviews += 1
        assert report["token"]["symbol"]
        return "- dev à surveiller"


async def test_ai_silences_routine_posts(setup):
    NarrativeAnalysis = pytest.importorskip("memebot.ai").NarrativeAnalysis
    engine, notifier, feed, data = setup
    engine.ai = FakeAI(NarrativeAnalysis(memeable=False, hype_score=5, keywords=[], tickers=[],
                                         crypto_direct=False, summary_fr="banal"))
    narr = await engine.on_event(trump_event("Signed the TAXES bill today."))
    await asyncio.sleep(0.05)
    assert notifier.sent == [] and not narr.active


async def test_ai_announces_viral_posts_and_reviews_alerts(setup):
    NarrativeAnalysis = pytest.importorskip("memebot.ai").NarrativeAnalysis
    engine, notifier, feed, data = setup
    engine.ai = FakeAI(NarrativeAnalysis(memeable=True, hype_score=85, keywords=["hamster nugget"],
                                         tickers=["HNUG"], crypto_direct=False, summary_fr="viral"))
    await engine.on_event(trump_event())
    await asyncio.sleep(0.05)
    assert len(notifier.sent) == 1 and "hype 85/100" in notifier.sent[0][0]
    tok = make_token("Hamster Nugget", "HNUG", "Dev", 1.0)
    engine.on_new_token(tok)
    pump(engine, tok)
    await asyncio.sleep(0.4)
    assert engine.ai.reviews == 1
    assert any("Avis IA sur $HNUG" in t for t, _ in notifier.sent)


async def test_ai_failure_falls_back_to_source_weight(setup):
    engine, notifier, feed, data = setup
    engine.ai = FakeAI(None)
    await engine.on_event(trump_event())
    await asyncio.sleep(0.05)
    assert len(notifier.sent) == 1 and "Nouveau narratif" in notifier.sent[0][0]
