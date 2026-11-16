"""Mise en forme des messages (HTML Telegram)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from html import escape
from typing import Any

from .models import (
    CreatorInfo,
    DexInfo,
    HolderInfo,
    Narrative,
    NewToken,
    RiskReport,
    RugcheckInfo,
    TokenMetadata,
    TradeStats,
)
from .narratives import MatchResult

ICONS = {"ok": "✅", "warn": "⚠️", "danger": "⛔", "critical": "🚨"}
DISCLAIMER = "<i>Pas un conseil financier. La majorité des tokens pump.fun finissent à zéro : ne mise que ce que tu peux perdre.</i>"


@dataclass
class TokenReport:
    token: NewToken
    risk: RiskReport
    momentum: int = 0
    match: MatchResult | None = None
    stats: TradeStats | None = None
    metadata: TokenMetadata | None = None
    holders: HolderInfo | None = None
    rugcheck: RugcheckInfo | None = None
    creator: CreatorInfo | None = None
    dex: DexInfo | None = None
    sol_usd: float | None = None
    copies: int = 0  # autres tokens qui copient le même narratif
    rank: int = 1  # rang par volume parmi les copies
    extra_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Version compacte pour l'avis de l'IA."""
        t, s = self.token, self.stats
        d: dict[str, Any] = {
            "token": {"name": t.name, "symbol": t.symbol, "age_seconds": int(time.time() - t.created_at),
                      "dev_buy_pct": round(t.dev_buy_pct, 2)},
            "safety_score": self.risk.safety,
            "verdict": self.risk.verdict[1],
            "momentum_score": self.momentum,
            "flags": [f"{lvl}: {txt}" for lvl, txt in self.risk.flags],
            "copies_of_same_narrative": self.copies,
            "rank_by_volume_among_copies": self.rank,
        }
        if self.match:
            n = self.match.narrative
            d["narrative"] = {"source": n.event.source_name, "text": n.event.text[:500],
                              "hype_score": n.hype_score, "matched_keywords": self.match.keywords,
                              "match_score": self.match.score}
        if s:
            d["trading"] = {"buys": s.buys, "sells": s.sells, "unique_buyers": len(s.buyers),
                            "buy_sol": round(s.buy_sol, 2), "sell_sol": round(s.sell_sol, 2),
                            "market_cap_sol_first": round(s.first_mcap, 1),
                            "market_cap_sol_now": round(s.last_mcap, 1),
                            "market_cap_sol_max": round(s.max_mcap, 1),
                            "creator_sold": s.creator_sold,
                            "bonding_curve_progress_pct": s.curve_progress}
        if self.metadata:
            d["description"] = self.metadata.description[:300]
            d["socials"] = self.metadata.socials
        return d


def fmt_usd(v: float | None) -> str:
    if v is None:
        return "?"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v / 1_000:.1f}k"
    return f"${v:.0f}"


def fmt_mcap(sol: float, sol_usd: float | None) -> str:
    usd = f" (~{fmt_usd(sol * sol_usd)})" if sol_usd else ""
    return f"{sol:.1f} SOL{usd}"


def fmt_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} j"


def links(mint: str) -> str:
    return (f'🔗 <a href="https://pump.fun/coin/{mint}">pump.fun</a> · '
            f'<a href="https://dexscreener.com/solana/{mint}">DexScreener</a> · '
            f'<a href="https://gmgn.ai/sol/token/{mint}">GMGN</a> · '
            f'<a href="https://rugcheck.xyz/tokens/{mint}">RugCheck</a> · '
            f'<a href="https://solscan.io/token/{mint}">Solscan</a>')


def short(text: str, n: int = 180) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _risk_lines(risk: RiskReport) -> list[str]:
    order = {"critical": 0, "danger": 1, "warn": 2, "ok": 3}
    return [f"{ICONS.get(lvl, '•')} {escape(txt)}"
            for lvl, txt in sorted(risk.flags, key=lambda f: order.get(f[0], 9))]


