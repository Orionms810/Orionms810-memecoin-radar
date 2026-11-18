"""Le cerveau du bot : relie les sources de news, le flux pump.fun, l'analyse
de risque et les notifications."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from html import escape
from typing import TYPE_CHECKING, Any, Awaitable, Protocol

from .config import Settings
from .models import (
    CreatorInfo,
    DexInfo,
    HolderInfo,
    Narrative,
    NewsEvent,
    NewToken,
    RugcheckInfo,
    TokenMetadata,
    Trade,
    TradeStats,
)
from .creator import build_history, summarize as summarize_history
from .journal import ALERT, REJECT_LIMIT, REJECT_MOMENTUM, REJECT_SAFETY, CandidateJournal
from .journal import format_analysis, to_csv
from .narratives import KeywordBaseline, MatchResult, NarrativeBook, match_token
from .report import (
    TokenReport,
    format_ai_narrative,
    format_alert,
    format_check,
    format_narrative,
    format_paper_stats,
    format_rug_warning,
    fmt_age,
)
from .risk import assess_risk, momentum_score
from .storage import Storage
from .telegram import Notifier
from .text import extract_keywords, manual_keywords

if TYPE_CHECKING:  # l'IA est optionnelle (module anthropic)
    from .ai import AIAnalyst

log = logging.getLogger(__name__)
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

HELP = """🤖 <b>Memecoin Radar</b> — commandes
/check &lt;adresse&gt; — analyse anti-rug d'un token
/narratif &lt;mots&gt; — surveiller un narratif à la main (ex : /narratif covfefe, $COVF)
/narratifs — narratifs actifs
/supprimer &lt;id&gt; — arrêter un narratif (ex : /supprimer N3)
/bilan — bilan « paper trading » des alertes
/analyse — ce que sont devenus les tokens alertés ET refusés
/export — fichier Excel (CSV) de tous les candidats
/status — état du bot
/pause · /reprendre — couper / remettre les alertes
<b>Chaînes Telegram</b> (tweets d'Elon, Trump... republiés)
/chaine @nom [poids] — suivre une chaîne · /chaines — liste · /retirer @nom
<b>Trading auto</b>
/trading — état · /trading on · /trading off
/positions — positions ouvertes
/vendre tout · /vendre $TICKER — vente immédiate
/journal — derniers trades"""
TRADING_COMMANDS = {"/trading", "/positions", "/vendre", "/journal"}
CHANNEL_COMMANDS = {"/chaines", "/chaine", "/retirer"}


class DataProvider(Protocol):
    async def metadata(self, uri: str) -> TokenMetadata | None: ...
    async def holders(self, mint: str, *, exclude_owners: set[str],
                      creator: str | None = None) -> HolderInfo | None: ...
    async def rugcheck(self, mint: str) -> RugcheckInfo | None: ...
    async def creator_coins(self, creator: str) -> list[dict[str, Any]] | None: ...
    async def creator_exits(self, creator: str, mints: list[str]) -> dict[str, bool] | None: ...
    async def pump_coin(self, mint: str) -> dict[str, Any] | None: ...
    async def dexscreener(self, mint: str) -> DexInfo | None: ...
    async def funding(self, buyers: list[str], *, creator: str | None = None) -> Any: ...
    async def sol_usd(self) -> float | None: ...


class TradeFeed(Protocol):
    connected: bool

    async def subscribe_trades(self, mints: list[str]) -> None: ...
    async def unsubscribe_trades(self, mints: list[str]) -> None: ...


@dataclass
class Candidate:
    token: NewToken
    match: MatchResult
    stats: TradeStats
    started_at: float = field(default_factory=time.time)
    stage: str = "observing"  # observing -> watching -> (supprimé)
    alert_id: int | None = None
    alert_msg_id: int | None = None
    alert_mcap: float = 0.0
    peak_after_alert: float = 0.0
    warned_dev_sold: bool = False
    warned_drop: bool = False


async def _safe(aw: Awaitable[Any], timeout: float = 10.0) -> Any:
    try:
        return await asyncio.wait_for(aw, timeout)
    except Exception as e:  # noqa: BLE001 - données optionnelles
        log.debug("Donnée indisponible : %s", e)
        return None


class Engine:
    def __init__(self, settings: Settings, storage: Storage, notifier: Notifier,
                 data: DataProvider, feed: TradeFeed | None = None,
                 ai: AIAnalyst | None = None):
        self.s = settings
        self.storage = storage
        self.notifier = notifier
        self.data = data
        self.feed = feed
        self.ai = ai
        self.book = NarrativeBook(settings.ttl_minutes * 60, set(settings.generic_keywords))
        self.baseline = KeywordBaseline()
        self.recent: deque[NewToken] = deque(maxlen=20_000)
        self.candidates: dict[str, Candidate] = {}
        self.tasks: set[asyncio.Task] = set()
        self.started = time.time()
        self.tokens_seen = 0
        self.alerts_sent = 0
        self.sol_usd: float | None = None
        self.trader: Any = None  # trading.trader.Trader si le trading est activé
        self.tg_channels: Any = None  # sources.telegram_channels si configuré
        self.journal = CandidateJournal(storage)
        self.journal.release = self.release_mint

    # ------------------------------------------------------------- utilitaires
    def spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("Tâche en erreur", exc_info=task.exception())

    # ------------------------------------------------------------------- news
    async def on_event(self, ev: NewsEvent) -> Narrative | None:
        if self.storage.event_seen(ev.event_id):
            return None
        self.storage.add_event(ev.event_id, ev.source, ev.source_name, ev.text, ev.url)
        if ev.source == "manual":
            kws = manual_keywords(ev.text)
        else:
            kws = extract_keywords(ev.text, generic=set(self.s.generic_keywords),
                                   aliases=ev.aliases)
        if not kws:
            log.info("Aucun mot-clé exploitable dans %s", ev.event_id)
            return None
        narr = self.book.add(ev, kws)
        log.info("Narratif %s (%s) : %s", narr.id, ev.source_name, ", ".join(list(kws)[:8]))

        # Le matching démarre tout de suite ; seule l'annonce attend l'avis de l'IA
        # (sinon chaque post banal d'un gros compte déclencherait un message).
        self._retro_match(narr)
        important = self._important(ev)
        if ev.source == "manual":
            pass
        elif self.ai:
            self.spawn(self._ai_enrich(narr))
        elif important:
            await self.notifier.send(format_narrative(narr))
        return narr

    def _important(self, ev: NewsEvent) -> bool:
        if ev.announce is not None:
            return ev.announce
        return ev.weight >= self.s.notify_narrative_min_weight

    async def _ai_enrich(self, narr: Narrative) -> None:
        assert self.ai is not None
        important = self._important(narr.event)
        res = await self.ai.analyze_event(narr.event)
        if res is None:  # IA indisponible : on se rabat sur le poids de la source
            if important:
                await self.notifier.send(format_narrative(narr))
            return
        added = self.book.apply_ai(narr, keywords=res.keywords, tickers=res.tickers,
                                   hype=res.hype_score, memeable=res.memeable,
                                   summary=res.summary_fr, min_hype=self.s.ai_min_hype)
        log.info("IA %s : hype %s, actif=%s, +%s", narr.id, narr.hype_score, narr.active, added)
        hype = narr.hype_score or 0
        if narr.active and (important or hype >= self.s.notify_narrative_min_hype):
            await self.notifier.send(format_narrative(narr) + "\n\n" + format_ai_narrative(narr, added))
        if added and narr.active:
            self._retro_match(narr)

    # ----------------------------------------------------------------- tokens
    def on_new_token(self, token: NewToken) -> None:
        self.tokens_seen += 1
        self.storage.add_token(token)
        self.baseline.add(token.name, token.symbol)
        self.recent.append(token)
        self._try_match(token, self.book.live())

    def _retro_match(self, narr: Narrative) -> None:
        """Un narratif arrive : des tokens ont peut-être été créés juste avant."""
        cutoff = time.time() - self.s.retro_minutes * 60
        for token in list(self.recent):
            if token.created_at >= cutoff:
                self._try_match(token, [narr])

    def _try_match(self, token: NewToken, narratives: list[Narrative]) -> None:
        if token.mint in self.candidates or not narratives:
            return
        m = match_token(token.name, token.symbol, "", narratives, self.baseline)
        if not m or m.score < self.s.min_match_score:
            return
        narr = m.narrative
        narr.matched_mints.add(token.mint)
        if narr.alerts_sent >= self.s.max_alerts_per_narrative:
            return
        if len(self.candidates) >= self.s.max_watched_tokens:
            log.info("Trop de tokens surveillés, %s ignoré", token.symbol)
            return
        cand = Candidate(token=token, match=m, stats=TradeStats.for_token(token))
        self.candidates[token.mint] = cand
        log.info("Candidat %s ($%s) ← %s (score %.2f, mots : %s)", token.name, token.symbol,
                 narr.id, m.score, ", ".join(m.keywords))
        if self.feed:
            self.spawn(self.feed.subscribe_trades([token.mint]))
        self.spawn(self._evaluate_later(cand, self.s.observe_seconds))

    def on_trade(self, trade: Trade) -> None:
        if self.trader:
            self.trader.on_trade(trade)
        self.journal.on_trade(trade)
        cand = self.candidates.get(trade.mint)
        if not cand:
            return
        cand.stats.apply(trade)
        if cand.stage == "watching":
            cand.peak_after_alert = max(cand.peak_after_alert, trade.market_cap_sol)
            self._check_watch(cand)

    def _check_watch(self, cand: Candidate) -> None:
        s, t = cand.stats, cand.token
        if s.creator_sold and not cand.warned_dev_sold:
            cand.warned_dev_sold = True
            self.spawn(self.notifier.send(format_rug_warning(
                t, f"Le créateur vend ({s.creator_sold_sol:.2f} SOL au total). Risque de rug élevé.",
                self.sol_usd, s.last_mcap), reply_to=cand.alert_msg_id))
        drop = self.s.rug_drop_alert_pct / 100
        if (cand.alert_mcap > 0 and not cand.warned_drop
                and s.last_mcap < cand.alert_mcap * (1 - drop)):
            cand.warned_drop = True
            pct = (1 - s.last_mcap / cand.alert_mcap) * 100
            self.spawn(self.notifier.send(format_rug_warning(
                t, f"Chute de -{pct:.0f}% depuis l'alerte.", self.sol_usd, s.last_mcap),
                reply_to=cand.alert_msg_id))

    # ---------------------------------------------------------------- analyse
    async def _evaluate_later(self, cand: Candidate, delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            await self._evaluate(cand)
        except Exception:
            log.exception("Erreur d'évaluation de %s", cand.token.mint)
            await self._drop(cand)

    async def build_report(self, token: NewToken, *, stats: TradeStats | None = None,
                           match: MatchResult | None = None, dex: DexInfo | None = None,
                           dev_buy_known: bool = True) -> TokenReport:
        exclude = {token.bonding_curve} if token.bonding_curve else set()
        if dex and dex.pair_address:
            exclude.add(dex.pair_address)
        buyers = (stats.first_buyers[: self.s.funding_wallets]
                  if stats and self.s.funding_check else [])
        now = time.time()
        local_prev = self.storage.creator_tokens(token.creator, token.mint) if token.creator else []
        # sorties vérifiables : anciens tokens où le dev avait acheté, lancés il y a > 10 min
        exit_mints = [r["mint"] for r in local_prev
                      if (r["initial_buy"] or 0) > 0 and now - (r["created_at"] or now) > 600]
        meta, holders, rug, coins, funding, exits = await asyncio.gather(
            _safe(self.data.metadata(token.uri), 6) if token.uri else _none(),
            _safe(self.data.holders(token.mint, exclude_owners=exclude,
                                    creator=token.creator or None), 12),
            _safe(self.data.rugcheck(token.mint), 8),
            _safe(self.data.creator_coins(token.creator), 6) if token.creator else _none(),
            _safe(self.data.funding(buyers, creator=token.creator or None), 15) if buyers else _none(),
            _safe(self.data.creator_exits(token.creator, exit_mints), 10) if exit_mints else _none(),
        )
        creator = None
        if token.creator:
            n = self.storage.creator_launches(token.creator, time.time() - 86400)
            if self.storage.get_token(token.mint) and token.created_at >= time.time() - 86400:
                n -= 1  # ne pas compter le token analysé lui-même
            local_mints = [r["mint"] for r in local_prev]
            past = build_history(
                current_mint=token.mint, local_mints=local_mints,
                outcomes=self.storage.candidate_outcomes(local_mints), api_coins=coins,
                exits=exits, sol_usd=self.sol_usd, now=now)
            creator = CreatorInfo(launches_24h=max(0, n),
                                  launches_total_api=len(coins) if coins is not None else None,
                                  **summarize_history(past))
        risk = assess_risk(
            dev_buy_pct=token.dev_buy_pct if dev_buy_known else None,
            stats=stats, holders=holders, rugcheck=rug, creator=creator,
            metadata=meta, th=self.s.risk, funding=funding,
        )
        return TokenReport(token=token, risk=risk, momentum=momentum_score(stats), match=match,
                           stats=stats, metadata=meta, holders=holders, rugcheck=rug,
                           creator=creator, dex=dex, funding=funding, sol_usd=self.sol_usd)

    def _copies_rank(self, cand: Candidate) -> tuple[int, int]:
        narr = cand.match.narrative
        peers = [c for c in self.candidates.values() if c.match.narrative is narr]
        peers.sort(key=lambda c: c.stats.buy_sol, reverse=True)
        rank = peers.index(cand) + 1 if cand in peers else 1
        return max(0, len(narr.matched_mints) - 1), rank

    async def _evaluate(self, cand: Candidate) -> None:
        token, narr = cand.token, cand.match.narrative
        report = await self.build_report(token, stats=cand.stats, match=cand.match)
        risk = report.risk
        decision, detail = ALERT, "alerte envoyée"
        if (risk.critical or risk.safety < self.s.min_safety_to_alert) and not self.s.show_dangerous:
            decision = REJECT_SAFETY
            detail = f"sécurité {risk.safety}/100 : " + "; ".join(
                t for lvl, t in risk.flags if lvl in ("danger", "critical"))
        elif report.momentum < self.s.min_momentum:
            decision, detail = REJECT_MOMENTUM, f"momentum {report.momentum}/100 (personne n'achète)"
        elif narr.alerts_sent >= self.s.max_alerts_per_narrative:
            decision, detail = REJECT_LIMIT, f"déjà {narr.alerts_sent} alertes sur {narr.id}"
        row_id = self.journal.record(
            token=token, report=report, stats=cand.stats, decision=decision, detail=detail,
            narrative_id=narr.id, source=narr.event.source_name, match_score=cand.match.score)
        if decision != ALERT:
            log.info("%s ($%s) écarté : %s", token.name, token.symbol, detail)
            await self._drop(cand)
            return
        narr.alerts_sent += 1
        self.alerts_sent += 1
        report.copies, report.rank = self._copies_rank(cand)
        observed = time.time() - cand.started_at
        msg_id = await self.notifier.send(format_alert(report, observe_seconds=observed))
        cand.stage = "watching"
        cand.alert_msg_id = msg_id
        cand.alert_mcap = cand.stats.last_mcap or token.market_cap_sol
        cand.peak_after_alert = cand.alert_mcap
        cand.alert_id = self.storage.add_alert(
            mint=token.mint, name=token.name, symbol=token.symbol, narrative=narr.id,
            match_score=cand.match.score, safety=risk.safety, momentum=report.momentum,
            verdict=risk.verdict[1], entry_mcap_sol=cand.alert_mcap)
        if self.trader:
            self.spawn(self._trade_and_log(token, report, cand.stats, msg_id, row_id))
        if self.ai and self.s.ai_review_alerts:
            self.spawn(self._ai_review(report, msg_id))
        self._check_watch(cand)  # le dev a peut-être déjà vendu pendant l'envoi
        self.spawn(self._watch_then_close(cand))

    async def _trade_and_log(self, token: NewToken, report: TokenReport, stats: TradeStats,
                             msg_id: int | None, row_id: int) -> None:
        reason = await self.trader.on_signal(token, report, stats, msg_id)
        self.journal.set_trade(row_id, reason is None, reason or "acheté")

    async def _ai_review(self, report: TokenReport, msg_id: int | None) -> None:
        assert self.ai is not None
        text = await self.ai.review_alert(report.to_dict())
        if text:
            await self.notifier.send(f"🧠 <b>Avis IA sur ${escape(report.token.symbol)}</b>\n"
                                     f"{escape(text)}", reply_to=msg_id)

    async def _watch_then_close(self, cand: Candidate) -> None:
        await asyncio.sleep(self.s.watch_minutes * 60)
        s = cand.stats
        if cand.alert_id:
            self.storage.close_alert(cand.alert_id, max_mcap_sol=cand.peak_after_alert,
                                     end_mcap_sol=s.last_mcap, creator_sold=s.creator_sold)
        await self._drop(cand)

    async def _drop(self, cand: Candidate) -> None:
        self.candidates.pop(cand.token.mint, None)
        await self.release_mint(cand.token.mint)

    async def release_mint(self, mint: str) -> None:
        """Se désabonne des trades d'un token si plus personne ne le suit."""
        if (mint in self.candidates or self.journal.holds(mint)
                or (self.trader and self.trader.holds(mint))):
            return
        if self.feed:
            await self.feed.unsubscribe_trades([mint])

    # --------------------------------------------------------- /check manuel
    async def manual_check(self, mint: str) -> str:
        token = self.storage.get_token(mint)
        dev_known = token is not None
        extra: list[str] = []
        dex = await _safe(self.data.dexscreener(mint), 8)
        if token is None:
            coin = await _safe(self.data.pump_coin(mint), 6) or {}
            created = coin.get("created_timestamp")
            if isinstance(created, (int, float)):
                created_at = float(created) / 1000  # millisecondes -> secondes
            else:
                created_at = (dex.pair_created_at if dex else None) or 0.0
            token = NewToken(
                mint=mint,
                name=str(coin.get("name") or (dex.name if dex else "")),
                symbol=str(coin.get("symbol") or (dex.symbol if dex else "")),
                creator=str(coin.get("creator") or ""),
                uri=str(coin.get("metadata_uri") or ""),
                bonding_curve=str(coin.get("bonding_curve") or ""),
                created_at=created_at,
            )
            extra.append("Achat initial du dev inconnu (token lancé avant le démarrage du bot)")
            if not token.creator:
                extra.append("Créateur inconnu : historique du créateur non vérifié")
        cand = self.candidates.get(mint)
        stats = cand.stats if cand else None
        if stats is None and self.storage.creator_sold_seen(mint):
            stats = TradeStats.for_token(token)  # le bot a vu le dev vendre lors d'un suivi
            stats.creator_sold = True
        report = await self.build_report(token, stats=stats, dex=dex, dev_buy_known=dev_known)
        report.extra_flags = extra
        if not report.holders:
            report.extra_flags.append("Holders non vérifiés (RPC Solana indisponible)")
        if not report.rugcheck and self.s.use_rugcheck:
            report.extra_flags.append("RugCheck indisponible pour ce token")
        return format_check(report)

    # -------------------------------------------------------------- commandes
    async def handle_command(self, text: str) -> str | None:
        cmd, _, arg = text.strip().partition(" ")
        cmd = cmd.lower().split("@")[0]
        arg = arg.strip()
        if cmd in ("/start", "/aide", "/help"):
            return HELP
        if cmd == "/check":
            if not MINT_RE.match(arg):
                return "Usage : /check &lt;adresse du token&gt; (adresse Solana du mint)"
            return await self.manual_check(arg)
        if cmd == "/narratif":
            if not arg:
                return "Usage : /narratif mots, clés, $TICKER"
            narr = await self.on_event(NewsEvent(
                source="manual", source_name="toi (manuel)", event_id=f"manual:{time.time()}",
                text=arg, weight=1.0))
            if not narr:
                return "Aucun mot-clé exploitable (3 caractères minimum)."
            return format_narrative(narr)
        if cmd == "/narratifs":
            live = self.book.live()
            if not live:
                return "Aucun narratif actif pour l'instant."
            lines = ["📚 <b>Narratifs actifs</b>"]
            for n in sorted(live, key=lambda n: -n.created_at)[:15]:
                hype = f" · hype {n.hype_score}" if n.hype_score is not None else ""
                lines.append(f"<b>{n.id}</b> {escape(n.event.source_name)}{hype} · "
                             f"il y a {fmt_age(time.time() - n.created_at)} · "
                             f"{len(n.matched_mints)} token(s) — {escape(', '.join(list(n.keywords)[:5]))}")
            return "\n".join(lines)
        if cmd in ("/supprimer", "/stop"):
            return (f"Narratif {escape(arg.upper())} supprimé." if self.book.remove(arg)
                    else "Narratif introuvable (voir /narratifs).")
        if cmd == "/pause":
            self.notifier.muted = True
            return "🔕 Alertes en pause (/reprendre pour les remettre)."
        if cmd == "/reprendre":
            self.notifier.muted = False
            return "🔔 Alertes réactivées."
        if cmd == "/analyse":
            return format_analysis(self.storage.candidate_rows())
        if cmd == "/export":
            rows = self.storage.candidate_rows()
            if not rows:
                return "Aucun candidat enregistré pour l'instant."
            name = time.strftime("candidats-%Y%m%d-%H%M.csv")
            ok = await self.notifier.send_document(name, to_csv(rows),
                                                   f"{len(rows)} candidats (ouvre avec Excel)")
            return None if ok else "Envoi du fichier impossible (Telegram non configuré ?)."
        if cmd == "/bilan":
            return format_paper_stats(self.storage.paper_stats())
        if cmd in CHANNEL_COMMANDS:
            if not self.tg_channels:
                return ("Chaînes Telegram non configurées : ajoute TG_API_ID et TG_API_HASH dans "
                        ".env, lance « python -m memebot telegram-login », puis relance le bot.")
            return await self.tg_channels.handle_command(cmd, arg)
        if cmd in TRADING_COMMANDS:
            if not self.trader:
                return 'Trading désactivé (mode = "off" dans config.toml, section [trading]).'
            return await self.trader.handle_command(cmd, arg)
        if cmd == "/status":
            ws = "connecté" if self.feed and self.feed.connected else "déconnecté"
            sol = f"${self.sol_usd:.0f}" if self.sol_usd else "?"
            return (f"⚙️ <b>Status</b>\nEn ligne depuis {fmt_age(time.time() - self.started)}\n"
                    f"Flux pump.fun : {ws} · {self.tokens_seen} tokens vus\n"
                    f"Narratifs actifs : {len(self.book.live())} · tokens surveillés : "
                    f"{len(self.candidates)} · alertes : {self.alerts_sent}\n"
                    f"IA : {'oui' if self.ai else 'non'} · SOL : {sol} · "
                    f"alertes {'en pause' if self.notifier.muted else 'actives'}")
        return "Commande inconnue. Tape /aide."

    # ------------------------------------------------------------- entretien
    async def housekeeping(self, every: float = 60) -> None:
        last_prune = 0.0
        while True:
            now = time.time()
            self.book.prune(now)
            cutoff = now - self.s.retro_minutes * 60
            while self.recent and self.recent[0].created_at < cutoff:
                self.recent.popleft()
            price = await _safe(self.data.sol_usd(), 10)
            if price:
                self.sol_usd = price
            if now - last_prune > 6 * 3600:
                self.storage.prune()
                last_prune = now
            await asyncio.sleep(every)


async def _none() -> None:
    return None
