"""Normalisation de texte et extraction heuristique de mots-clés (sans IA).

L'idée : quand un gros compte poste, les créateurs de tokens reprennent en
quelques secondes les mots les plus « memeables » : tickers ($XXX), hashtags,
mots en MAJUSCULES, noms propres, phrases entre guillemets, mots bizarres.
"""

from __future__ import annotations

import re
import unicodedata

STOPWORDS = {
    # anglais
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can", "had", "her",
    "was", "one", "our", "out", "day", "get", "has", "him", "his", "how", "man", "new",
    "now", "old", "see", "two", "way", "who", "did", "its", "let", "put", "say", "she",
    "too", "use", "that", "with", "have", "this", "will", "your", "from", "they", "know",
    "want", "been", "good", "much", "some", "time", "very", "when", "come", "here", "just",
    "like", "long", "make", "many", "over", "such", "take", "than", "them", "well", "were",
    "what", "into", "more", "only", "then", "there", "these", "their", "about", "would",
    "which", "other", "could", "should", "after", "being", "every", "where", "while",
    "because", "people", "today", "really", "thank", "thanks", "also", "even", "most",
    "never", "ever", "must", "said", "says", "going", "doing", "done", "back", "next",
    "last", "first", "year", "years", "week", "weeks", "great", "total",
    "http", "https", "www", "com", "amp", "via", "rt", "it's", "don't", "i'm",
    # français
    "les", "des", "une", "est", "pas", "que", "qui", "dans", "pour", "par", "sur", "avec",
    "son", "ses", "aux", "mais", "plus", "tout", "tous", "cette", "comme", "fait", "nous",
    "vous", "elle", "ils", "elles", "leur", "leurs", "sont", "ont", "été", "etre", "être",
    "avoir", "aussi", "bien", "encore", "entre", "depuis", "après", "apres", "avant",
}

URL_RE = re.compile(r"https?://\S+")
TICKER_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,11})\b")
HASHTAG_RE = re.compile(r"#(\w{2,40})")
QUOTE_RE = re.compile(r"[\"“”«»]([^\"“”«»]{2,40})[\"“”«»]")
WORD_RE = re.compile(r"[A-Za-zÀ-ÿ0-9']+")
CAMEL_RE = re.compile(r"[A-Z][a-z]+|[A-Z]+(?![a-z])|\d+")


def strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def normalize(s: str) -> str:
    """'Big Beautiful Bill!' -> 'bigbeautifulbill'"""
    return re.sub(r"[^a-z0-9]", "", strip_accents(s).lower())


def words(s: str) -> list[str]:
    """Mots normalisés d'un texte."""
    return [w for w in re.split(r"[^a-z0-9]+", strip_accents(s).lower()) if w]


def acronym(parts: list[str]) -> str:
    return "".join(p[0] for p in parts if p).lower()


def _add(out: dict[str, float], kw: str, weight: float, min_len: int) -> None:
    kw = normalize(kw)
    if len(kw) < min_len or kw in STOPWORDS or kw.isdigit():
        return
    if weight > out.get(kw, 0.0):
        out[kw] = weight


def extract_keywords(
    text: str,
    *,
    generic: set[str] | frozenset[str] = frozenset(),
    aliases: tuple[str, ...] = (),
    min_len: int = 3,
    limit: int = 25,
) -> dict[str, float]:
    """Retourne {mot-clé normalisé: poids 0..1}, les plus probables en premier."""
    text = URL_RE.sub(" ", text)
    out: dict[str, float] = {}

    for t in TICKER_RE.findall(text):
        _add(out, t, 1.0, min_len)

    for tag in HASHTAG_RE.findall(text):
        _add(out, tag, 0.9, min_len)
        parts = CAMEL_RE.findall(tag)
        if len(parts) >= 3:  # #MakeAmericaGreatAgain -> maga
            _add(out, acronym(parts), 0.8, min_len)

    for phrase in QUOTE_RE.findall(text):
        ws = WORD_RE.findall(phrase)
        if 1 <= len(ws) <= 5:
            _add(out, phrase, 0.9, min_len)
            for w in ws:
                _add(out, w, 0.6, min_len)

    # Phrases découpées pour repérer les débuts de phrase (majuscule « normale »).
    for sentence in re.split(r"[.!?\n]+", text):
        tokens = WORD_RE.findall(sentence)
        run: list[str] = []
        for i, w in enumerate(tokens):
            clean = w.strip("'")
            is_caps = len(clean) >= 3 and clean.isupper() and clean.isalpha()
            is_cap = clean[:1].isupper() and not is_caps
            if is_caps:
                _add(out, clean, 0.8, min_len)
            elif is_cap and i > 0:
                _add(out, clean, 0.6, min_len)
            elif len(clean) >= 6 and clean.isalpha():
                # mot long / bizarre : un nom de token potentiel, poids faible
                _add(out, clean, 0.3, min_len)

            if (is_cap or is_caps) and normalize(clean) not in STOPWORDS:
                run.append(clean)
            else:
                _flush_run(out, run, min_len)
                run = []
        _flush_run(out, run, min_len)

    generic_all = {normalize(g) for g in generic} | {normalize(a) for a in aliases}
    for kw in list(out):
        if kw in generic_all:
            out[kw] *= 0.3

    ranked = sorted(out.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    return dict(ranked[:limit])


def _flush_run(out: dict[str, float], run: list[str], min_len: int) -> None:
    """'Big Beautiful Bill' -> bigbeautifulbill (0.7) + bbb (0.5)."""
    if 2 <= len(run) <= 4:
        _add(out, "".join(run), 0.7, min_len)
        if len(run) >= 3:
            _add(out, acronym(run), 0.5, min_len)


def manual_keywords(text: str, min_len: int = 3) -> dict[str, float]:
    """Pour /narratif : l'utilisateur tape exactement ce qu'il cherche."""
    out: dict[str, float] = {}
    for part in re.split(r"[,;]+", text):
        part = part.strip()
        if not part:
            continue
        _add(out, part, 1.0, min_len)
        for w in WORD_RE.findall(part):
            _add(out, w, 0.9, min_len)
    for t in TICKER_RE.findall(text):
        _add(out, t, 1.0, min_len)
    return out
