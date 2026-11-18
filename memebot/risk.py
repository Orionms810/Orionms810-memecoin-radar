"""Évaluation du risque (rug pull / pump & dump) et du momentum d'un token.

Aucune analyse ne peut garantir qu'un memecoin n'est pas une arnaque : ces
règles repèrent seulement les signaux d'alerte les plus classiques sur pump.fun.
"""

from __future__ import annotations

from dataclasses import dataclass

from .funding import FundingInfo
from .models import (
    CreatorInfo,
    HolderInfo,
    RiskReport,
    RugcheckInfo,
    TokenMetadata,
    TradeStats,
)


@dataclass
class RiskThresholds:
    dev_buy_warn: float = 5.0  # % de la supply achetée par le dev au lancement
    dev_buy_danger: float = 15.0
    top10_warn: float = 30.0  # % détenu par le top 10 (hors bonding curve / pool)
    top10_danger: float = 45.0
    whale_warn: float = 8.0  # % détenu par un seul wallet
    whale_danger: float = 15.0
    snipers_warn: int = 4  # acheteurs dans les 2 premières secondes
    snipers_danger: int = 8
    serial_warn: int = 1  # AUTRES tokens lancés par le même créateur en 24 h
    serial_danger: int = 3
    shared_funder_warn: int = 2  # premiers acheteurs financés par le même wallet
    shared_funder_danger: int = 3
    fresh_ratio_warn: float = 0.7  # part des premiers acheteurs qui sont des wallets neufs
    creator_linked_critical: int = 3  # acheteurs liés au créateur : au-delà, refus direct


