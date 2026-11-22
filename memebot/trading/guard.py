"""Gestion du risque : taille des positions et coupe-circuits."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from .settings import TradingSettings


@dataclass
class GuardState:
    equity_peak: float = 0.0
    day: str = ""
    day_start_equity: float = 0.0
    consecutive_losses: int = 0
    cooldown_until: float = 0.0
    halted: bool = False
    halt_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class RiskGuard:
    def __init__(self, ts: TradingSettings, state: GuardState | None = None):
        self.ts = ts
        self.state = state or GuardState()

    def update_equity(self, equity: float, now: float | None = None) -> str | None:
        """À appeler régulièrement. Retourne une raison si un coupe-circuit vient de sauter."""
        now = now or time.time()
        st = self.state
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if day != st.day:
            st.day = day
            st.day_start_equity = equity
        st.equity_peak = max(st.equity_peak, equity)
        if (not st.halted and st.equity_peak > 0
                and equity <= st.equity_peak * (1 - self.ts.max_drawdown_pct / 100)):
            st.halted = True
            st.halt_reason = (f"perte de {self.ts.max_drawdown_pct:.0f}% depuis le plus haut "
                              f"({st.equity_peak:.4f} → {equity:.4f} SOL)")
            return st.halt_reason
        return None

    def can_open(self, equity: float, open_positions: int, now: float | None = None, *,
                 extra_slots: int = 0, ignore_cooldown: bool = False) -> tuple[bool, str]:
        now = now or time.time()
        st, ts = self.state, self.ts
        if st.halted:
            return False, f"trading arrêté : {st.halt_reason} (/trading on pour relancer)"
        if open_positions >= ts.max_open_positions + extra_slots:
            return False, "nombre maximum de positions ouvertes atteint"
        if now < st.cooldown_until and not ignore_cooldown:
            mins = (st.cooldown_until - now) / 60
            return False, f"pause après {ts.max_consecutive_losses} pertes d'affilée ({mins:.0f} min restantes)"
        if (st.day_start_equity > 0
                and equity <= st.day_start_equity * (1 - ts.daily_loss_limit_pct / 100)):
            return False, f"limite de perte du jour atteinte (-{ts.daily_loss_limit_pct:.0f}%)"
        return True, ""

    def size(self, available_sol: float) -> float | None:
        """Taille de la prochaine position, ou None si le capital est trop faible."""
        usable = available_sol - self.ts.reserve_sol
        if usable <= 0:
            return None
        size = min(self.ts.max_position_sol, usable * self.ts.position_pct)
        if size < self.ts.min_position_sol:
            # Petit capital : on prend le minimum s'il reste couvert par la réserve.
            if usable >= self.ts.min_position_sol:
                return self.ts.min_position_sol
            return None
        return size

    def record_close(self, pnl_sol: float, now: float | None = None) -> str | None:
        now = now or time.time()
        st = self.state
        if pnl_sol < 0:
            st.consecutive_losses += 1
            if st.consecutive_losses >= self.ts.max_consecutive_losses:
                st.consecutive_losses = 0
                st.cooldown_until = now + self.ts.cooldown_minutes * 60
                return f"{self.ts.max_consecutive_losses} pertes d'affilée : pause de {self.ts.cooldown_minutes:.0f} min"
        else:
            st.consecutive_losses = 0
        return None

    def resume(self, equity: float) -> None:
        """Relance manuelle (/trading on) après un arrêt."""
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.equity_peak = equity
        self.state.day_start_equity = equity  # la limite du jour repart d'ici
        self.state.cooldown_until = 0.0
        self.state.consecutive_losses = 0
