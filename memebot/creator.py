"""Historique du créateur : comment ont fini ses anciens tokens ?

Les ruggers recommencent en boucle. Pour chaque ancien token du créateur, on
cherche à savoir :
- s'il a vidé ses parts (il ne détient plus rien du token qu'il avait acheté) ;
- si le token est mort sans décoller ;
- si le token a « gradué » (bonding curve terminée = vrai succès).

Sources, de la plus fiable à la moins fiable :
1. le journal du bot (tokens qu'il a suivis : le dev a-t-il vendu en 30 min ?) ;
2. la blockchain via le RPC (le créateur détient-il encore ses tokens ?) ;
3. l'API (non officielle) de pump.fun (liste des tokens créés, graduation, market cap).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

LAUNCH_MCAP_SOL = 28.0  # market cap de départ d'un token pump.fun
DEAD_MCAP_SOL = 40.0  # n'a jamais vraiment décollé
MIN_AGE_FOR_VERDICT = 3600  # on ne juge pas un token de moins d'une heure


@dataclass
class PastToken:
    mint: str
    exited: bool | None = None  # le créateur a vidé ses parts
    dead: bool | None = None  # mort sans décoller
    graduated: bool | None = None  # bonding curve terminée
    mcap_usd: float | None = None


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def coin_outcome(coin: dict[str, Any], now: float, sol_usd: float | None) -> PastToken | None:
    """Interprète un token de l'API pump.fun (/coins/user-created-coins)."""
    mint = coin.get("mint")
    if not mint:
        return None
    created = _num(coin.get("created_timestamp"))
    created_s = created / 1000 if created and created > 1e12 else created
    complete = coin.get("complete")
    graduated = bool(complete) if complete is not None else None
    usd = _num(coin.get("usd_market_cap"))
    sol = _num(coin.get("market_cap"))
    if sol is None and usd is not None and sol_usd:
        sol = usd / sol_usd
    dead = None
    if graduated is False and created_s and now - created_s > MIN_AGE_FOR_VERDICT and sol is not None:
        dead = sol < DEAD_MCAP_SOL
    return PastToken(mint=str(mint), graduated=graduated, dead=dead, mcap_usd=usd)


def build_history(*, current_mint: str, local_mints: list[str], outcomes: list[Any],
                  api_coins: list[dict[str, Any]] | None, exits: dict[str, bool] | None,
                  sol_usd: float | None, now: float | None = None) -> dict[str, PastToken]:
    """Fusionne les sources en une fiche par ancien token."""
    now = now or time.time()
    past: dict[str, PastToken] = {m: PastToken(m) for m in local_mints if m != current_mint}

    for c in api_coins or []:
        p = coin_outcome(c, now, sol_usd) if isinstance(c, dict) else None
        if p and p.mint != current_mint:
            known = past.setdefault(p.mint, PastToken(p.mint))
            known.graduated = p.graduated if p.graduated is not None else known.graduated
            known.dead = p.dead if p.dead is not None else known.dead
            known.mcap_usd = p.mcap_usd if p.mcap_usd is not None else known.mcap_usd

    for row in outcomes:  # journal du bot : le plus fiable sur la vente du dev
        p = past.get(row["mint"])
        if p is None:
            continue
        if row["creator_sold_30"]:
            p.exited = True
        if row["mcap_30"] is not None and row["mcap_eval"]:
            if row["mcap_30"] < 0.5 * row["mcap_eval"] and (row["max_mcap_30"] or 0) < 2 * row["mcap_eval"]:
                p.dead = True if p.graduated is not True else p.dead

    for mint, exited in (exits or {}).items():
        p = past.get(mint)
        if p is not None and exited and p.exited is None:
            p.exited = True
    return past


def summarize(past: dict[str, PastToken]) -> dict[str, Any]:
    tokens = list(past.values())
    mcaps = [p.mcap_usd for p in tokens if p.mcap_usd is not None]
    return {
        "known_previous": len(tokens),
        # vendre après une graduation réussie = prise de profit, pas un rug
        "dev_exited": sum(1 for p in tokens if p.exited and not p.graduated),
        "dead": sum(1 for p in tokens if p.dead),
        "graduated": sum(1 for p in tokens if p.graduated),
        "best_mcap_usd": max(mcaps) if mcaps else None,
    }
