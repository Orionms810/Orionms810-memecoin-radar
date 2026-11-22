"""Le trader automatique : entre sur les meilleures alertes, gère les sorties et
applique les coupe-circuits de risque.

Sorties en deux temps :
- avant x2 : protection serrée (stop-loss, trailing, sortie si ça ne décolle pas) ;
- à x2 : la moitié est vendue, la mise est récupérée. Le reste (« moonbag ») peut
  courir : 2e palier à x5, trailing large, et sortie quand les vendeurs prennent
  le dessus. Si le token gradue, son prix est suivi sur DexScreener.
Après chaque vente, le prix est encore suivi 2 h (/sorties) pour vérifier avec des
chiffres si le bot sort trop tôt."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass
from html import escape
from typing import Any, Awaitable, Callable

from ..models import (PUMP_INITIAL_REAL_TOKENS, PUMP_INITIAL_VIRTUAL_TOKENS, PUMP_TOTAL_SUPPLY,
                      NewToken, Trade, TradeStats)
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
    bonding_curve: str = ""
    graduated: bool = False  # bonding curve terminée : le token se trade sur PumpSwap
    tp2_done: bool = False
    source: str = ""  # pourquoi on a acheté (alerte, CA posté par..., créateur suivi...)

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


@dataclass
class AfterWatch:
    """Suivi du prix d'un token après sa vente (le bot est-il sorti trop tôt ?)."""
    trade_id: int
    mint: str
    symbol: str
    bonding_curve: str
    entry_price: float
    exit_price: float
    closed_at: float
    max_price: float
    last_price: float
    graduated: bool = False