def assess_risk(
    *,
    dev_buy_pct: float | None,
    stats: TradeStats | None,
    holders: HolderInfo | None,
    rugcheck: RugcheckInfo | None,
    creator: CreatorInfo | None,
    metadata: TokenMetadata | None,
    th: RiskThresholds | None = None,
    funding: FundingInfo | None = None,
) -> RiskReport:
    """Score de sécurité 0..100 + liste des signaux (ok / warn / danger / critical)."""
    th = th or RiskThresholds()
    flags: list[tuple[str, str]] = []
    penalty = 0
    critical = False

    def flag(level: str, text: str, cost: int = 0) -> None:
        nonlocal penalty
        flags.append((level, text))
        penalty += cost

    # 1. Le dev a-t-il vendu ? C'est LE signal de rug sur pump.fun.
    if stats and stats.creator_sold:
        critical = True
        amount = f" ({stats.creator_sold_sol:.2f} SOL)" if stats.creator_sold_sol > 0 else ""
        flag("critical", f"Le créateur a déjà vendu{amount}", 45)

    # 1 bis. Le dev ne détient presque plus rien alors qu'il avait acheté au lancement
    elif (holders and holders.creator_pct is not None and dev_buy_pct is not None
          and dev_buy_pct >= 1.0 and holders.creator_pct < dev_buy_pct * 0.2):
        flag("danger", f"Le créateur a vendu ou transféré l'essentiel de ses tokens "
                       f"({dev_buy_pct:.1f}% au lancement → {holders.creator_pct:.1f}% aujourd'hui)", 30)

    # 2. Achat initial du dev
    if dev_buy_pct is not None:
        if dev_buy_pct >= th.dev_buy_danger:
            flag("danger", f"Le dev a acheté {dev_buy_pct:.1f}% de la supply au lancement", 25)
        elif dev_buy_pct >= th.dev_buy_warn:
            flag("warn", f"Achat initial du dev élevé : {dev_buy_pct:.1f}%", 10)
        else:
            flag("ok", f"Achat initial du dev faible : {dev_buy_pct:.1f}%")

    # 3. Créateur en série (lance des dizaines de tokens pour rug)
    if creator:
        n24 = creator.launches_24h
        total = creator.launches_total_api
        if n24 >= th.serial_danger or (total is not None and total >= 10):
            detail = f"{n24} autre(s) en 24 h" + (f", {total} au total" if total is not None else "")
            flag("danger", f"Créateur en série ({detail})", 25)
        elif n24 >= th.serial_warn or (total is not None and total >= 3):
            detail = f"{n24} autre(s) en 24 h" + (f", {total} au total" if total is not None else "")
            flag("warn", f"Le créateur a déjà lancé d'autres tokens ({detail})", 10)
        else:
            flag("ok", "Pas d'historique de lancements en série détecté")

    # 4. Concentration des holders
    if holders:
        if holders.top10_pct >= th.top10_danger:
            flag("danger", f"Top 10 holders : {holders.top10_pct:.0f}% de la supply", 25)
        elif holders.top10_pct >= th.top10_warn:
            flag("warn", f"Top 10 holders : {holders.top10_pct:.0f}% de la supply", 10)
        else:
            flag("ok", f"Top 10 holders : {holders.top10_pct:.0f}%")
        if holders.max_holder_pct >= th.whale_danger:
            flag("danger", f"Un seul wallet détient {holders.max_holder_pct:.1f}%", 15)
        elif holders.max_holder_pct >= th.whale_warn:
            flag("warn", f"Un wallet détient {holders.max_holder_pct:.1f}%", 5)

    # 5. Snipers / bundle : beaucoup d'achats dans les toutes premières secondes
    if stats:
        n = len(stats.early_buyers)
        if n >= th.snipers_danger:
            flag("danger", f"{n} wallets ont acheté dans les 2 premières secondes (bundle/snipers probable)", 15)
        elif n >= th.snipers_warn:
            flag("warn", f"{n} achats dans les 2 premières secondes (snipers)", 5)

        if stats.sells >= 5 and stats.sell_sol > stats.buy_sol:
            flag("warn", f"Plus de ventes que d'achats ({stats.sell_sol:.1f} vs {stats.buy_sol:.1f} SOL)", 10)
        if stats.max_mcap > 0 and stats.last_mcap < 0.6 * stats.max_mcap:
            drop = (1 - stats.last_mcap / stats.max_mcap) * 100
            flag("warn", f"Déjà -{drop:.0f}% depuis le plus haut", 10)

    # 5 bis. Faux acheteurs : d'où vient l'argent des premiers acheteurs ?
    if funding and funding.analyzed:
        n, total = funding.linked_to_creator, funding.analyzed
        suspicious = False
        if n:
            suspicious = True
            text = (f"{n} des {total} premiers acheteurs financés par le créateur ou par "
                    "la même source que lui (faux acheteurs)")
            if n >= th.creator_linked_critical:
                critical = True  # arnaque quasi certaine : éliminatoire
                flag("critical", text, 40)
            else:
                flag("danger", text, 30 if n >= 2 else 20)
        if not (n and funding.shared_is_creator_source):
            if funding.max_shared >= th.shared_funder_danger:
                suspicious = True
                flag("danger", f"{funding.max_shared} des {total} premiers acheteurs financés par "
                               "le même wallet (bundle probable)", 25)
            elif funding.max_shared >= th.shared_funder_warn:
                suspicious = True
                flag("warn", f"{funding.max_shared} premiers acheteurs financés par le même wallet", 8)
        if total >= 4 and funding.fresh_ratio >= th.fresh_ratio_warn:
            suspicious = True
            flag("warn", f"{funding.fresh_ratio:.0%} des premiers acheteurs sont des wallets tout neufs", 10)
        if not suspicious:
            flag("ok", f"Premiers acheteurs : financements indépendants ({total} vérifiés)")

    # 6. Réseaux sociaux
    if metadata is not None:
        if metadata.socials:
            flag("ok", "Socials : " + ", ".join(metadata.socials))
        else:
            flag("warn", "Aucun réseau social (twitter/telegram/site)", 8)

    # 7. RugCheck
    if rugcheck:
        if rugcheck.dangers:
            flag("danger", "RugCheck : " + "; ".join(rugcheck.dangers[:3]),
                 min(36, 12 * len(rugcheck.dangers)))
        if rugcheck.warnings:
            flag("warn", "RugCheck : " + "; ".join(rugcheck.warnings[:3]),
                 min(12, 4 * len(rugcheck.warnings)))

    safety = max(0, min(100, 100 - penalty))
    return RiskReport(safety=safety, flags=flags, critical=critical)


def momentum_score(stats: TradeStats | None) -> int:
    """0..100 : est-ce que des acheteurs arrivent vraiment ?"""
    if not stats:
        return 0
    score = 0.0
    score += min(40.0, len(stats.buyers - {stats.creator}) * 2.0)
    score += min(25.0, stats.buy_sol * 2.5)
    if stats.first_mcap > 0 and stats.last_mcap > stats.first_mcap:
        score += min(20.0, (stats.last_mcap / stats.first_mcap - 1) * 20)
    if stats.buy_sol > 1.5 * stats.sell_sol and stats.buy_sol > 0.5:
        score += 15
    return int(min(100, score))
