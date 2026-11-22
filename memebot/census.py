"""Recensement : enregistre TOUS les nouveaux tokens pump.fun et leur évolution,
pour les statistiques (régression, espérance...) qui régleront le bot plus tard.

Pour chaque token : sa bonding curve est relevée à chaque passage (toutes les
15 s) pendant ses 3 premières minutes, puis à 5, 15 et 30 minutes. Chaque relevé
note l'âge, les SOL entrés dans la courbe et la market cap. Ces lectures sont
partagées avec la stratégie tendance (mêmes tokens jeunes, même requête
groupée), donc presque aucun crédit RPC en plus.

Une seule écriture en base par token, quand son suivi est terminé.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any

from .models import NewToken
from .storage import Storage

YOUNG_SECONDS = 180  # relevé à chaque passage avant cet âge
LATER = ((300, "mcap_5m"), (900, "mcap_15m"), (1800, "mcap_30m"))
STALE_SECONDS = 1800 + 300  # sans relevé à 30 min, on clôt avec ce qu'on a


@dataclass
class CensusSettings:
    enabled: bool = True
    sample_pct: float = 100.0  # part des nouveaux tokens recensés
    max_tracked: int = 3000  # tokens suivis en même temps (mémoire)
    keep_days: int = 21  # durée de conservation en base


@dataclass
class Track:
    row: dict[str, Any]
    created_at: float
    path: list[list[float]] = field(default_factory=list)
    max_mcap: float = 0.0
    done: set[str] = field(default_factory=set)
    graduated: bool = False


class TokenCensus:
    def __init__(self, storage: Storage, settings: CensusSettings | None = None,
                 rng: random.Random | None = None):
        self.storage = storage
        self.s = settings or CensusSettings()
        self.rng = rng or random.Random()
        self.tracked: dict[str, Track] = {}  # adresse de la bonding curve -> suivi
        self.recorded = 0

    def add(self, token: NewToken, *, narrative: str = "", creator_24h: int = 0,
            creator_prev: int = 0) -> bool:
        if not (self.s.enabled and token.bonding_curve):
            return False
        if len(self.tracked) >= self.s.max_tracked:
            return False
        if self.s.sample_pct < 100 and self.rng.random() * 100 >= self.s.sample_pct:
            return False
        self.tracked[token.bonding_curve] = Track(
            created_at=token.created_at,
            max_mcap=token.market_cap_sol,
            row={
                "mint": token.mint, "created_at": token.created_at, "symbol": token.symbol,
                "name": token.name, "creator": token.creator,
                "dev_buy_pct": round(token.dev_buy_pct, 3), "dev_sol": round(token.sol_amount, 4),
                "mcap0": round(token.market_cap_sol, 3), "narrative": narrative,
                "creator_24h": creator_24h, "creator_prev": creator_prev,
            })
        return True

    def due(self, now: float | None = None) -> list[str]:
        """Bonding curves à relever à ce passage."""
        now = now or time.time()
        out = []
        for bc, t in self.tracked.items():
            age = now - t.created_at
            if age <= YOUNG_SECONDS:
                out.append(bc)
                continue
            for target, col in LATER:
                if col not in t.done and age >= target:
                    out.append(bc)
                    break
        return out

    def record(self, curves: dict[str, Any], now: float | None = None) -> None:
        now = now or time.time()
        for bc, t in list(self.tracked.items()):
            age = now - t.created_at
            c = curves.get(bc)
            if c is not None:
                mcap = c.market_cap_sol
                due_later = [col for target, col in LATER if col not in t.done and age >= target]
                if age <= YOUNG_SECONDS or due_later:
                    t.path.append([round(age, 1), round(c.real_sol, 4), round(mcap, 3)])
                    t.max_mcap = max(t.max_mcap, mcap)
                for col in due_later:
                    t.row[col] = round(mcap, 3)
                    t.done.add(col)
                if c.complete:  # gradué : la courbe ne bougera plus
                    t.graduated = True
                    for target, col in LATER:
                        if col not in t.done:
                            t.row[col] = round(mcap, 3)
                            t.done.add(col)
            if len(t.done) == len(LATER) or age > STALE_SECONDS:
                self._finish(bc, t)

    def _finish(self, bc: str, t: Track) -> None:
        del self.tracked[bc]
        row = dict(t.row)
        row.update({"path": json.dumps(t.path, separators=(",", ":")),
                    "max_mcap": round(t.max_mcap, 3), "graduated": int(t.graduated),
                    "points": len(t.path)})
        self.storage.census_add(row)
        self.recorded += 1