def _x(pct: float) -> str:
    """+100 % -> « x2 »."""
    return f"x{1 + pct / 100:g}"


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
        # Lecture des courbes (trades manqués, graduation) et prix DexScreener après graduation
        self.fetch_curves: Callable[[list[str]], Awaitable[dict[str, Any]]] | None = None
        self.dex_price: Callable[[str], Awaitable[float | None]] | None = None
        self.own_wallet = str(getattr(executor, "pubkey", "") or "")  # nos ventes ≠ pression vendeuse
        self.flows: dict[str, deque[tuple[float, bool, float]]] = {}
        self.after: dict[int, AfterWatch] = {}
        self._last_curve_check = self._last_dex_check = self._last_after_check = 0.0

        self.enabled = bool(storage.kv_get(self._k("enabled"), True))
        self.cash = float(storage.kv_get("paper_cash", ts.paper_start_sol))
        self.guard = RiskGuard(ts, GuardState(**(storage.kv_get(self._k("guard"), {}) or {})))
        for d in storage.kv_get(self._k("positions"), []) or []:
            pos = Position(**d)
            self.positions[pos.mint] = pos
            self.prices[pos.mint] = pos.last_price
        for d in storage.kv_get(self._k("after"), []) or []:
            w = AfterWatch(**d)
            self.after[w.trade_id] = w

    # ------------------------------------------------------------ utilitaires
    def _k(self, name: str) -> str:
        return f"trading:{self.mode}:{name}"

    def _save(self) -> None:
        self.storage.kv_set(self._k("positions"), [p.to_dict() for p in self.positions.values()])
        self.storage.kv_set(self._k("guard"), self.guard.state.to_dict())
        self.storage.kv_set(self._k("enabled"), self.enabled)
        self.storage.kv_set(self._k("after"), [asdict(w) for w in self.after.values()])
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

    def _slots_used(self) -> int:
        """Positions qui comptent dans la limite : les moonbags (mise déjà récupérée)
        ne bloquent pas les nouveaux achats, dans la limite de max_moonbags."""
        bags = sum(1 for p in self.positions.values() if p.tp_done)
        return len(self.positions) - bags + max(0, bags - self.ts.max_moonbags)

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
        return await self._buy(token, mcap, alert_msg_id)

    async def snipe(self, token: NewToken, mcap: float, reason: str, alert_msg_id: int | None = None,
                    *, graduated: bool = False, max_mcap_sol: float = 2000.0,
                    extra_slots: int = 1) -> str | None:
        """Achat immédiat (CA posté par un compte de confiance, créateur suivi) : pas
        d'observation ni de filtres d'alerte, la vitesse passe avant. Les coupe-circuits
        restent actifs, sauf la pause après des pertes d'affilée."""
        if not self.enabled:
            return "trading désactivé"
        if self.holds(token.mint):
            return "déjà en position"
        if mcap <= 0:
            return "prix inconnu"
        if mcap > max_mcap_sol:
            return f"market cap {mcap:.0f} SOL > {max_mcap_sol:.0f} (trop tard)"
        return await self._buy(token, mcap, alert_msg_id, source=reason, graduated=graduated,
                               extra_slots=extra_slots, ignore_cooldown=True)

    async def _buy(self, token: NewToken, mcap: float, alert_msg_id: int | None, *,
                   source: str = "", graduated: bool = False, extra_slots: int = 0,
                   ignore_cooldown: bool = False) -> str | None:
        mint = token.mint
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
            ok, why = self.guard.can_open(equity, self._slots_used() + len(self.pending) - 1,
                                          extra_slots=extra_slots, ignore_cooldown=ignore_cooldown)
            if not ok:
                log.info("Trader : pas d'achat de $%s (%s)", token.symbol, why)
                return why
            size = self.guard.size(cash)
            if size is None:
                await self._warn_once("small", (
                    f"⚠️ Capital trop faible ({cash:.4f} SOL) pour couvrir une position, "
                    "la réserve de frais et le loyer du compte token. Je ne trade plus."))
                return "capital trop faible"
            if self.feed and not graduated:
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
                       last_trade_at=now, alert_msg_id=alert_msg_id,
                       bonding_curve=token.bonding_curve, graduated=graduated, source=source)
        self.positions[mint] = pos
        self._save()
        await self._notify(
            f"🟢 <b>ACHAT</b> {self.tag} — <b>${escape(token.symbol)}</b>\n"
            + (f"⚡ {escape(source)}\n" if source else "")
            + f"Mise : {fill.sol:.4f} SOL{self._usd(fill.sol)} · market cap {mcap:.0f} SOL\n"
            f"Plan : {self.plan_text()}\n"
            + (f'<a href="https://solscan.io/tx/{fill.signature}">transaction</a>\n'
               if self.mode == "live" else "")
            + links(mint),
            reply_to=alert_msg_id)
        self._check_exits(pos)  # le prix a pu bouger pendant l'exécution
        return None

    # ----------------------------------------------------------------- sorties
    def plan_text(self) -> str:
        ts = self.ts
        parts = [f"stop-loss -{ts.stop_loss_pct:.0f}%",
                 f"à {_x(ts.take_profit_pct)} vente de {ts.take_profit_fraction:.0%} (mise récupérée)"]
        if ts.take_profit2_pct > 0:
            parts.append(f"à {_x(ts.take_profit2_pct)} vente de {ts.take_profit2_fraction:.0%} du reste")
        parts.append(f"le reste court (trailing {ts.moonbag_trailing_pct:.0f}%"
                     + (", sortie si les vendeurs prennent le dessus" if ts.reversal_window_seconds > 0
                        else "") + ")")
        parts.append("sortie immédiate si le dev vend")
        return " · ".join(parts)

    def _set_price(self, pos: Position, price: float, now: float) -> None:
        self.prices[pos.mint] = price
        pos.last_price = price
        pos.peak_price = max(pos.peak_price, price)
        pos.last_trade_at = now

    def on_trade(self, trade: Trade) -> None:
        pos = self.positions.get(trade.mint)
        if trade.market_cap_sol > 0 and (pos or trade.mint in self.pending):
            self.prices[trade.mint] = trade.market_cap_sol / PUMP_TOTAL_SUPPLY
        if not pos:
            return
        now = time.time()
        if not self.own_wallet or trade.trader != self.own_wallet:  # nos propres ventes ne comptent pas
            flow = self.flows.setdefault(pos.mint, deque())
            flow.append((now, trade.is_buy, trade.sol_amount))
            while flow and now - flow[0][0] > 300:
                flow.popleft()
        if trade.mint in self.prices:
            self._set_price(pos, self.prices[trade.mint], now)
        if trade.v_tokens is not None and \
                PUMP_INITIAL_VIRTUAL_TOKENS - trade.v_tokens >= PUMP_INITIAL_REAL_TOKENS * 0.999:
            self._mark_graduated(pos)
        if not trade.is_buy and trade.trader == pos.creator:
            self._exit_soon(pos, 1.0, "le dev vend")
        else:
            self._check_exits(pos)

    def _mark_graduated(self, pos: Position) -> None:
        if pos.graduated:
            return
        pos.graduated = True
        self._save()
        self.spawn(self._notify(
            f"🎓 <b>${escape(pos.symbol)} a gradué</b> (bonding curve terminée, {pos.gain_pct:+.0f}%) : "
            "il se trade maintenant sur PumpSwap. Je suis son prix sur DexScreener et la position "
            "continue.", reply_to=pos.alert_msg_id))

    def sell_pressure(self, pos: Position, now: float | None = None) -> tuple[float, float]:
        """SOL achetés et vendus par les autres sur la fenêtre récente."""
        now = now or time.time()
        buys = sells = 0.0
        for t, is_buy, sol in self.flows.get(pos.mint, ()):
            if now - t <= self.ts.reversal_window_seconds:
                if is_buy:
                    buys += sol
                else:
                    sells += sol
        return buys, sells

    def _reversal(self, pos: Position, now: float) -> str | None:
        ts = self.ts
        if ts.reversal_window_seconds <= 0 or pos.graduated:
            return None
        drop = (1 - pos.last_price / pos.peak_price) * 100 if pos.peak_price else 0.0
        if drop < ts.reversal_drop_pct:
            return None
        buys, sells = self.sell_pressure(pos, now)
        if sells >= ts.reversal_min_sell_sol and sells >= ts.reversal_sell_ratio * buys:
            return (f"les vendeurs prennent le dessus : {sells:.1f} SOL vendus contre {buys:.1f} "
                    f"achetés en {ts.reversal_window_seconds:.0f} s, -{drop:.0f}% depuis le plus haut "
                    f"({pos.gain_pct:+.0f}%)")
        return None

    def exit_reason(self, pos: Position, now: float | None = None) -> tuple[float, str] | None:
        now = now or time.time()
        ts, g = self.ts, pos.gain_pct
        if g <= -ts.stop_loss_pct:
            return 1.0, f"stop-loss ({g:+.0f}%)"
        if not pos.tp_done and g >= ts.take_profit_pct:
            return ts.take_profit_fraction, f"prise de profit ({g:+.0f}%)"
        if pos.tp_done and not pos.tp2_done and ts.take_profit2_pct > 0 and g >= ts.take_profit2_pct:
            return ts.take_profit2_fraction, f"2e prise de profit ({g:+.0f}%)"
        trail = ts.moonbag_trailing_pct if pos.tp_done else ts.trailing_stop_pct
        if (pos.tp_done or pos.peak_gain_pct >= ts.trailing_arm_pct) and \
                pos.last_price <= pos.peak_price * (1 - trail / 100):
            return 1.0, f"trailing stop (plus haut {pos.peak_gain_pct:+.0f}%, maintenant {g:+.0f}%)"
        if pos.tp_done:  # moonbag : la mise est déjà récupérée, on laisse courir
            why = self._reversal(pos, now)
            if why:
                return 1.0, why
            if ts.moonbag_max_hours > 0 and now - pos.opened_at > ts.moonbag_max_hours * 3600:
                return 1.0, f"durée maximale de garde ({ts.moonbag_max_hours:.0f} h, {g:+.0f}%)"
            if not pos.graduated and now - pos.last_trade_at > ts.moonbag_stale_minutes * 60:
                return 1.0, f"plus aucun échange depuis {ts.moonbag_stale_minutes:.0f} min"
            return None
        if now - pos.opened_at > ts.time_stop_minutes * 60 and g < ts.time_stop_min_gain_pct:
            return 1.0, f"pas de décollage en {ts.time_stop_minutes:.0f} min ({g:+.0f}%)"
        if not pos.graduated and now - pos.last_trade_at > ts.stale_minutes * 60:
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
                first = not pos.tp_done
                pos.tp2_done = pos.tp2_done or not first
                pos.tp_done = True
                self._save()
                ts = self.ts
                recovered = pos.realized_sol / pos.entry_sol * 100 if pos.entry_sol else 0
                nxt = (f"prochain palier à {_x(ts.take_profit2_pct)}, "
                       if first and ts.take_profit2_pct > 0 else "")
                await self._notify(
                    f"💰 <b>VENTE PARTIELLE</b> {self.tag} — ${escape(pos.symbol)} — {escape(reason)}\n"
                    f"Reçu : {fill.sol:.4f} SOL{self._usd(fill.sol)} · déjà encaissé {recovered:.0f}% de "
                    f"la mise\nLe reste court : {nxt}trailing {ts.moonbag_trailing_pct:.0f}% depuis le "
                    "plus haut, sortie si les vendeurs prennent le dessus ou si le dev vend",
                    reply_to=pos.alert_msg_id)
        finally:
            pos.closing = False
            if retry:
                await asyncio.sleep(3)
                if pos.mint in self.positions:
                    self._exit_soon(pos, fraction, reason)

    async def _close(self, pos: Position, reason: str, signature: str = "") -> None:
        self.positions.pop(pos.mint, None)
        self.flows.pop(pos.mint, None)
        pnl = pos.realized_sol - pos.entry_sol
        pct = pnl / pos.entry_sol * 100 if pos.entry_sol else 0.0
        entry = pos.entry_price or 1.0
        trade_id = self.storage.add_trade(
            mode=self.mode, mint=pos.mint, symbol=pos.symbol, name=pos.name,
            opened_at=pos.opened_at, entry_sol=pos.entry_sol, exit_sol=pos.realized_sol,
            pnl_sol=pnl, reason=reason, exit_x=round(pos.last_price / entry, 4),
            peak_x=round(pos.peak_price / entry, 4), graduated=int(pos.graduated),
            source=pos.source)
        if self.ts.after_watch_minutes > 0 and trade_id:
            self.after[trade_id] = AfterWatch(
                trade_id=trade_id, mint=pos.mint, symbol=pos.symbol,
                bonding_curve=pos.bonding_curve, entry_price=entry, exit_price=pos.last_price,
                closed_at=time.time(), max_price=pos.last_price, last_price=pos.last_price,
                graduated=pos.graduated)
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

    # ------------------------------------------------------- prix hors trades
    async def refresh_prices(self, now: float | None = None) -> None:
        """Relit la courbe des positions sans trade récent (trades manqués, graduation)
        et le prix DexScreener des positions graduées."""
        now = now or time.time()
        ts = self.ts
        if self.fetch_curves and now - self._last_curve_check >= ts.curve_check_seconds:
            silent = [p for p in self.positions.values() if p.bonding_curve and not p.graduated
                      and now - p.last_trade_at >= ts.curve_check_seconds]
            if silent:
                self._last_curve_check = now
                curves = await self.fetch_curves([p.bonding_curve for p in silent]) or {}
                for p in silent:
                    c = curves.get(p.bonding_curve)
                    if c is None or p.mint not in self.positions:
                        continue
                    if c.complete:
                        self._mark_graduated(p)
                        continue
                    price = c.market_cap_sol / PUMP_TOTAL_SUPPLY
                    if price > 0 and (p.last_price <= 0 or abs(price / p.last_price - 1) > 1e-4):
                        self._set_price(p, price, now)  # des trades nous ont échappé
        if self.dex_price and now - self._last_dex_check >= ts.curve_check_seconds:
            graduated = [p for p in self.positions.values() if p.graduated]
            if graduated:
                self._last_dex_check = now
                for p in graduated:
                    price = await self.dex_price(p.mint)
                    if price and price > 0 and p.mint in self.positions and \
                            (p.last_price <= 0 or abs(price / p.last_price - 1) > 1e-4):
                        self._set_price(p, price, now)

    async def watch_after(self, now: float | None = None) -> None:
        """Suit le prix des tokens vendus pendant after_watch_minutes."""
        if not self.after:
            return
        now = now or time.time()
        on_curve = [w for w in self.after.values() if w.bonding_curve and not w.graduated]
        curves: dict[str, Any] = {}
        if on_curve and self.fetch_curves:
            curves = await self.fetch_curves([w.bonding_curve for w in on_curve]) or {}
        for key, w in list(self.after.items()):
            price = None
            c = None if w.graduated else curves.get(w.bonding_curve)
            if c is not None:
                price = c.market_cap_sol / PUMP_TOTAL_SUPPLY
                w.graduated = bool(c.complete)
            elif w.graduated and self.dex_price:
                price = await self.dex_price(w.mint)
            if price and price > 0:
                w.last_price = price
                w.max_price = max(w.max_price, price)
            if now - w.closed_at >= self.ts.after_watch_minutes * 60:
                self.storage.update_trade(w.trade_id, {
                    "after_max_x": round(w.max_price / w.entry_price, 4),
                    "after_end_x": round(w.last_price / w.entry_price, 4),
                    "graduated": int(w.graduated)})
                del self.after[key]
        self.storage.kv_set(self._k("after"), [asdict(w) for w in self.after.values()])

    # ------------------------------------------------------------------ boucle
    async def run(self, every: float = 5.0) -> None:
        if self.feed and self.positions:
            await self.feed.subscribe_trades(list(self.positions))
        last_equity_check = 0.0
        while True:
            now = time.time()
            try:
                await self.refresh_prices(now)
            except Exception:  # noqa: BLE001 - une lecture ratée ne doit pas tout arrêter
                log.exception("Trader : lecture des prix impossible")
            for pos in list(self.positions.values()):
                self._check_exits(pos)
            if now - self._last_after_check >= 60:
                self._last_after_check = now
                try:
                    await self.watch_after(now)
                except Exception:  # noqa: BLE001
                    log.exception("Trader : suivi après vente impossible")
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
        bags = sum(1 for p in self.positions.values() if p.tp_done)
        lines.append(f"Positions ouvertes : {len(self.positions)}"
                     + (f" (dont {bags} moonbag(s), mise déjà récupérée)" if bags else ""))
        if stats["count"]:
            lines.append(f"Trades : {stats['count']} · gagnants {stats['wins']}/{stats['count']} · "
                         f"résultat total {stats['pnl']:+.4f} SOL{self._usd(abs(stats['pnl']))}")
        return "\n".join(lines)

    def positions_text(self) -> str:
        if not self.positions:
            return "Aucune position ouverte."
        lines = ["📂 <b>Positions</b>"]
        now = time.time()
        for p in self.positions.values():
            age = (now - p.opened_at) / 60
            stage = ("🌙 moonbag : mise récupérée, le reste court" if p.tp_done
                     else f"avant {_x(self.ts.take_profit_pct)} : protection serrée")
            if p.graduated:
                stage += " · 🎓 gradué (prix DexScreener)"
            elif self.flows.get(p.mint):
                buys, sells = self.sell_pressure(p, now)
                stage += f" · dernière minute : {buys:.1f} SOL achetés / {sells:.1f} vendus"
            lines.append(f"<b>${escape(p.symbol)}</b> {p.gain_pct:+.0f}% (plus haut {p.peak_gain_pct:+.0f}%) · "
                         f"mise {p.entry_sol:.4f} SOL · encaissé {p.realized_sol:.4f} · valeur "
                         f"{p.value:.4f} SOL · {age:.0f} min\n{stage}\n<code>{p.mint}</code>")
        return "\n".join(lines)

    def exits_text(self) -> str:
        """Ce que les tokens sont devenus après la vente : le bot sort-il trop tôt ?"""
        hours = self.ts.after_watch_minutes / 60
        rows = [r for r in self.storage.trade_rows(self.mode) if r["exit_x"] is not None]
        if not rows:
            return ("Aucune vente suivie pour l'instant. Après chaque vente, je surveille le token "
                    f"encore {hours:g} h pour voir s'il fallait garder plus longtemps.")
        lines = [f"🔎 <b>Et après la vente ?</b> (prix suivi {hours:g} h après chaque sortie)",
                 "<i>x1 = ton prix d'achat · x3 = trois fois ton prix d'achat</i>"]
        early = good = followed = 0
        for r in rows:
            if r["after_max_x"] is None:
                continue
            followed += 1
            if r["after_max_x"] >= 2 * r["exit_x"]:
                early += 1
            elif r["after_end_x"] <= 0.7 * r["exit_x"]:
                good += 1
        for r in rows[-10:][::-1]:
            pct = r["pnl_sol"] / r["entry_sol"] * 100 if r["entry_sol"] else 0
            line = (f"{'✅' if r['pnl_sol'] >= 0 else '🔴'} <b>${escape(r['symbol'])}</b> {pct:+.0f}% — "
                    f"vendu à x{r['exit_x']:.2f} (plus haut pendant la position x{r['peak_x']:.2f})\n"
                    f"   {escape(r['reason'])}")
            if r["after_max_x"] is None:
                line += "\n   après : suivi en cours…"
            else:
                verdict = ("⚠️ sorti trop tôt" if r["after_max_x"] >= 2 * r["exit_x"] else
                           "👍 bien sorti" if r["after_end_x"] <= 0.7 * r["exit_x"] else "≈ pareil")
                line += (f"\n   après : plus haut x{r['after_max_x']:.2f}, x{r['after_end_x']:.2f} "
                         f"au bout de {hours:g} h{' 🎓' if r['graduated'] else ''} — {verdict}")
            lines.append(line)
        if followed:
            lines.append("")
            lines.append(f"<b>Bilan sur {followed} vente(s) suivie(s)</b> : {early} trop tôt (le token a "
                         f"ensuite doublé par rapport au prix de vente) · {good} bien sortie(s) (il valait "
                         f"30 % de moins ou pire {hours:g} h après) · {followed - early - good} sans grande "
                         "différence.")
            lines.append("<i>Il faut une vingtaine de ventes pour conclure.</i>")
        return "\n".join(lines)

    def journal_text(self) -> str:
        rows = self.storage.recent_trades(self.mode, 10)
        if not rows:
            return "Aucun trade clôturé pour l'instant."
        lines = [f"📒 <b>Derniers trades</b> ({self.tag})"]
        for r in rows:
            pct = r["pnl_sol"] / r["entry_sol"] * 100 if r["entry_sol"] else 0
            src = r["source"] if "source" in r.keys() and r["source"] else ""
            lines.append(f"{'✅' if r['pnl_sol'] >= 0 else '🔴'} ${escape(r['symbol'])} "
                         f"{r['pnl_sol']:+.4f} SOL ({pct:+.0f}%) — {escape(r['reason'])}"
                         + (f" · ⚡ {escape(src)}" if src else ""))
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
        if cmd == "/sorties":
            return self.exits_text()
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
