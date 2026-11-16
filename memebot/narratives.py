"""Gestion des narratifs actifs et matching avec les nouveaux tokens."""

from __future__ import annotations

import itertools
import time
from collections import Counter, deque
from dataclasses import dataclass
from difflib import SequenceMatcher

from .models import Narrative, NewsEvent
from .text import STOPWORDS, normalize, words


class KeywordBaseline:
    """Fréquence des mots dans les noms des derniers tokens lancés.

    Un mot qui apparaît dans 2 % de tous les lancements (« trump », « pepe »,
    « cat »...) ne prouve pas qu'un token suit le narratif : on le pénalise.
    """

    def __init__(self, size: int = 5000):
        self._window: deque[set[str]] = deque()
        self._size = size
        self._counts: Counter[str] = Counter()

    def add(self, name: str, symbol: str) -> None:
        toks = set(words(name)) | {normalize(name), normalize(symbol)}
        toks.discard("")
        self._window.append(toks)
        self._counts.update(toks)
        if len(self._window) > self._size:
            for t in self._window.popleft():
                self._counts[t] -= 1
                if self._counts[t] <= 0:
                    del self._counts[t]

    def __len__(self) -> int:
        return len(self._window)

    def rarity(self, kw: str) -> float:
        """1.0 = mot rare (bon signal), 0.2 = mot hyper courant."""
        n = len(self._window)
        if n < 200:  # pas assez de données au démarrage
            return 1.0
        freq = self._counts.get(kw, 0) / n
        return max(0.2, min(1.0, 1.0 / (1.0 + freq * 50)))


@dataclass
class MatchResult:
    narrative: Narrative
    score: float
    keywords: list[str]


def keyword_hit(kw: str, name_n: str, sym_n: str, name_words: set[str],
                desc_n: str, desc_words: set[str]) -> float:
    """Force de la correspondance entre un mot-clé et un token (0..1)."""
    if kw == sym_n or kw == name_n:
        return 1.0
    if kw in name_words:
        return 0.9
    if len(kw) >= 4 and (kw in name_n or kw in sym_n):
        return 0.75
    if len(kw) >= 5:
        for target in (name_n, sym_n):
            if len(target) >= 4 and SequenceMatcher(None, kw, target).ratio() >= 0.85:
                return 0.6
    if kw in desc_words or (len(kw) >= 6 and kw in desc_n):
        return 0.4
    return 0.0


def match_token(
    name: str,
    symbol: str,
    description: str,
    narratives: list[Narrative],
    baseline: KeywordBaseline | None = None,
) -> MatchResult | None:
    """Meilleur narratif correspondant à un token, ou None."""
    name_n, sym_n = normalize(name), normalize(symbol)
    if not name_n and not sym_n:
        return None
    name_words = set(words(name))
    desc_n = normalize(description)
    desc_words = set(words(description))

    best: MatchResult | None = None
    for narr in narratives:
        hits: list[tuple[float, str]] = []
        for kw, w in narr.keywords.items():
            s = keyword_hit(kw, name_n, sym_n, name_words, desc_n, desc_words)
            if s <= 0:
                continue
            rarity = baseline.rarity(kw) if baseline else 1.0
            hits.append((s * w * rarity, kw))
        if not hits:
            continue
        hits.sort(reverse=True)
        kw_score = hits[0][0] + 0.15 * min(2, len(hits) - 1)
        score = min(1.0, kw_score) * (0.5 + 0.5 * narr.weight)
        if best is None or score > best.score:
            best = MatchResult(narr, round(score, 3), [k for _, k in hits[:4]])
    return best


class NarrativeBook:
    """Narratifs actifs, avec durée de vie (TTL)."""

    def __init__(self, ttl_seconds: float, generic: set[str] | None = None):
        self.ttl = ttl_seconds
        self.generic = {normalize(g) for g in (generic or set())}
        self._items: dict[str, Narrative] = {}
        self._ids = itertools.count(1)

    def add(self, event: NewsEvent, keywords: dict[str, float],
            now: float | None = None) -> Narrative:
        now = now or time.time()
        narr = Narrative(
            id=f"N{next(self._ids)}",
            event=event,
            keywords=dict(keywords),
            created_at=now,
            expires_at=now + self.ttl,
        )
        self._items[narr.id] = narr
        return narr

    def apply_ai(self, narr: Narrative, *, keywords: list[str], tickers: list[str],
                 hype: int, memeable: bool, summary: str, min_hype: int) -> list[str]:
        """Intègre l'analyse IA. Retourne les nouveaux mots-clés ajoutés."""
        added: list[str] = []
        aliases = {normalize(a) for a in narr.event.aliases}

        def put(kw: str, w: float) -> None:
            k = normalize(kw)
            if len(k) < 3 or k in STOPWORDS:
                return
            if k in self.generic or k in aliases:
                w *= 0.3
            if w > narr.keywords.get(k, 0.0):
                if k not in narr.keywords:
                    added.append(k)
                narr.keywords[k] = w

        for i, kw in enumerate(keywords[:10]):
            put(kw, 1.0 if i < 3 else 0.85)
            parts = words(kw)
            if len(parts) > 1:
                for p in parts:
                    put(p, 0.6)
        for t in tickers[:6]:
            put(t, 1.0)

        narr.hype_score = max(0, min(100, int(hype)))
        narr.memeable = memeable
        narr.summary = summary.strip()
        narr.tickers = [t.upper().lstrip("$") for t in tickers[:6]]
        if narr.event.source != "manual":
            narr.active = memeable and narr.hype_score >= min_hype
        if narr.hype_score >= 70:  # les gros narratifs durent plus longtemps
            narr.expires_at = narr.created_at + self.ttl * 2
        return added

    def get(self, nid: str) -> Narrative | None:
        return self._items.get(nid.upper())

    def remove(self, nid: str) -> bool:
        return self._items.pop(nid.upper(), None) is not None

    def live(self, now: float | None = None) -> list[Narrative]:
        now = now or time.time()
        return [n for n in self._items.values() if n.is_live(now)]

    def all(self) -> list[Narrative]:
        return list(self._items.values())

    def prune(self, now: float | None = None) -> None:
        now = now or time.time()
        for nid in [k for k, n in self._items.items() if n.expires_at < now]:
            del self._items[nid]
