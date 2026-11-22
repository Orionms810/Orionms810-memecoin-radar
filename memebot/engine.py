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
from .census import TokenCensus
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
    fmt_mcap,
    links,
)
from .risk import assess_risk, momentum_score
from .storage import Storage
from .telegram import Notifier
from .text import extract_keywords, manual_keywords
from .trending import TREND_ID, Probe, curve_trigger
from .vip import CREATOR_ID, bonding_curve_address, extract_mints, is_pubkey

if TYPE_CHECKING:  # l'IA est optionnelle (module anthropic)
    from .ai import AIAnalyst

log = logging.getLogger(__name__)
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

HELP = """🤖 <b>Memecoin Radar</b> — commandes
/check &lt;adresse&gt; — analyse anti-rug d'un token
/narratif &lt;mots&gt; — surveiller un narratif à la main (ex : /narratif covfefe, $COVF)
/narratifs — narratifs actifs
/tendance — tokens qui décollent sans news : /tendance on · /tendance off
/tendance achats off — la tendance observe sans acheter (/tendance achats on pour réactiver)
/supprimer &lt;id&gt; — arrêter un narratif (ex : /supprimer N3)
/bilan — bilan « paper trading » des alertes
/analyse — ce que sont devenus les tokens alertés ET refusés
/export — fichier Excel (CSV) de tous les candidats
/export recensement 2 — tous les tokens des 2 derniers jours (pour les stats)
/status — état du bot
/pause · /reprendre — couper / remettre les alertes
<b>Chaînes Telegram</b> (tweets d'Elon, Trump... republiés)
/chaine @nom [poids] — suivre une chaîne · /chaines — liste · /retirer @nom
<b>Trading auto</b>
/trading — état · /trading on · /trading off
/positions — positions ouvertes
/vendre tout · /vendre $TICKER — vente immédiate
/journal — derniers trades
<b>Snipes</b> (achat immédiat, sans observation)
/confiance @chaine — si cette chaîne poste l'adresse d'un token : achat immédiat
/confiance — liste · /confiance retirer @chaine
/createur &lt;wallet&gt; [nom] — quand ce wallet lance un token : achat immédiat
/createurs — liste · /createur retirer &lt;wallet&gt;
/snipe &lt;adresse&gt; — acheter un token tout de suite
/sorties — ce que les tokens sont devenus après la vente (sort-il trop tôt ?)
/export trades — fichier de tous les trades"""
TRADING_COMMANDS = {"/trading", "/positions", "/vendre", "/journal", "/sorties"}
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
    async def curves(self, addresses: list[str]) -> dict[str, Any]: ...
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
        self.token_feed: Any = None  # flux des nouveaux tokens (PumpPortal), si différent de feed
        self._seen_mints: dict[str, None] = {}
        self.tg_channels: Any = None  # sources.telegram_channels si configuré
        self.journal = CandidateJournal(storage, fetch_curves=self._fetch_curves)
        self.census = TokenCensus(storage, settings.census)
        # Avant le flux de trades on-chain (nov. 2026), aucun trade n'arrivait : les
        # lignes du journal de cette période sont fausses (tout à x1.00). On les ignore.
        self.journal_since = storage.kv_get("journal_since")
        if self.journal_since is None:
            self.journal_since = time.time()
            storage.kv_set("journal_since", self.journal_since)
        # Stratégie « tendance » : un pseudo-narratif qui ne matche aucun mot-clé
        self.trend_narrative = Narrative(
            id=TREND_ID, keywords={}, created_at=time.time(), expires_at=float("inf"),
            event=NewsEvent(source="trend", source_name="🔥 Tendance pump.fun", event_id="trend",
                            text="Token qui décolle tout seul, sans news", weight=0.5,
                            announce=False))
        saved = storage.kv_get("trend_enabled", None)
        self.trend_enabled = settings.trend.enabled if saved is None else bool(saved)
        saved_trade = storage.kv_get("trend_trade", None)
        if saved_trade is not None:  # choix fait avec /tendance achats on|off
            settings.trend.trade = bool(saved_trade)
        self.probation: dict[str, Probe] = {}
        self._trend_checks: deque[float] = deque()
        # Créateurs qui ont déjà fait graduer un token : un autre pseudo-narratif
        self.creator_narrative = Narrative(
            id=CREATOR_ID, keywords={}, created_at=time.time(), expires_at=float("inf"),
            event=NewsEvent(source="creator", source_name="🏆 Créateur qui a déjà réussi",
                            event_id="creator", text="Un ancien token de ce créateur a gradué",
                            weight=0.5, announce=False))
        # Snipes : comptes de confiance (chaînes) et wallets de créateurs suivis
        self.trusted: set[str] = set(storage.kv_get("trusted_accounts", []) or [])
        self.watched_creators: dict[str, str] = dict(storage.kv_get("watched_creators", {}) or {})
        self._sniped: dict[str, None] = {}
        self._proven_checks: deque[float] = deque()

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
        if self.s.snipe.enabled and ev.source != "manual" and self.account_of(ev) in self.trusted:
            for mint in extract_mints(ev.text)[:3]:  # le CA posté par le vrai compte
                self.spawn(self.snipe_mint(mint, f"CA posté par {ev.source_name}"))
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
        if token.mint in self._seen_mints:  # reçu aussi par l'autre source
            return
        self._seen_mints[token.mint] = None
        if len(self._seen_mints) > 50_000:
            self._seen_mints.pop(next(iter(self._seen_mints)))
        self.tokens_seen += 1
        self.storage.add_token(token)
        self.baseline.add(token.name, token.symbol)
        self.recent.append(token)
        self._try_match(token, self.book.live())
        if self.s.snipe.enabled and token.creator in self.watched_creators:
            name = self.watched_creators[token.creator]
            self.spawn(self.fire_snipe(token, token.market_cap_sol,
                                       f"nouveau token de {name} (créateur suivi)"))
        elif self.s.snipe.proven_creators and token.creator and token.mint not in self.candidates \
                and len(self.candidates) < self.s.max_watched_tokens:
            grads = self.storage.creator_graduations(token.creator, token.mint)
            now = time.time()
            while self._proven_checks and now - self._proven_checks[0] > 3600:
                self._proven_checks.popleft()
            if grads and len(self._proven_checks) < self.s.snipe.proven_max_per_hour:
                self._proven_checks.append(now)
                m = MatchResult(self.creator_narrative, 1.0, [f"{grads} ancien(s) token(s) gradué(s)"])
                self._add_candidate(token, m, self.s.observe_seconds)
        if self.trend_enabled and token.mint not in self.candidates:
            self._start_probe(token)
        if self.s.census.enabled:
            cand = self.candidates.get(token.mint)
            prev = self.storage.creator_tokens(token.creator, token.mint, limit=50) if token.creator else []
            recent = sum(1 for r in prev if (r["created_at"] or 0) >= token.created_at - 86400)
            self.census.add(token, narrative=cand.match.narrative.id if cand else "",
                            creator_24h=recent, creator_prev=len(prev))

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
        log.info("Candidat %s ($%s) ← %s (score %.2f, mots : %s)", token.name, token.symbol,
                 narr.id, m.score, ", ".join(m.keywords))
        self._add_candidate(token, m, self.s.observe_seconds)

    def _add_candidate(self, token: NewToken, m: MatchResult, delay: float,
                       curve: Any = None) -> None:
        self.probation.pop(token.mint, None)
        stats = TradeStats.for_token(token)
        if curve is not None:  # point de départ : l'état actuel de la courbe
            stats.last_mcap = curve.market_cap_sol
            stats.max_mcap = max(stats.max_mcap, curve.market_cap_sol)
            stats.last_v_tokens = curve.v_tokens
        cand = Candidate(token=token, match=m, stats=stats)
        self.candidates[token.mint] = cand
        if self.feed:
            self.spawn(self.feed.subscribe_trades([token.mint]))
        self.spawn(self._evaluate_later(cand, delay))

    # -------------------------------------------------------- stratégie tendance
    def _start_probe(self, token: NewToken) -> None:
        if token.mint in self.probation or len(self.probation) >= self.s.trend.max_probes:
            return
        if not token.bonding_curve:
            return  # sans adresse de courbe, impossible de lire son état
        self.probation[token.mint] = Probe(token=token,
                                           until=token.created_at + self.s.trend.window_seconds)

    async def _fetch_curves(self, addresses: list[str]) -> dict[str, Any]:
        return await _safe(self.data.curves(addresses), 15) or {}

    async def dex_price_sol(self, mint: str) -> float | None:
        """Prix en SOL par token sur DexScreener (tokens gradués, hors bonding curve)."""
        info = await _safe(self.data.dexscreener(mint), 10)
        if info and info.price_usd and self.sol_usd:
            return info.price_usd / self.sol_usd
        return None

    async def poll_curves(self, now: float | None = None) -> None:
        """Un passage : lit en une fois les courbes utiles au recensement et à la tendance."""
        now = now or time.time()
        for mint, probe in list(self.probation.items()):
            if now > probe.until:
                del self.probation[mint]
        probes = list(self.probation.values()) if self.trend_enabled else []
        addrs = {p.token.bonding_curve for p in probes}
        addrs.update(self.census.due(now))
        if not addrs:
            return
        curves = await self._fetch_curves(sorted(addrs))
        self.census.record(curves, now)
        if probes:
            self._apply_trend(probes, curves, now)

    poll_trending = poll_curves  # ancien nom

    def _apply_trend(self, probes: list[Probe], curves: dict[str, Any], now: float) -> None:
        while self._trend_checks and now - self._trend_checks[0] > 3600:
            self._trend_checks.popleft()
        for probe in probes:
            token = probe.token
            curve = curves.get(token.bonding_curve)
            if curve is None or token.mint not in self.probation:
                continue
            if curve.complete:  # déjà gradué : trop tard
                self.probation.pop(token.mint, None)
                continue
            reason = curve_trigger(token, curve, now, self.s.trend)
            if not reason:
                continue
            if (len(self._trend_checks) >= self.s.trend.max_checks_per_hour
                    or len(self.candidates) >= self.s.max_watched_tokens):
                continue  # quota atteint : on réessaiera au prochain passage
            self._trend_checks.append(now)
            log.info("Tendance : %s ($%s) — %s", token.name, token.symbol, reason)
            self._add_candidate(token, MatchResult(self.trend_narrative, 1.0, [reason]),
                                self.s.observe_seconds, curve=curve)

    async def run_curves(self) -> None:
        while True:
            try:
                await self.poll_curves()
            except Exception:  # noqa: BLE001
                log.exception("Lecture des courbes : erreur")
            await asyncio.sleep(self.s.trend.poll_seconds)

    run_trending = run_curves  # ancien nom

    def set_trend(self, enabled: bool) -> None:
        self.trend_enabled = enabled
        self.storage.kv_set("trend_enabled", enabled)
        if not enabled:
            self.probation.clear()

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
        elif not self._pseudo(narr) and narr.alerts_sent >= self.s.max_alerts_per_narrative:
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
        if not self._pseudo(narr):
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
        trade_ok = (self.s.trend.trade if narr is self.trend_narrative else
                    self.s.snipe.proven_trade if narr is self.creator_narrative else True)
        if self.trader and trade_ok:
            self.spawn(self._trade_and_log(token, report, cand.stats, msg_id, row_id))
        if self.ai and self.s.ai_review_alerts:
            self.spawn(self._ai_review(report, msg_id))
        self._check_watch(cand)  # le dev a peut-être déjà vendu pendant l'envoi
        self.spawn(self._watch_then_close(cand))

    def _pseudo(self, narr: Narrative) -> bool:
        """Tendance et créateurs : pas de vrai narratif derrière."""
        return narr is self.trend_narrative or narr is self.creator_narrative

    # ------------------------------------------------------------------ snipes
    @staticmethod
    def account_of(ev: NewsEvent) -> str:
        """Compte qui a posté : la chaîne Telegram (d'après le lien) ou le compte X."""
        m = re.search(r"t\.me/([A-Za-z0-9_]+)/", ev.url or "")
        return (m.group(1) if m else ev.source_name.lstrip("@")).lower()

    async def snipe_mint(self, mint: str, reason: str) -> None:
        """Adresse postée : on vérifie que c'est un token pump.fun puis on achète."""
        if mint in self._sniped:
            return
        token = self.storage.get_token(mint)
        bc = token.bonding_curve if token and token.bonding_curve else bonding_curve_address(mint)
        curve = (await self._fetch_curves([bc])).get(bc)
        if curve is None:
            if mint in self._sniped:
                return
            self._sniped[mint] = None
            await self.notifier.send(
                f"⚡ <b>Adresse détectée</b> — {escape(reason)}\n<code>{mint}</code>\n"
                "Ce n'est pas un token pump.fun (ou le RPC n'a pas répondu) : pas d'achat "
                "automatique. Regarde-le à la main :\n" + links(mint), force=True)
            return
        if token is None:
            coin = await _safe(self.data.pump_coin(mint), 5) or {}
            created = coin.get("created_timestamp")
            token = NewToken(
                mint=mint, name=str(coin.get("name") or "?"), symbol=str(coin.get("symbol") or "?"),
                creator=str(coin.get("creator") or ""), uri=str(coin.get("metadata_uri") or ""),
                created_at=float(created) / 1000 if isinstance(created, (int, float)) else time.time())
        token.bonding_curve = bc
        mcap = curve.market_cap_sol
        if curve.complete:  # déjà sur PumpSwap : prix DexScreener
            dex = await _safe(self.data.dexscreener(mint), 8)
            mcap = dex.market_cap_usd / self.sol_usd if dex and dex.market_cap_usd and self.sol_usd else 0.0
            if dex and token.symbol == "?":
                token.name, token.symbol = dex.name or "?", dex.symbol or "?"
        token.market_cap_sol = mcap
        await self.fire_snipe(token, mcap, reason, graduated=curve.complete)

    async def fire_snipe(self, token: NewToken, mcap: float, reason: str, *,
                         graduated: bool = False) -> None:
        if token.mint in self._sniped:
            return
        self._sniped[token.mint] = None
        if len(self._sniped) > 5000:
            self._sniped.pop(next(iter(self._sniped)))
        ss = self.s.snipe
        msg_id = await self.notifier.send("\n".join([
            f"⚡ <b>SNIPE</b> — {escape(reason)}", "",
            f"<b>{escape(token.name)}</b> (${escape(token.symbol)})",
            f"<code>{token.mint}</code>",
            f"💰 Market cap : {fmt_mcap(mcap, self.sol_usd) if mcap else '?'}"
            + (" · 🎓 déjà gradué" if graduated else ""),
            "⚠️ Achat immédiat, sans observation ni contrôle anti-rug : la vitesse d'abord. "
            f"Contrôle quand même avec /check {token.mint}",
            "", links(token.mint)]), force=True)
        if not (self.trader and ss.trade):
            return
        why = await self.trader.snipe(token, mcap, reason, msg_id, graduated=graduated,
                                      max_mcap_sol=ss.max_mcap_sol, extra_slots=ss.extra_slots)
        if why:
            await self.notifier.send(f"Pas d'achat de ${escape(token.symbol)} : {escape(why)}",
                                     reply_to=msg_id, force=True)

    def _trusted_command(self, arg: str) -> str:
        words = arg.split()
        if not words:
            if not self.trusted:
                return ("Aucun compte de confiance. Ajoute une chaîne qui republie les posts d'UNE "
                        "personne : /confiance @nom_de_la_chaine")
            return "🤝 <b>Comptes de confiance</b> (achat immédiat si ils postent une adresse)\n" + \
                "\n".join(f"@{escape(h)}" for h in sorted(self.trusted))
        remove = words[0].lower() in ("retirer", "supprimer", "remove", "off")
        raw = words[1] if remove and len(words) > 1 else words[0]
        h = re.sub(r"^(https?://)?(www\.)?(t|telegram|x|twitter)\.(me|com)/", "", raw.strip(), flags=re.I)
        h = h.lstrip("@").split("/")[0].lower()
        if not re.fullmatch(r"[a-z0-9_]{3,40}", h):
            return "Usage : /confiance @nom_de_la_chaine · /confiance retirer @nom"
        if remove:
            self.trusted.discard(h)
            self.storage.kv_set("trusted_accounts", sorted(self.trusted))
            return f"@{escape(h)} n'est plus un compte de confiance."
        self.trusted.add(h)
        self.storage.kv_set("trusted_accounts", sorted(self.trusted))
        lines = [f"✅ @{escape(h)} est un compte de confiance : s'il poste l'adresse d'un token "
                 "pump.fun, j'achète tout de suite.",
                 "⚠️ Mets en confiance UNIQUEMENT des chaînes qui republient les posts d'une seule "
                 "personne (son compte officiel). Une chaîne de news ou de « calls » qui poste "
                 "n'importe quelle adresse = achats de scams."]
        channels = getattr(self.tg_channels, "channels", None)
        if channels is not None and h not in channels:
            lines.append(f"Je ne suis pas encore cette chaîne : ajoute-la aussi avec /chaine @{escape(h)}")
        return "\n".join(lines)

    def _creators_command(self, cmd: str, arg: str) -> str:
        words = arg.split()
        if cmd == "/createurs" or not words:
            if not self.watched_creators:
                return ("Aucun créateur suivi. Ajoute le wallet d'un créateur : "
                        "/createur &lt;wallet&gt; nom")
            return "👤 <b>Créateurs suivis</b> (achat immédiat à chaque nouveau token)\n" + "\n".join(
                f"{escape(n)} — <code>{w}</code>" for w, n in self.watched_creators.items())
        if words[0].lower() in ("retirer", "supprimer", "remove") and len(words) > 1:
            name = self.watched_creators.pop(words[1], None)
            self.storage.kv_set("watched_creators", self.watched_creators)
            return f"{escape(name)} n'est plus suivi." if name else "Wallet introuvable (voir /createurs)."
        wallet = words[0]
        if not is_pubkey(wallet):
            return "Usage : /createur &lt;adresse du wallet&gt; nom · /createur retirer &lt;wallet&gt;"
        name = " ".join(words[1:])[:40] or wallet[:6]
        self.watched_creators[wallet] = name
        self.storage.kv_set("watched_creators", self.watched_creators)
        return (f"✅ Créateur suivi : <b>{escape(name)}</b>. Dès que ce wallet lance un token sur "
                "pump.fun, j'achète tout de suite.\nAstuce : le wallet d'un créateur est sur la page "
                "pump.fun de ses tokens (« created by ») ou sur Solscan.")

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
        # (le journal et la stratégie tendance lisent les courbes : pas besoin des trades)
        if mint in self.candidates or (self.trader and self.trader.holds(mint)):
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
        if cmd == "/tendance":
            words = arg.lower().split()
            if len(words) == 2 and words[0] in ("achats", "achat") and words[1] in ("on", "off"):
                self.s.trend.trade = words[1] == "on"
                self.storage.kv_set("trend_trade", self.s.trend.trade)
            elif arg.lower() in ("on", "off"):
                self.set_trend(arg.lower() == "on")
            t = self.s.trend
            state = "✅ activée" if self.trend_enabled else "⏹ désactivée"
            return (f"🔥 <b>Stratégie tendance</b> : {state}\n"
                    f"Tokens en observation : {len(self.probation)} · analyses cette heure : "
                    f"{len(self._trend_checks)}/{t.max_checks_per_hour}\n"
                    f"Déclenchement : ≥ {t.min_buy_sol:g} SOL entrés (hors dev), courbe "
                    f"≥ {t.min_progress_pct:g}%, entre {t.min_age_seconds:g} et "
                    f"{t.window_seconds:g} s après le lancement\n"
                    f"Achats papier sur ces tokens : {'oui' if t.trade else 'non (observation seule)'}\n"
                    "/tendance on · /tendance off · /tendance achats on · /tendance achats off")
        if cmd == "/analyse":
            return format_analysis(self.storage.candidate_rows(self.journal_since))
        if cmd == "/export" and arg.lower().split()[:1] == ["recensement"]:
            parts = arg.split()
            days = float(parts[1]) if len(parts) > 1 and parts[1].replace(".", "", 1).isdigit() else 2
            rows = self.storage.census_rows(time.time() - days * 86400)
            if not rows:
                return "Recensement vide pour l'instant (il faut ~30 min par token)."
            name = time.strftime("recensement-%Y%m%d-%H%M.csv")
            ok = await self.notifier.send_document(
                name, to_csv(rows), f"{len(rows)} tokens recensés sur {days:g} jour(s)")
            return None if ok else "Envoi du fichier impossible (Telegram non configuré ?)."
        if cmd == "/export" and arg.lower().split()[:1] == ["trades"]:
            rows = self.storage.trade_rows()
            if not rows:
                return "Aucun trade clôturé pour l'instant."
            ok = await self.notifier.send_document(time.strftime("trades-%Y%m%d-%H%M.csv"),
                                                   to_csv(rows), f"{len(rows)} trades")
            return None if ok else "Envoi du fichier impossible (Telegram non configuré ?)."
        if cmd == "/export":
            rows = self.storage.candidate_rows(self.journal_since)
            if not rows:
                return "Aucun candidat enregistré pour l'instant."
            name = time.strftime("candidats-%Y%m%d-%H%M.csv")
            ok = await self.notifier.send_document(name, to_csv(rows),
                                                   f"{len(rows)} candidats (ouvre avec Excel)")
            return None if ok else "Envoi du fichier impossible (Telegram non configuré ?)."
        if cmd == "/bilan":
            return format_paper_stats(self.storage.paper_stats())
        if cmd == "/snipe":
            mints = extract_mints(arg)
            if not mints:
                return "Usage : /snipe &lt;adresse du token&gt; — achat immédiat"
            self._sniped.pop(mints[0], None)  # un /snipe manuel peut retenter
            await self.snipe_mint(mints[0], "envoyé par toi (/snipe)")
            return None
        if cmd == "/confiance":
            return self._trusted_command(arg)
        if cmd in ("/createur", "/createurs"):
            return self._creators_command(cmd, arg)
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
            def state(f: Any) -> str:
                return "connecté" if f and f.connected else "déconnecté"
            sol = f"${self.sol_usd:.0f}" if self.sol_usd else "?"
            sources = []
            if hasattr(self.feed, "creations_ok"):
                sources.append(f"blockchain {'✅' if self.feed.creations_ok else '❌'}")
            pp = self.token_feed if self.token_feed is not None else self.feed
            if pp is not None and not hasattr(pp, "creations_ok"):
                sources.append(f"PumpPortal {'✅' if pp.connected else '❌'}")
            return (f"⚙️ <b>Status</b>\nEn ligne depuis {fmt_age(time.time() - self.started)}\n"
                    f"Nouveaux tokens : {' · '.join(sources) or '?'} · {self.tokens_seen} vus\n"
                    f"Trades : {state(self.feed)} · {getattr(self.feed, 'messages', 0)} messages reçus\n"
                    f"Narratifs actifs : {len(self.book.live())} · tokens surveillés : "
                    f"{len(self.candidates)} · alertes : {self.alerts_sent}\n"
                    f"Recensement : {self.storage.census_count()} tokens enregistrés · "
                    f"{len(self.census.tracked)} en cours\n"
                    f"Tendance : {'on' if self.trend_enabled else 'off'} "
                    f"({len(self.probation)} en observation)\n"
                    f"Snipes : {len(self.trusted)} compte(s) de confiance · "
                    f"{len(self.watched_creators)} créateur(s) suivi(s) · achat "
                    f"{'immédiat' if self.s.snipe.trade else 'non (alerte seule)'}\n"
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
                self.storage.prune_census(self.s.census.keep_days)
                last_prune = now
            await asyncio.sleep(every)


async def _none() -> None:
    return None
