"""Lecture groupée des bonding curves pump.fun (état d'un token sans écouter ses trades).

Une seule requête RPC `getMultipleAccounts` donne l'état de 100 tokens : réserves,
SOL réellement entré, avancement de la courbe, graduation. Beaucoup moins de
données (donc de crédits RPC) que de recevoir chaque trade en direct.

Format vérifié dans l'IDL officiel de pump.fun (compte « BondingCurve »,
discriminant 17b7f83760d8ac60) : virtual_token_reserves (u64),
virtual_quote_reserves (u64, SOL), real_token_reserves (u64),
real_quote_reserves (u64, SOL), token_total_supply (u64), complete (bool), ...
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass

from .models import PUMP_INITIAL_REAL_TOKENS, PUMP_INITIAL_VIRTUAL_TOKENS

CURVE_DISCRIMINATOR = bytes.fromhex("17b7f83760d8ac60")


@dataclass
class CurveState:
    v_token_raw: int
    v_sol_lamports: int
    real_sol_lamports: int
    complete: bool

    @property
    def market_cap_sol(self) -> float:
        return self.v_sol_lamports * 1e6 / self.v_token_raw if self.v_token_raw else 0.0

    @property
    def real_sol(self) -> float:
        """SOL réellement entrés dans la courbe (achats - ventes, dev compris)."""
        return self.real_sol_lamports / 1e9

    @property
    def v_tokens(self) -> float:
        return self.v_token_raw / 1e6

    @property
    def progress_pct(self) -> float:
        sold = PUMP_INITIAL_VIRTUAL_TOKENS - self.v_tokens
        return max(0.0, min(100.0, sold / PUMP_INITIAL_REAL_TOKENS * 100))


def decode_curve(data: bytes) -> CurveState | None:
    if len(data) < 8 + 5 * 8 + 1 or data[:8] != CURVE_DISCRIMINATOR:
        return None
    v_tok, v_sol, _real_tok, real_sol, _supply = struct.unpack_from("<QQQQQ", data, 8)
    complete = data[48] == 1
    if v_tok <= 0:
        return None
    return CurveState(v_token_raw=v_tok, v_sol_lamports=v_sol, real_sol_lamports=real_sol,
                      complete=complete)


def decode_account_value(value: object) -> CurveState | None:
    """Décode un compte renvoyé par getMultipleAccounts (encoding base64)."""
    if not isinstance(value, dict):
        return None
    data = value.get("data")
    if not (isinstance(data, list) and data and isinstance(data[0], str)):
        return None
    try:
        return decode_curve(base64.b64decode(data[0]))
    except (ValueError, TypeError):
        return None
