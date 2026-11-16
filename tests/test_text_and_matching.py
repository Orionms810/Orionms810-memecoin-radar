import time

from memebot.models import NewsEvent
from memebot.narratives import KeywordBaseline, NarrativeBook, match_token
from memebot.text import extract_keywords, manual_keywords, normalize


def test_normalize():
    assert normalize("Big Beautiful Bill!") == "bigbeautifulbill"
    assert normalize("Élysée Café") == "elyseecafe"
    assert normalize("🚀🚀") == ""


def test_extract_keywords_picks_memeable_words():
    kws = extract_keywords(
        "Met the smartest hamster, his name is NUGGET. Buy $HAMMY now! "
        "#MakeHamstersGreatAgain https://t.co/xyz")
    assert kws["hammy"] == 1.0  # ticker
    assert kws["nugget"] == 0.8  # MAJUSCULES
    assert kws["makehamstersgreatagain"] == 0.9  # hashtag
    assert "mhga" in kws  # acronyme du hashtag
    assert "tco" not in kws and "https" not in kws  # URL ignorée
    assert "the" not in kws and "his" not in kws  # mots vides


def test_extract_keywords_proper_noun_runs_and_quotes():
    kws = extract_keywords('Tonight the Big Beautiful Bill passed. He called it "Freedom Burger".')
    assert "bigbeautifulbill" in kws
    assert "bbb" in kws
    assert kws["freedomburger"] == 0.9


def test_generic_and_aliases_are_downweighted():
    kws = extract_keywords("TRUMP loves COVFEFE", generic={"trump"})
    assert kws["covfefe"] == 0.8
    assert kws["trump"] < 0.3
    kws = extract_keywords("ELON posts DOGE", aliases=("elon",))
    assert kws["elon"] < kws["doge"]


def test_manual_keywords():
    kws = manual_keywords("covfefe, big burger; $COVF")
    assert kws["covfefe"] == 1.0
    assert kws["bigburger"] == 1.0
    assert kws["burger"] == 0.9
    assert kws["covf"] == 1.0


def _book_with(text, weight=1.0, manual=False):
    book = NarrativeBook(ttl_seconds=600)
    ev = NewsEvent(source="manual" if manual else "x", source_name="@test", event_id="e1",
                   text=text, weight=weight)
    kws = manual_keywords(text) if manual else extract_keywords(text)
    return book, book.add(ev, kws)


def test_match_exact_symbol_and_name_word():
    book, narr = _book_with("His name is NUGGET.")
    m = match_token("Nugget the Hamster", "NUGGET", "", book.live())
    assert m and m.narrative is narr and m.score >= 0.8 and "nugget" in m.keywords
    assert match_token("Random Pepe", "RPEPE", "", book.live()) is None


def test_match_substring_fuzzy_and_description():
    book, _ = _book_with("covfefe", manual=True)
    assert match_token("BabyCovfefe", "BCOV", "", book.live()).score >= 0.7  # sous-chaîne
    assert match_token("Covfefee", "CVF", "", book.live()).score >= 0.55  # faute de frappe
    weak = match_token("Some Coin", "SOME", "the covfefe coin", book.live())
    assert weak and weak.score < 0.55  # seulement dans la description : signal faible


def test_low_weight_source_needs_stronger_match():
    book, _ = _book_with("Central bank meets in Frankfurt today.", weight=0.3)
    m = match_token("Frankfurt", "FRANK", "", book.live())
    assert m is None or m.score < 0.55


def test_baseline_penalizes_common_words():
    base = KeywordBaseline()
    for i in range(300):
        base.add("Pepe " + str(i), "PEPE" if i % 2 else f"P{i}")
    assert base.rarity("pepe") < 0.3
    assert base.rarity("covfefe") == 1.0
    book, _ = _book_with("pepe", manual=True)
    with_base = match_token("Pepe", "PEPE", "", book.live(), base).score
    without = match_token("Pepe", "PEPE", "", book.live()).score
    assert with_base < without


def test_baseline_window_forgets_old_tokens():
    base = KeywordBaseline(size=250)
    for i in range(250):
        base.add("Pepe", "PEPE")
    for i in range(250):
        base.add(f"Other {i}", f"O{i}")
    assert base.rarity("pepe") == 1.0


def test_ai_update_can_deactivate_and_extend():
    book, narr = _book_with("routine policy statement about TAXES")
    book.apply_ai(narr, keywords=[], tickers=[], hype=5, memeable=False, summary="rien",
                  min_hype=25)
    assert not narr.active and narr not in book.live()

    book, narr = _book_with("He said COVFEFE")
    added = book.apply_ai(narr, keywords=["covfefe", "late night typo"], tickers=["COVF"],
                          hype=90, memeable=True, summary="viral", min_hype=25)
    assert narr.active and "latenighttypo" in added and "covf" in added
    assert narr.expires_at - narr.created_at == 1200  # TTL doublé pour hype >= 70
    assert narr.weight > 0.9


def test_manual_narrative_stays_active_and_prunes():
    book, narr = _book_with("burger", manual=True)
    book.apply_ai(narr, keywords=[], tickers=[], hype=0, memeable=False, summary="", min_hype=25)
    assert narr.active
    book.prune(now=time.time() + 10_000)
    assert book.live() == [] and book.get(narr.id) is None


def test_example_config_loads(tmp_path):
    from pathlib import Path

    from memebot.config import load_settings

    root = Path(__file__).resolve().parent.parent
    s = load_settings(root / "config.example.toml", tmp_path / "absent.env")
    assert [a["username"] for a in s.x_accounts] == ["realDonaldTrump", "elonmusk", "cz_binance"]
    assert s.rss_feeds[0]["aliases"] == ["trump", "donald"]
    assert s.min_match_score == 0.55 and s.observe_seconds == 20 and s.ipfs_gateway is None
    assert s.risk.serial_warn == 1 and s.trading.mode == "paper"
    assert s.trading.stop_loss_pct == 25 and s.trading.paper_latency_seconds == 2.0
