"""Le trader automatique : entre sur les meilleures alertes, gère les sorties
(stop-loss, prise de profit, trailing stop, sortie si le dev vend) et applique
les coupe-circuits de risque."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from html import escape
from typing import Any, Awaitable, Callable

from ..models import PUMP_TOTAL_SUPPLY, NewToken, Trade, TradeStats
from ..report import TokenReport, fmt_usd, links
from ..storage import Storage
from ..telegram import Notifier
from .guard import GuardState, RiskGuard
from .settings import TradingSettings

log = logging.getLogger(__name__)


@dataclass
class Position:
    mint: str
    name: str
    symbol: str
    creator: str
    entry_sol: float  # SOL dépensés, frais inclus
    tokens: float  # tokens encore détenus
    entry_price: float  # SOL par token, frais inclus
    opened_at: float
    peak_price: float
    last_price: float
    last_trade_at: float
    realized_sol: float = 0.0
    tp_done: bool = False
    closing: bool = False
    sell_failures: int = 0
    alert_msg_id: int | None = None

    @property
    def value(self) -> float:
        return self.tokens * self.last_price

    @property
    def gain_pct(self) -> float:
        return (self.last_price / self.entry_price - 1) * 100 if self.entry_price else 0.0

    @property
    def peak_gain_pct(self) -> float:
        return (self.peak_price / self.entry_price - 1) * 100 if self.entry_price else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["closing"] = False
        return d


class Trader:
    def __init__(self, ts: TradingSettings, executor: Any, storage: Storage, notifier: Notifier,
                 *, feed: Any = None, sol_usd: Callable[[], float | None] = lambda: None):
        self.ts = ts
        self.ex = executor
        self.storage = storage
        self.notifier = notifier
        self.feed = feed
        self.sol_usd = sol_usd
        self.release: Callable[[str], Awaitable[None]] | None = None
        self.mode = "live" if getattr(executor, "live", False) else "paper"
        self.positions: dict[str, Position] = {}
        self.prices: dict[str, float] = {}
        self.pending: set[str] = set()
        self.tasks: set[asyncio.Task] = set()
        self._warned: set[str] = set()
        self._live_balance: float | None = None

        self.enabled = bool(storage.kv_get(self._k("enabled"), True))
        self.cash = float(storage.kv_get("paper_cash", ts.paper_start_sol))
        self.guard = RiskGuard(ts, GuardState(**(storage.kv_get(self._k("guard"), {}) or {})))
        for d in storage.kv_get(self._k("positions"), []) or []:
            pos = Position(**d)
            self.positions[pos.mint] = pos
            self.prices[pos.mint] = pos.last_price

    # ------------------------------------------------------------ utilitaires
    def _k(self, name: str) -> str:
        return f"trading:{self.mode}:{name}"

    def _save(self) -> None:
        self.storage.kv_set(self._k("positions"), [p.to_dict() for p in self.positions.values()])
        self.storage.kv_set(self._k("guard"), self.guard.state.to_dict())
        self.storage.kv_set(self._k("enabled"), self.enabled)
        if self.mode == "paper":
            self.storage.kv_set("paper_cash", self.cash)

    def spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception():
            log.error("Tâche de trading en erreur", exc_info=task.exception())

    def holds(self, mint: str) -> bool:
        return mint in self.positions or mint in self.pending

    def price_of(self, mint: str) -> float | None:
        return self.prices.get(mint)

    def _usd(self, sol: float) -> str:
        p = self.sol_usd()
        return f" (~{fmt_usd(sol * p)})" if p else ""

    @property
    def tag(self) -> str:
        return "💸 RÉEL" if self.mode == "live" else "🧪 papier"

    async def available_cash(self) -> float:
        if self.mode == "live":
            bal = await self.ex.balance()
            self._live_balance = bal
            return bal
        return self.cash

    def equity_from(self, cash: float) -> float:
        return cash + sum(p.value for p in self.positions.values())

    async def _notify(self, text: str, reply_to: int | None = None) -> None:
        await self.notifier.send(text, reply_to=reply_to, force=True)

    async def _warn_once(self, key: str, text: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            await self._notify(text)

    # ----------------------------------------------------------------- entrées
    def entry_rejection(self, token: NewToken, report: TokenReport,
                        stats: TradeStats | None, now: float | None = None) -> str | None:
        now = now or time.time()
        ts = self.ts
        if report.risk.critical or report.risk.safety < ts.min_safety:
            return f"sécurité {report.risk.safety}/100 < {ts.min_safety}"
        if report.momentum < ts.min_momentum:
            return f"momentum {report.momentum}/100 < {ts.min_momentum}"
        if stats and stats.creator_sold:
            return "le dev a vendu"
        mcap = stats.last_mcap if stats and stats.last_mcap else token.market_cap_sol
        if mcap > ts.max_entry_mcap_sol:
            return f"market cap {mcap:.0f} SOL > {ts.max_entry_mcap_sol:.0f} (trop tard)"
        if now - token.created_at > ts.max_entry_age_seconds:
            return "token trop vieux pour une entrée rapide"
        if token.dev_buy_pct >= ts.max_dev_buy_pct:
            return f"le dev détient {token.dev_buy_pct:.1f}% (≥ {ts.max_dev_buy_pct:g}%)"
        holders = getattr(report, "holders", None)
        if holders is not None and holders.top10_pct >= ts.max_top10_pct:
            return f"top 10 holders {holders.top10_pct:.0f}% (≥ {ts.max_top10_pct:g}%)"
        return None

    async def on_signal(self, token: NewToken, report: TokenReport, stats: TradeStats | None,
                        alert_msg_id: int | None = None) -> str | None:
        """Appelé à chaque alerte du radar. Retourne la raison si on n'achète pas."""
        mint = token.mint
        if not self.enabled:
            return "trading désactivé"
        if self.holds(mint):
            return "déjà en position"
        why = self.entry_rejection(token, report, stats)
        if why:
            log.info("Trader : pas d'achat de $%s (%s)", token.symbol, why)
            return why
        mcap = stats.last_mcap if stats and stats.last_mcap else token.market_cap_sol
        self.prices[mint] = mcap / PUMP_TOTAL_SUPPLY

        self.pending.add(mint)  # réserve la place avant toute attente
        try:
            cash = await self.available_cash()
            equity = self.equity_from(cash)
            if self.mode == "live" and cash > self.ts.wallet_max_balance_sol:
                self.enabled = False
                self._save()
                await self._notify(
                    f"⛔ Le wallet contient {cash:.3f} SOL, plus que la limite "
                    f"({self.ts.wallet_max_balance_sol} SOL). Trading coupé par sécurité : "
                    "utilise un wallet dédié avec peu d'argent.")
                return "wallet trop rempli"
            halted = self.guard.update_equity(equity)
            if halted:
                await self._notify(f"🛑 <b>Trading arrêté</b> : {escape(halted)}")
            ok, why = self.guard.can_open(equity, len(self.positions) + len(self.pending) - 1)
            if not ok:
                log.info("Trader : pas d'achat de $%s (%s)", token.symbol, why)
                return why
            size = self.guard.size(cash)
            if size is None:
                await self._warn_once("small", (
                    f"⚠️ Capital trop faible ({cash:.4f} SOL) pour couvrir une position, "
                    "la réserve de frais et le loyer du compte token. Je ne trade plus."))
                return "capital trop faible"
            if self.feed:
                await self.feed.subscribe_trades([mint])
            fill = await self.ex.buy(mint, size)
        finally:
            self.pending.discard(mint)

        if not fill.ok:
            await self._notify(f"❌ Achat de ${escape(token.symbol)} échoué : {escape(fill.error)}",
                               reply_to=alert_msg_id)
            if self.release:
                await self.release(mint)
            return fill.error
        if self.mode == "paper":
            self.cash -= fill.sol
        now = time.time()
        price_now = self.prices.get(mint, fill.price)
        pos = Position(mint=mint, name=token.name, symbol=token.symbol, creator=token.creator,
                       entry_sol=fill.sol, tokens=fill.tokens, entry_price=fill.price,
                       opened_at=now, peak_price=price_now, last_price=price_now,
                       last_trade_at=now, alert_msg_id=alert_msg_id)
        self.positions[mint] = pos
        self._save()
        ts = self.ts
        await self._notify(
            f"🟢 <b>ACHAT</b> {self.tag} — <b>${escape(token.symbol)}</b>\n"
            f"Mise : {fill.sol:.4f} SOL{self._usd(fill.sol)} · market cap {mcap:.0f} SOL\n"
            f"Plan : stop-loss -{ts.stop_loss_pct:.0f}% · vente de {ts.take_profit_fraction:.0%} à "
            f"+{ts.take_profit_pct:.0f}% · trailing {ts.trailing_stop_pct:.0f}% · sortie si le dev vend\n"
            + (f'<a href="https://solscan.io/tx/{fill.signature}">transaction</a>\n'
               if self.mode == "live" else "")
            + links(mint),
            reply_to=alert_msg_id)
        self._check_exits(pos)  # le prix a pu bouger pendant l'exécution
        return None

    # ----------------------------------------------------------------- sorties
    def on_trade(self, trade: Trade) -> None:
        pos = self.positions.get(trade.mint)
        if trade.market_cap_sol > 0 and (pos or trade.mint in self.pending):
            self.prices[trade.mint] = trade.market_cap_sol / PUMP_TOTAL_SUPPLY
        if not pos:
            return
        pos.last_price = self.prices[trade.mint]
        pos.peak_price = max(pos.peak_price, pos.last_price)
        pos.last_trade_at = time.time()
        if not trade.is_buy and trade.trader == pos.creator:
            self._exit_soon(pos, 1.0, "le dev vend")
        else:
            self._check_exits(pos)

    def exit_reason(self, pos: Position, now: float | None = None) -> tuple[float, str] | None:
        now = now or time.time()
        ts, g = self.ts, pos.gain_pct
        if g <= -ts.stop_loss_pct:
            return 1.0, f"stop-loss ({g:+.0f}%)"
        if not pos.tp_done and g >= ts.take_profit_pct:
            return ts.take_profit_fraction, f"prise de profit ({g:+.0f}%)"
        if (pos.tp_done or pos.peak_gain_pct >= ts.trailing_arm_pct) and \
                pos.last_price <= pos.peak_price * (1 - ts.trailing_stop_pct / 100):
            return 1.0, f"trailing stop (plus haut {pos.peak_gain_pct:+.0f}%, maintenant {g:+.0f}%)"
        if now - pos.opened_at > ts.time_stop_minutes * 60 and g < ts.time_stop_min_gain_pct:
            return 1.0, f"pas de décollage en {ts.time_stop_minutes:.0f} min ({g:+.0f}%)"
        if now - pos.last_trade_at > ts.stale_minutes * 60:
            return 1.0, f"plus aucun échange depuis {ts.stale_minutes:.0f} min"
        return None

    def _check_exits(self, pos: Position) -> None:
        if pos.closing:
            return
        decision = self.exit_reason(pos)
        if decision:
            self._exit_soon(pos, *decision)

    def _exit_soon(self, pos: Position, fraction: float, reason: str) -> None:
        if pos.closing:
            return
        pos.closing = True
        self.spawn(self._exit(pos, fraction, reason))

    async def _exit(self, pos: Position, fraction: float, reason: str) -> None:
        retry = False
        try:
            full = fraction >= 0.999
            tokens = pos.tokens if full else pos.tokens * fraction
            fill = await self.ex.sell(pos.mint, tokens, 1.0 if full else fraction)
            if not fill.ok:
                pos.sell_failures += 1
                n = pos.sell_failures
                if n <= 3 or n % 10 == 0:
                    await self._notify(
                        f"❌ Vente de ${escape(pos.symbol)} échouée ({escape(reason)}) : "
                        f"{escape(fill.error)}" + (" — je réessaie." if n < 3 else
                                                   " — je continue d'essayer, <b>vends aussi à la main</b> : "
                                                   + links(pos.mint)),
                        reply_to=pos.alert_msg_id)
                retry = True
                return
            pos.sell_failures = 0
            pos.realized_sol += fill.sol
            pos.tokens = max(0.0, pos.tokens - fill.tokens)
            if self.mode == "paper":
                self.cash += fill.sol
            if full or pos.tokens <= 0:
                await self._close(pos, reason, fill.signature)
            else:
                pos.tp_done = True
                self._save()
                await self._notify(
                    f"💰 <b>VENTE PARTIELLE</b> {self.tag} — ${escape(pos.symbol)} — {escape(reason)}\n"
                    f"Reçu : {fill.sol:.4f} SOL{self._usd(fill.sol)} · le reste suit un trailing stop",
                    reply_to=pos.alert_msg_id)
        finally:
            pos.closing = False
            if retry:
                await asyncio.sleep(3)
                if pos.mint in self.positions:
                    self._exit_soon(pos, fraction, reason)

    async def _close(self, pos: Position, reason: str, signature: str = "") -> None:
        self.positions.pop(pos.mint, None)
        pnl = pos.realized_sol - pos.entry_sol
        pct = pnl / pos.entry_sol * 100 if pos.entry_sol else 0.0
        self.storage.add_trade(mode=self.mode, mint=pos.mint, symbol=pos.symbol, name=pos.name,
                               opened_at=pos.opened_at, entry_sol=pos.entry_sol,
                               exit_sol=pos.realized_sol, pnl_sol=pnl, reason=reason)
        cooldown = self.guard.record_close(pnl)
        cash = await self._safe_cash()
        if cash is not None:
            halted = self.guard.update_equity(self.equity_from(cash))
        else:
            halted = None
        self._save()
        emoji = "✅" if pnl >= 0 else "🔴"
        lines = [f"{emoji} <b>VENTE</b> {self.tag} — ${escape(pos.symbol)} — {escape(reason)}",
                 f"Reçu au total : {pos.realized_sol:.4f} SOL · résultat "
                 f"{pnl:+.4f} SOL ({pct:+.0f}%){self._usd(abs(pnl))}"]
        if cash is not None:
            lines.append(f"Capital : {self.equity_from(cash):.4f} SOL{self._usd(self.equity_from(cash))}")
        if signature and self.mode == "live":
            lines.append(f'<a href="https://solscan.io/tx/{signature}">transaction</a>')
        if cooldown:
            lines.append(f"⏸ {escape(cooldown)}")
        if halted:
            lines.append(f"🛑 <b>Trading arrêté</b> : {escape(halted)}")
        await self._notify("\n".join(lines), reply_to=pos.alert_msg_id)
        if self.mode == "live":
            self.spawn(self.ex.close_account(pos.mint))
        if self.release:
            await self.release(pos.mint)

    async def _safe_cash(self) -> float | None:
        try:
            return await self.available_cash()
        except Exception as e:  # noqa: BLE001 - le RPC peut être indisponible
            log.warning("Solde indisponible : %s", e)
            return None

    # ------------------------------------------------------------------ boucle
    async def run(self, every: float = 5.0) -> None:
        if self.feed and self.positions:
            await self.feed.subscribe_trades(list(self.positions))
        last_equity_check = 0.0
        while True:
            for pos in list(self.positions.values()):
                self._check_exits(pos)
            now = time.time()
            if now - last_equity_check > 60:
                last_equity_check = now
                halted = None
                cash = await self._safe_cash()
                if cash is not None:
                    halted = self.guard.update_equity(self.equity_from(cash))
                    if halted:
                        await self._notify(f"🛑 <b>Trading arrêté</b> : {escape(halted)}")
                if self.positions or halted:
                    self._save()
            await asyncio.sleep(every)

    # --------------------------------------------------------------- commandes
    async def status_text(self) -> str:
        cash = await self._safe_cash()
        st = self.guard.state
        stats = self.storage.trade_stats(self.mode)
        lines = [f"📊 <b>Trading {self.tag}</b> — {'actif' if self.enabled else 'désactivé'}"]
        if cash is not None:
            eq = self.equity_from(cash)
            lines.append(f"Capital : {eq:.4f} SOL{self._usd(eq)} · disponible {cash:.4f} SOL")
            if self.mode == "paper":
                lines.append(f"Départ : {self.ts.paper_start_sol:.4f} SOL → x{eq / self.ts.paper_start_sol:.2f}")
        if st.halted:
            lines.append(f"🛑 Arrêté : {escape(st.halt_reason)}")
        elif st.cooldown_until > time.time():
            lines.append(f"⏸ Pause encore {(st.cooldown_until - time.time()) / 60:.0f} min")
        lines.append(f"Positions ouvertes : {len(self.positions)}")
        if stats["count"]:
            lines.append(f"Trades : {stats['count']} · gagnants {stats['wins']}/{stats['count']} · "
                         f"résultat total {stats['pnl']:+.4f} SOL{self._usd(abs(stats['pnl']))}")
        return "\n".join(lines)

    def positions_text(self) -> str:
        if not self.positions:
            return "Aucune position ouverte."
        lines = ["📂 <b>Positions</b>"]
        for p in self.positions.values():
            age = (time.time() - p.opened_at) / 60
            lines.append(f"<b>${escape(p.symbol)}</b> {p.gain_pct:+.0f}% (plus haut {p.peak_gain_pct:+.0f}%) · "
                         f"mise {p.entry_sol:.4f} SOL · valeur {p.value:.4f} SOL · {age:.0f} min\n"
                         f"<code>{p.mint}</code>")
        return "\n".join(lines)

    def journal_text(self) -> str:
        rows = self.storage.recent_trades(self.mode, 10)
        if not rows:
            return "Aucun trade clôturé pour l'instant."
        lines = [f"📒 <b>Derniers trades</b> ({self.tag})"]
        for r in rows:
            pct = r["pnl_sol"] / r["entry_sol"] * 100 if r["entry_sol"] else 0
            lines.append(f"{'✅' if r['pnl_sol'] >= 0 else '🔴'} ${escape(r['symbol'])} "
                         f"{r['pnl_sol']:+.4f} SOL ({pct:+.0f}%) — {escape(r['reason'])}")
        return "\n".join(lines)

    async def handle_command(self, cmd: str, arg: str) -> str | None:
        if cmd == "/trading":
            if arg.lower() in ("on", "start", "marche"):
                cash = await self._safe_cash()
                self.enabled = True
                if cash is not None:
                    self.guard.resume(self.equity_from(cash))
                self._warned.discard("small")
                self._save()
                return "▶️ Trading activé.\n" + await self.status_text()
            if arg.lower() in ("off", "stop", "arret", "arrêt"):
                self.enabled = False
                self._save()
                return ("⏹ Trading désactivé : plus aucun nouvel achat. "
                        "Les positions ouvertes restent gérées (/vendre tout pour tout couper).")
            return await self.status_text()
        if cmd == "/positions":
            return self.positions_text()
        if cmd == "/journal":
            return self.journal_text()
        if cmd == "/vendre":
            targets = (list(self.positions.values()) if arg.lower() in ("tout", "all")
                       else [p for p in self.positions.values() if p.mint == arg or
                             p.symbol.lower() == arg.lower().lstrip("$")])
            if not targets:
                return "Position introuvable (voir /positions). Usage : /vendre tout | /vendre $TICKER"
            for p in targets:
                self._exit_soon(p, 1.0, "vente manuelle")
            return f"Vente lancée pour {len(targets)} position(s)."
        return None
