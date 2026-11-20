"""Stratégie « tendance » : repérer les tokens qui décollent tout seuls, sans news.

Chaque nouveau token pump.fun est observé pendant ses premières minutes
(« période d'essai ») en lisant sa bonding curve toutes les 15 s, en groupe
(une requête RPC pour 100 tokens, bien moins coûteux que d'écouter chaque
trade). Si de vrais SOL entrent vite et que la courbe avance, il devient
candidat : ses trades sont alors écoutés en direct pendant l'observation, puis
il passe par les mêmes contrôles anti-rug que les autres.

Stratégie séparée des narratifs (alertes 🔥, lignes à part dans /analyse),
activable/désactivable à chaud avec /tendance on|off.
"""

from __future__ import annotations

from dataclasses import dataclass

from .curves import CurveState
from .models import NewToken

TREND_ID = "TENDANCE"


@dataclass
class TrendSettings:
    enabled: bool = True  # réglage de départ ; /tendance on|off le change à chaud
    window_seconds: float = 180.0  # durée d'observation de chaque nouveau token
    min_age_seconds: float = 30.0  # pas de déclenchement avant (les bundles achètent à la 1re seconde)
    min_buy_sol: float = 4.0  # SOL entrés dans la courbe, hors achat du dev
    min_progress_pct: float = 10.0  # avancement de la bonding curve
    poll_seconds: float = 15.0  # lecture groupée des courbes (1 requête RPC pour 100 tokens)
    max_checks_per_hour: int = 6  # analyses complètes max par heure (économise le RPC)
    max_probes: int = 100  # tokens observés en même temps
    trade: bool = True  # le trader (papier) peut acheter ces tokens


@dataclass
class Probe:
    token: NewToken
    until: float


def curve_trigger(token: NewToken, curve: CurveState, now: float, th: TrendSettings) -> str | None:
    """Retourne une description si le token « décolle » (d'après sa courbe), sinon None."""
    age = now - token.created_at
    if age < th.min_age_seconds or age > th.window_seconds or curve.complete:
        return None
    organic = curve.real_sol - token.sol_amount  # sans l'achat initial du dev
    progress = curve.progress_pct
    if organic < th.min_buy_sol or progress < th.min_progress_pct:
        return None
    return f"{organic:.1f} SOL entrés en {age:.0f} s · courbe {progress:.0f}%"
