"""Exécution des ordres.

- PaperExecutor : simule les achats/ventes au prix du marché (argent virtuel).
- LiveExecutor  : vrais ordres via l'API « Local Transaction » de PumpPortal.
  La transaction est construite par PumpPortal, mais SIGNÉE ICI, sur ta
  machine : ta clé privée n'est jamais envoyée à personne.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

log = logging.getLogger(__name__)

TRADE_LOCAL_URL = "https://pumpportal.fun/api/trade-local"
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ATA_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
LAMPORTS = 1_000_000_000


@dataclass
class Fill:
    ok: bool
    sol: float = 0.0  # SOL dépensés (achat) ou reçus (vente), frais inclus
    tokens: float = 0.0  # tokens reçus (achat) ou vendus (vente)
    price: float = 0.0  # SOL par token, frais inclus
    signature: str = ""
    error: str = ""


class TradeError(Exception):
    pass


def load_keypair(secret: str) -> Keypair:
    """Accepte la clé au format base58 (export Phantom) ou tableau JSON (Solana CLI)."""
    secret = secret.strip()
    if secret.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(secret)))
    return Keypair.from_base58_string(secret)


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


class LiveExecutor:
    """Vrais ordres. À n'utiliser qu'avec un wallet dédié contenant peu d'argent."""

    live = True

    def __init__(self, http: httpx.AsyncClient, rpc_url: str, keypair: Keypair, *,
                 slippage_pct: float, priority_fee_sol: float, pool: str = "auto"):
        self.http = http
        self.rpc_url = rpc_url
        self.keypair = keypair
        self.pubkey = keypair.pubkey()
        self.slippage = slippage_pct
        self.priority_fee = priority_fee_sol
        self.pool = pool
        self._rpc_id = 0

    # ------------------------------------------------------------------- RPC
    async def _rpc(self, method: str, params: list[Any]) -> Any:
        self._rpc_id += 1
        r = await self.http.post(self.rpc_url, timeout=15, json={
            "jsonrpc": "2.0", "id": self._rpc_id, "method": method, "params": params})
        data = r.json()
        if "error" in data:
            raise TradeError(f"RPC {method} : {data['error'].get('message', data['error'])}")
        return data.get("result")

    async def balance(self) -> float:
        res = await self._rpc("getBalance", [str(self.pubkey), {"commitment": "confirmed"}])
        return int(res["value"]) / LAMPORTS

    async def token_balance(self, mint: str) -> float:
        res = await self._rpc("getTokenAccountsByOwner", [
            str(self.pubkey), {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        total = 0.0
        for acc in res.get("value", []):
            amount = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            total += float(amount.get("uiAmountString") or amount.get("uiAmount") or 0)
        return total

    async def _send(self, tx: VersionedTransaction) -> str:
        encoded = base64.b64encode(bytes(tx)).decode()
        return await self._rpc("sendTransaction", [encoded, {
            "encoding": "base64", "skipPreflight": False,
            "preflightCommitment": "confirmed", "maxRetries": 3}])

    async def _confirm(self, signature: str, timeout: float = 45.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            res = await self._rpc("getSignatureStatuses",
                                  [[signature], {"searchTransactionHistory": False}])
            status = (res.get("value") or [None])[0]
            if status:
                if status.get("err"):
                    raise TradeError(f"transaction échouée : {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return
            await asyncio.sleep(1.0)
        raise TradeError("transaction non confirmée à temps (vérifie sur Solscan)")

    async def _trade(self, action: str, mint: str, amount: float | str,
                     in_sol: bool) -> str:
        r = await self.http.post(TRADE_LOCAL_URL, timeout=15, data={
            "publicKey": str(self.pubkey),
            "action": action,
            "mint": mint,
            "amount": str(amount),
            "denominatedInSol": "true" if in_sol else "false",
            "slippage": str(self.slippage),
            "priorityFee": str(self.priority_fee),
            "pool": self.pool,
        })
        if r.status_code != 200:
            raise TradeError(f"PumpPortal HTTP {r.status_code} : {r.text[:200]}")
        unsigned = VersionedTransaction.from_bytes(r.content)
        if unsigned.message.account_keys[0] != self.pubkey:
            raise TradeError("transaction reçue pour un autre wallet : refusée")
        tx = VersionedTransaction(unsigned.message, [self.keypair])
        signature = await self._send(tx)
        await self._confirm(signature)
        return signature

    async def _token_balance_settled(self, mint: str, before: float) -> float:
        """Le RPC peut avoir un léger retard : on relit le solde quelques fois."""
        bal = before
        for _ in range(5):
            bal = await self.token_balance(mint)
            if bal != before:
                return bal
            await asyncio.sleep(1.0)
        return bal

    async def buy(self, mint: str, sol: float) -> Fill:
        try:
            sol0, tok0 = await self.balance(), await self.token_balance(mint)
            sig = await self._trade("buy", mint, round(sol, 6), in_sol=True)
            tok1 = await self._token_balance_settled(mint, tok0)
            sol1 = await self.balance()
        except (TradeError, httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            return Fill(False, error=str(e) or type(e).__name__)
        tokens, spent = tok1 - tok0, sol0 - sol1
        if tokens <= 0:
            return Fill(False, signature=sig, error="aucun token reçu")
        return Fill(True, sol=spent, tokens=tokens, price=spent / tokens, signature=sig)

    async def sell(self, mint: str, tokens: float, fraction: float) -> Fill:
        amount = "100%" if fraction >= 0.999 else f"{max(1, round(fraction * 100))}%"
        try:
            sol0, tok0 = await self.balance(), await self.token_balance(mint)
            if tok0 <= 0:
                return Fill(False, error="aucun token à vendre dans le wallet")
            sig = await self._trade("sell", mint, amount, in_sol=False)
            tok1 = await self._token_balance_settled(mint, tok0)
            sol1 = await self.balance()
        except (TradeError, httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            return Fill(False, error=str(e) or type(e).__name__)
        sold = tok0 - tok1
        received = max(0.0, sol1 - sol0)
        return Fill(True, sol=received, tokens=sold,
                    price=received / sold if sold else 0.0, signature=sig)

    # ------------------------------------------------- récupération du loyer
    def build_close_account_tx(self, mint: str, token_program: Pubkey,
                               blockhash: Hash) -> VersionedTransaction:
        mint_pk = Pubkey.from_string(mint)
        ata, _ = Pubkey.find_program_address(
            [bytes(self.pubkey), bytes(token_program), bytes(mint_pk)], ATA_PROGRAM)
        ix = Instruction(token_program, bytes([9]), [  # 9 = CloseAccount
            AccountMeta(ata, False, True),
            AccountMeta(self.pubkey, False, True),  # le loyer revient au wallet
            AccountMeta(self.pubkey, True, False),
        ])
        msg = MessageV0.try_compile(self.pubkey, [ix], [], blockhash)
        return VersionedTransaction(msg, [self.keypair])

    async def close_account(self, mint: str) -> None:
        """Ferme le compte token vide pour récupérer ~0,002 SOL de loyer (énorme sur 7 $)."""
        try:
            if await self.token_balance(mint) > 0:
                return
            info = await self._rpc("getAccountInfo", [mint, {"encoding": "base64"}])
            owner = Pubkey.from_string(info["value"]["owner"])
            if owner not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
                return
            bh = await self._rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
            tx = self.build_close_account_tx(mint, owner, Hash.from_string(bh["value"]["blockhash"]))
            sig = await self._send(tx)
            await self._confirm(sig)
            log.info("Compte token fermé, loyer récupéré (%s)", sig)
        except (TradeError, httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.info("Fermeture du compte token impossible (%s) : loyer non récupéré", e)