def format_alert(r: TokenReport, *, observe_seconds: float) -> str:
    t, s = r.token, r.stats
    emoji, verdict = r.risk.verdict
    lines = [f"🎯 <b>TOKEN LIÉ À UN NARRATIF</b> — {emoji} <b>{verdict}</b> "
             f"(sécurité {r.risk.safety}/100)", ""]
    lines.append(f"<b>{escape(t.name)}</b> (${escape(t.symbol)})")
    lines.append(f"<code>{t.mint}</code>")
    lines.append("")
    if r.match:
        n = r.match.narrative
        lines.append(f"📰 Narratif {n.id} : {escape(n.event.source_name)} — "
                     f"« {escape(short(n.event.text, 160))} »")
        kws = ", ".join(r.match.keywords)
        lines.append(f"🔑 Mots-clés : {escape(kws)} (match {r.match.score:.2f})")
        if n.hype_score is not None:
            lines.append(f"🧠 Hype IA : {n.hype_score}/100")
    lines.append("")
    mcap = s.last_mcap if s else t.market_cap_sol
    curve = f" · courbe {s.curve_progress:.0f}%" if s and s.curve_progress is not None else ""
    lines.append(f"💰 Market cap : {fmt_mcap(mcap, r.sol_usd)}{curve}")
    copies = (f" · {r.copies} autre(s) token(s) sur ce narratif, celui-ci #{r.rank} en volume"
              if r.copies else " · seul token détecté sur ce narratif")
    lines.append(f"⏱ Âge : {fmt_age(time.time() - t.created_at)}{copies}")
    if s:
        lines.append(f"📈 Flux ({observe_seconds:.0f} s observées) : {s.buys} achats / {s.sells} ventes · "
                     f"{len(s.buyers)} acheteurs uniques · +{s.buy_sol:.2f} / -{s.sell_sol:.2f} SOL")
    lines.append(f"⚡ Momentum : {r.momentum}/100")
    lines.append("")
    lines.append("🛡 <b>Contrôles anti-rug</b>")
    lines.extend(_risk_lines(r.risk))
    if r.metadata and r.metadata.description:
        lines.append("")
        lines.append(f"📝 {escape(short(r.metadata.description, 200))}")
    lines.append("")
    lines.append(links(t.mint))
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_check(r: TokenReport) -> str:
    t = r.token
    emoji, verdict = r.risk.verdict
    lines = [f"🔎 <b>CHECK</b> — {emoji} <b>{verdict}</b> (sécurité {r.risk.safety}/100)", ""]
    name = f"<b>{escape(t.name)}</b> (${escape(t.symbol)})" if t.name else "<b>Token</b>"
    lines.append(name)
    lines.append(f"<code>{t.mint}</code>")
    if t.created_at:
        lines.append(f"⏱ Âge : {fmt_age(time.time() - t.created_at)}")
    if r.dex:
        d = r.dex
        lines.append(f"💰 Market cap : {fmt_usd(d.market_cap_usd)} · liquidité {fmt_usd(d.liquidity_usd)} "
                     f"({escape(d.dex)})")
        lines.append(f"📊 Volume 1 h : {fmt_usd(d.volume_h1)} · 24 h : {fmt_usd(d.volume_h24)}")
        if d.change_h1 is not None or d.change_h24 is not None:
            lines.append(f"📈 Variation 1 h : {d.change_h1 if d.change_h1 is not None else '?'}% · "
                         f"24 h : {d.change_h24 if d.change_h24 is not None else '?'}%")
        if d.buys_h1 is not None:
            lines.append(f"🔁 Transactions 1 h : {d.buys_h1} achats / {d.sells_h1} ventes")
    elif t.market_cap_sol:
        lines.append(f"💰 Market cap au lancement : {fmt_mcap(t.market_cap_sol, r.sol_usd)}")
    if r.holders and r.holders.creator_pct is not None:
        lines.append(f"👤 Le créateur détient encore {r.holders.creator_pct:.1f}%")
    if r.rugcheck and r.rugcheck.score_normalised is not None:
        lines.append(f"🧪 Score RugCheck : {r.rugcheck.score_normalised} (plus bas = mieux)")
    lines.append("")
    lines.append("🛡 <b>Contrôles anti-rug</b>")
    lines.extend(_risk_lines(r.risk))
    for extra in r.extra_flags:
        lines.append(f"ℹ️ {escape(extra)}")
    lines.append("")
    lines.append(links(t.mint))
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def format_narrative(n: Narrative) -> str:
    kws = ", ".join(list(n.keywords)[:8])
    lines = [f"📡 <b>Nouveau narratif {n.id}</b> — {escape(n.event.source_name)}",
             f"« {escape(short(n.event.text, 300))} »"]
    if n.event.url:
        lines.append(f'<a href="{escape(n.event.url)}">voir le post</a>')
    lines.append(f"🔑 Mots-clés surveillés : {escape(kws)}")
    lines.append("👀 Je surveille les nouveaux tokens pump.fun qui reprennent ce narratif.")
    return "\n".join(lines)


def format_ai_narrative(n: Narrative, added: list[str]) -> str:
    status = "✅ narratif actif" if n.active else "💤 jugé peu « memeable », ignoré"
    lines = [f"🧠 <b>Analyse IA {n.id}</b> — hype {n.hype_score}/100 ({status})"]
    if n.summary:
        lines.append(escape(n.summary))
    if n.tickers:
        lines.append("Tickers probables : " + escape(", ".join("$" + t for t in n.tickers)))
    if added:
        lines.append("Mots-clés ajoutés : " + escape(", ".join(added[:8])))
    return "\n".join(lines)


def format_rug_warning(token: NewToken, reason: str, sol_usd: float | None,
                       mcap_sol: float) -> str:
    return (f"🚨 <b>ALERTE</b> sur <b>{escape(token.name)}</b> (${escape(token.symbol)})\n"
            f"{escape(reason)}\n"
            f"Market cap actuelle : {fmt_mcap(mcap_sol, sol_usd)}\n"
            f"<code>{token.mint}</code>\n" + links(token.mint))


def format_paper_stats(st: dict[str, Any]) -> str:
    if not st.get("closed"):
        return (f"📒 <b>Bilan paper trading</b>\nAucune alerte clôturée pour l'instant "
                f"({st.get('open', 0)} en cours de suivi). Laisse tourner le bot quelques jours.")
    best_sym, best_x = st["best"]
    lines = [
        "📒 <b>Bilan paper trading</b> (si tu avais acheté à chaque alerte, au prix de l'alerte)",
        f"Alertes clôturées : {st['closed']} · en cours : {st['open']}",
        f"Positives à la fin du suivi : {st['winners_at_end']}/{st['closed']}",
        f"Multiple médian à la fin : x{st['median_end_x']:.2f} · moyen : x{st['mean_end_x']:.2f}",
        f"Multiple médian au plus haut : x{st['median_max_x']:.2f} "
        "(irréaliste : personne ne vend pile au sommet)",
        f"Dev qui a vendu pendant le suivi : {st['rugs']}/{st['closed']}",
        f"Meilleur : ${escape(str(best_sym))} x{best_x:.1f} au plus haut",
    ]
    if st.get("by_verdict"):
        lines.append("Par verdict (médiane fin) : " + ", ".join(
            f"{escape(k)} x{v:.2f}" for k, v in st["by_verdict"].items()))
    lines.append("<i>Frais, slippage et latence non comptés : la réalité est pire que ce bilan.</i>")
    return "\n".join(lines)
