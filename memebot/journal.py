"""Journal complet des candidats : chaque token repéré (alerté OU refusé) est
enregistré avec ses mesures au moment de la décision, puis son prix est suivi
5, 15 et 30 minutes après. Ça permet de vérifier avec des chiffres si les
filtres écartent les bons tokens et si les seuils sont bien réglés."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import statistics
import time
from dataclasses import dataclass, field
from html import escape
from typing import Any, Awaitable, Callable

from .models import NewToken, TradeStats
from .storage import Storage

log = logging.getLogger(__name__)

CHECKPOINTS = (("mcap_5", 300), ("mcap_15", 900), ("mcap_30", 1800))

# Décisions possibles (colonne « decision »)
ALERT = "alerte"
REJECT_SAFETY = "refus_securite"
REJECT_MOMENTUM = "refus_momentum"
REJECT_LIMIT = "refus_limite"
LABELS = {
    ALERT: "Alertés",
    REJECT_SAFETY: "Refusés (sécurité)",
    REJECT_MOMENTUM: "Refusés (momentum)",
    REJECT_LIMIT: "Refusés (trop d'alertes sur le narratif)",
}


@dataclass
class Tracked:
    row_id: int
    mint: str
    creator: str
    t0: float
    last_mcap: float
    max_mcap: float
    creator_sold: bool = False
    bonding_curve: str = ""
    done: set[str] = field(default_factory=set)


class CandidateJournal:
    def __init__(self, storage: Storage, *, max_tracked: int = 150,
                 fetch_curves: Callable[[list[str]], Awaitable[dict[str, Any]]] | None = None):
        self.storage = storage
        self.max_tracked = max_tracked
        self.tracked: dict[str, Tracked] = {}
        # Le prix est lu sur la bonding curve (lecture groupée), sans écouter les trades.
        self.fetch_curves = fetch_curves

    def holds(self, mint: str) -> bool:
        return mint in self.tracked

    # ------------------------------------------------------- enregistrement
    def record(self, *, token: NewToken, report: Any, stats: TradeStats | None,
               decision: str, detail: str, narrative_id: str = "", source: str = "",
               match_score: float = 0.0, now: float | None = None) -> int:
        now = now or time.time()
        mcap = (stats.last_mcap if stats and stats.last_mcap else token.market_cap_sol) or 0.0
        holders = getattr(report, "holders", None)
        funding = getattr(report, "funding", None)
        creator = getattr(report, "creator", None)
        row = {
            "created_at": now,
            "mint": token.mint,
            "symbol": token.symbol,
            "name": token.name,
            "narrative": narrative_id,
            "source": source,
            "match_score": round(match_score, 3),
            "safety": report.risk.safety,
            "momentum": report.momentum,
            "dev_buy_pct": round(token.dev_buy_pct, 2),
            "top10_pct": round(holders.top10_pct, 1) if holders else None,
            "early_buyers": len(stats.early_buyers) if stats else None,
            "buyers": len(stats.buyers) if stats else None,
            "buy_sol": round(stats.buy_sol, 3) if stats else None,
            "sell_sol": round(stats.sell_sol, 3) if stats else None,
            "age_s": round(now - token.created_at, 1),
            "decision": decision,
            "detail": detail[:300],
            "mcap_eval": mcap,
            "max_mcap_30": mcap,
            "creator_sold_30": int(bool(stats and stats.creator_sold)),
            "funding_checked": funding.analyzed if funding else None,
            "funding_shared": funding.max_shared if funding else None,
            "funding_creator": funding.linked_to_creator if funding else None,
            "funding_fresh_pct": round(funding.fresh_ratio * 100) if funding else None,
            "creator": token.creator,
            "creator_prev": creator.known_previous if creator else None,
            "creator_exited_prev": creator.dev_exited if creator else None,
            "creator_dead_prev": creator.dead if creator else None,
            "creator_graduated_prev": creator.graduated if creator else None,
        }
        if stats is not None:
            row.update(stats.flow_features())
        row_id = self.storage.add_candidate(row)
        if mcap > 0 and len(self.tracked) < self.max_tracked:
            self.tracked[token.mint] = Tracked(row_id=row_id, mint=token.mint, creator=token.creator,
                                               t0=now, last_mcap=mcap, max_mcap=mcap,
                                               creator_sold=bool(stats and stats.creator_sold),
                                               bonding_curve=token.bonding_curve)
        return row_id

    def set_trade(self, row_id: int, bought: bool, reason: str) -> None:
        self.storage.update_candidate(row_id, {"traded": int(bought), "trade_detail": reason[:200]})

    # ----------------------------------------------------------------- suivi
    def on_trade(self, trade: Any) -> None:
        t = self.tracked.get(trade.mint)
        if not t:
            return
        if trade.market_cap_sol > 0:
            t.last_mcap = trade.market_cap_sol
            t.max_mcap = max(t.max_mcap, trade.market_cap_sol)
        if not trade.is_buy and trade.trader == t.creator:
            t.creator_sold = True

    async def _refresh_prices(self) -> None:
        if not self.fetch_curves:
            return
        tracked = [t for t in self.tracked.values() if t.bonding_curve]
        if not tracked:
            return
        curves = await self.fetch_curves([t.bonding_curve for t in tracked])
        for t in tracked:
            c = curves.get(t.bonding_curve)
            if c is not None and c.market_cap_sol > 0:
                t.last_mcap = c.market_cap_sol
                t.max_mcap = max(t.max_mcap, c.market_cap_sol)

    async def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        await self._refresh_prices()
        for t in list(self.tracked.values()):
            updates: dict[str, Any] = {}
            for col, delay in CHECKPOINTS:
                if col not in t.done and now - t.t0 >= delay:
                    updates[col] = t.last_mcap
                    t.done.add(col)
            if updates:
                updates["max_mcap_30"] = t.max_mcap
                updates["creator_sold_30"] = int(t.creator_sold)
                self.storage.update_candidate(t.row_id, updates)
            if len(t.done) == len(CHECKPOINTS):
                del self.tracked[t.mint]

    async def run(self, every: float = 60.0) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - le journal ne doit jamais arrêter le bot
                log.exception("Journal : erreur de suivi")
            await asyncio.sleep(every)


# ------------------------------------------------------------------ analyse
def _x(row: Any, col: str) -> float | None:
    base, val = row["mcap_eval"], row[col]
    if not base or val is None:
        return None
    return val / base


def _group_line(label: str, rows: list[Any]) -> str | None:
    x30 = [v for r in rows if (v := _x(r, "mcap_30")) is not None]
    if not x30:
        return None
    xmax = [v for r in rows if (v := _x(r, "max_mcap_30")) is not None]
    up = sum(1 for v in x30 if v > 1.0)
    rugs = sum(1 for r in rows if r["creator_sold_30"])
    return (f"<b>{escape(label)}</b> ({len(x30)}) : x{statistics.median(x30):.2f} à 30 min "
            f"(médiane) · en hausse {up * 100 // len(x30)}% · plus haut médian "
            f"x{statistics.median(xmax):.2f} · dev a vendu {rugs * 100 // len(rows)}%")


def _bucket_lines(rows: list[Any], col: str, edges: list[int], name: str) -> list[str]:
    lines = []
    bounds = list(zip(edges, edges[1:] + [101]))
    for lo, hi in bounds:
        sub = [r for r in rows if r[col] is not None and lo <= r[col] < hi]
        x30 = [v for r in sub if (v := _x(r, "mcap_30")) is not None]
        if x30:
            lines.append(f"  {name} {lo}-{hi - 1} : x{statistics.median(x30):.2f} "
                         f"(n={len(x30)}, en hausse {sum(v > 1 for v in x30) * 100 // len(x30)}%)")
    return lines


def format_analysis(rows: list[Any]) -> str:
    done = [r for r in rows if r["mcap_30"] is not None and r["mcap_eval"]]
    pending = len(rows) - len(done)
    if not done:
        return (f"📊 <b>Analyse des candidats</b>\nPas encore assez de données : {len(rows)} "
                f"candidat(s) enregistré(s), il faut 30 min de suivi par token. Reviens plus tard.")
    lines = [f"📊 <b>Analyse des candidats</b> — {len(done)} suivis 30 min "
             f"({pending} en cours)",
             "<i>« x1.00 » = prix inchangé 30 min après la décision, « x0.50 » = -50 %.</i>", ""]
    def kind(r: Any) -> str:
        return {"TENDANCE": "🔥 tendance", "CREATEUR": "🏆 créateur"}.get(r["narrative"], "🎯 narratif")

    kinds = ("🎯 narratif", "🔥 tendance", "🏆 créateur")
    for decision, label in LABELS.items():
        rows_d = [r for r in done if r["decision"] == decision]
        if decision == ALERT:
            for k in kinds:
                line = _group_line(f"Alertés {k}", [r for r in rows_d if kind(r) == k])
                if line:
                    lines.append(line)
            continue
        line = _group_line(label, rows_d)
        if line:
            lines.append(line)
    traded = [r for r in done if r["traded"]]
    for name, sub in ((f"Achetés {k}", [r for r in traded if kind(r) == k]) for k in kinds):
        line = _group_line(name, sub)
        if line:
            lines.append(line)
    lines.append("")
    lines.append("<b>Par score de sécurité</b>")
    lines += _bucket_lines(done, "safety", [0, 40, 60, 80], "sécurité")
    lines.append("<b>Par momentum</b>")
    lines += _bucket_lines(done, "momentum", [0, 10, 30, 60], "momentum")
    best = max(done, key=lambda r: _x(r, "max_mcap_30") or 0)
    lines.append("")
    lines.append(f"Meilleur au plus haut : ${escape(best['symbol'])} "
                 f"x{_x(best, 'max_mcap_30'):.1f} ({LABELS.get(best['decision'], best['decision'])})")
    lines.append("<i>Il faut au moins 50-100 tokens par groupe pour tirer des conclusions.</i>")
    return "\n".join(line for line in lines if line is not None)


def to_csv(rows: list[Any]) -> bytes:
    buf = io.StringIO()
    if rows:
        writer = csv.writer(buf, delimiter=";")
        writer.writerow(rows[0].keys())
        for r in rows:
            writer.writerow(["" if v is None else v for v in r])
    return buf.getvalue().encode("utf-8-sig")  # utf-8-sig : Excel lit bien les accents
