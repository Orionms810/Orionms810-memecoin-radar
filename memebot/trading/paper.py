"""Exécution simulée (mode papier). Aucune dépendance lourde : marche partout,
y compris sur un petit Raspberry Pi."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable


@dataclass
class Fill:
    ok: bool
    sol: float = 0.0  # SOL dépensés (achat) ou reçus (vente), frais inclus
    tokens: float = 0.0  # tokens reçus (achat) ou vendus (vente)
    price: float = 0.0  # SOL par token, frais inclus
    signature: str = ""
    error: str = ""



class PaperExecutor:
    """Simulation : exécute au dernier prix connu, après un délai, avec des frais."""

    live = False

    def __init__(self, price_fn: Callable[[str], float | None], *, fee_pct: float,
                 tx_fee_sol: float, latency: float):
        self.price_fn = price_fn
        self.fee = fee_pct / 100
        self.tx_fee = tx_fee_sol
        self.latency = latency

    async def buy(self, mint: str, sol: float) -> Fill:
        await asyncio.sleep(self.latency)  # pendant ce temps, les autres achètent
        price = self.price_fn(mint)
        if not price:
            return Fill(False, error="prix inconnu")
        net = sol * (1 - self.fee) - self.tx_fee
        if net <= 0:
            return Fill(False, error="montant trop faible pour couvrir les frais")
        tokens = net / price
        return Fill(True, sol=sol, tokens=tokens, price=sol / tokens, signature="papier")

    async def sell(self, mint: str, tokens: float, fraction: float) -> Fill:
        await asyncio.sleep(self.latency)
        price = self.price_fn(mint)
        if not price:
            return Fill(False, error="prix inconnu")
        received = max(0.0, tokens * price * (1 - self.fee) - self.tx_fee)
        return Fill(True, sol=received, tokens=tokens, price=price, signature="papier")

    async def balance(self) -> float | None:
        return None

    async def close_account(self, mint: str) -> None:
        return None
