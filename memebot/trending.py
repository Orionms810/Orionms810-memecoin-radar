"""Stratégie « tendance » : repérer les tokens qui décollent tout seuls, sans news.

Chaque nouveau token pump.fun est observé pendant ses premières minutes
(« période d'essai »). S'il attire vite beaucoup d'acheteurs différents, avec
de vrais montants, plus d'achats que de ventes et une bonding curve qui avance,
il devient candidat et passe par les mêmes contrôles anti-rug que les autres.

Stratégie séparée des narratifs (alertes 🔥, lignes à part dans /analyse),
activable/désactivable à chaud avec /tendance on|off.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import NewToken, TradeStats

TREND_ID = "TENDANCE"


@dataclass
class TrendSettings:
    enabled: bool = True  # réglage de départ ; /tendance on|off le change à chaud
    window_seconds: float = 180.0  # durée d'observation de chaque nouveau token
    min_age_seconds: float = 30.0  # pas de déclenchement avant (les bundles achètent à la 1re seconde)
    min_buyers: int = 20  # acheteurs différents (hors créateur)
    min_buy_sol: float = 4.0  # SOL achetés au total
    min_progress_pct: float = 10.0  # avancement de la bonding curve
    min_buy_sell_ratio: float = 2.0  # SOL achetés / SOL vendus
    max_checks_per_hour: int = 10  # analyses complètes max par heure (économise le RPC)
    max_probes: int = 60  # tokens observés en même temps (abonnements RPC)
    trade: bool = True  # le trader (papier) peut acheter ces tokens


@dataclass
class Probe:
    token: NewToken
    stats: TradeStats
    until: float


def trend_trigger(stats: TradeStats, now: float, th: TrendSettings) -> str | None:
    """Retourne une description si le token « décolle », sinon None."""
    age = now - stats.created_at
    if age < th.min_age_seconds or age > th.window_seconds:
        return None
    if stats.creator_sold:
        return None
    buyers = len(stats.buyers - {stats.creator})
    progress = stats.curve_progress or 0.0
    if buyers < th.min_buyers or stats.buy_sol < th.min_buy_sol or progress < th.min_progress_pct:
        return None
    if stats.sell_sol > 0 and stats.buy_sol / stats.sell_sol < th.min_buy_sell_ratio:
        return None
    return (f"{buyers} acheteurs et {stats.buy_sol:.1f} SOL en {age:.0f} s · "
            f"courbe {progress:.0f}%")
