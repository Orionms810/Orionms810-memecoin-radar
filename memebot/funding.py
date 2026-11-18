"""Détection des faux acheteurs (« bundles ») : d'où vient l'argent des
premiers acheteurs d'un token ?

Arnaque classique sur pump.fun : le créateur achète son propre token avec
plein de wallets neufs pour simuler l'engouement, puis revend tout d'un coup.
Ces wallets sont presque toujours remplis par la même source (souvent le
créateur ou le wallet qui a financé le créateur). Pour chaque premier acheteur,
on remonte à la transaction qui lui a envoyé ses premiers SOL.

Limites : un wallet ancien (beaucoup de transactions) n'est pas remonté ; les
plateformes d'échange (Binance, Coinbase...) financent beaucoup de wallets
sans lien entre eux : on les ignore grâce à leur solde énorme.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

Rpc = Callable[[str, list[Any]], Awaitable[Any]]
LAMPORTS = 1_000_000_000


@dataclass
class WalletOrigin:
    wallet: str
    fresh: bool  # peu de transactions : wallet récent / jetable
    funder: str | None  # qui lui a envoyé ses premiers SOL
    age_s: float | None


@dataclass
class FundingInfo:
    analyzed: int  # premiers acheteurs vérifiés
    fresh: int  # dont wallets tout neufs
    max_shared: int  # plus grand nombre d'acheteurs financés par le même wallet
    shared_funder: str
    linked_to_creator: int  # acheteurs financés par le créateur ou par la même source que lui
    shared_is_creator_source: bool = False

    @property
    def fresh_ratio(self) -> float:
        return self.fresh / self.analyzed if self.analyzed else 0.0


def find_funder(tx: Any, wallet: str) -> str | None:
    """Dans une transaction (jsonParsed), qui a envoyé des SOL à `wallet` ?"""
    if not isinstance(tx, dict):
        return None
    try:
        instructions = list(tx["transaction"]["message"].get("instructions") or [])
    except (KeyError, TypeError, AttributeError):
        return None
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        instructions += inner.get("instructions") or []
    for ix in instructions:
        if not isinstance(ix, dict) or ix.get("program") != "system":
            continue
        parsed = ix.get("parsed") or {}
        info = parsed.get("info") or {}
        kind = parsed.get("type")
        if kind in ("transfer", "transferWithSeed") and info.get("destination") == wallet:
            if info.get("source") and info["source"] != wallet:
                return str(info["source"])
        if kind in ("createAccount", "createAccountWithSeed") and info.get("newAccount") == wallet:
            if info.get("source"):
                return str(info["source"])
    return None


def summarize(buyers: list[WalletOrigin], creator: WalletOrigin | None,
              ignore: set[str]) -> FundingInfo:
    counts = Counter(o.funder for o in buyers if o.funder and o.funder not in ignore)
    shared_funder, max_shared = counts.most_common(1)[0] if counts else ("", 0)
    creator_sources: set[str] = set()
    if creator:
        creator_sources.add(creator.wallet)
        if creator.funder and creator.funder not in ignore:
            creator_sources.add(creator.funder)
    linked = sum(1 for o in buyers if o.funder and o.funder in creator_sources)
    return FundingInfo(
        analyzed=len(buyers),
        fresh=sum(1 for o in buyers if o.fresh),
        max_shared=max_shared,
        shared_funder=shared_funder,
        linked_to_creator=linked,
        shared_is_creator_source=bool(shared_funder and shared_funder in creator_sources),
    )


class FundingTracer:
    def __init__(self, rpc: Rpc, *, page: int = 25, exchange_balance_sol: float = 5000.0,
                 ignore: tuple[str, ...] | list[str] = (), concurrency: int = 4,
                 cache_size: int = 5000, min_interval: float = 0.12):
        self.rpc = rpc
        self.page = page
        self.exchange_lamports = exchange_balance_sol * LAMPORTS
        self.ignore = set(ignore)
        self.sem = asyncio.Semaphore(concurrency)
        # ~8 requêtes/s maximum : reste sous la limite des offres RPC gratuites (Helius : 10/s)
        self.min_interval = min_interval
        self._rate_lock = asyncio.Lock()
        self._next_slot = 0.0
        self.cache_size = cache_size
        self._origins: dict[str, WalletOrigin | None] = {}
        self._exchanges: dict[str, bool] = {}

    def _remember(self, cache: dict, key: str, value: Any) -> None:
        if len(cache) >= self.cache_size:
            cache.pop(next(iter(cache)))
        cache[key] = value

    async def _call(self, method: str, params: list[Any]) -> Any:
        async with self.sem:
            if self.min_interval > 0:
                async with self._rate_lock:
                    now = time.monotonic()
                    if self._next_slot > now:
                        await asyncio.sleep(self._next_slot - now)
                    self._next_slot = max(now, self._next_slot) + self.min_interval
            return await self.rpc(method, params)

    async def origin(self, wallet: str) -> WalletOrigin | None:
        if wallet in self._origins:
            return self._origins[wallet]
        sigs = await self._call("getSignaturesForAddress",
                                [wallet, {"limit": self.page, "commitment": "confirmed"}])
        if not isinstance(sigs, list) or not sigs:
            return None  # erreur RPC : on ne met pas en cache, on réessaiera
        oldest = sigs[-1]
        block_time = oldest.get("blockTime")
        age = time.time() - block_time if block_time else None
        fresh = len(sigs) < self.page
        funder = None
        if fresh:  # wallet récent : sa 1re transaction est presque toujours son financement
            tx = await self._call("getTransaction", [oldest.get("signature"), {
                "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                "commitment": "confirmed"}])
            funder = find_funder(tx, wallet)
        result = WalletOrigin(wallet=wallet, fresh=fresh, funder=funder, age_s=age)
        self._remember(self._origins, wallet, result)
        return result

    async def is_exchange(self, address: str) -> bool:
        """Plateforme d'échange / service : solde énorme, finance des milliers de wallets."""
        if address in self.ignore:
            return True
        if address in self._exchanges:
            return self._exchanges[address]
        res = await self._call("getBalance", [address, {"commitment": "confirmed"}])
        try:
            big = int(res["value"]) >= self.exchange_lamports
        except (KeyError, TypeError, ValueError):
            return False
        self._remember(self._exchanges, address, big)
        return big

    async def analyze(self, buyers: list[str], creator: str | None) -> FundingInfo | None:
        wallets = list(dict.fromkeys(w for w in buyers if w and w != creator))
        if not wallets:
            return None
        targets = wallets + ([creator] if creator else [])
        origins = await asyncio.gather(*(self.origin(w) for w in targets), return_exceptions=True)
        clean = [o if isinstance(o, WalletOrigin) else None for o in origins]
        buyer_origins = [o for o in clean[: len(wallets)] if o]
        creator_origin = clean[len(wallets)] if creator else None
        if not buyer_origins:
            return None
        funders = {o.funder for o in clean if o and o.funder}
        checks = await asyncio.gather(*(self.is_exchange(f) for f in funders),
                                      return_exceptions=True)
        exchanges = {f for f, big in zip(funders, checks) if big is True}
        return summarize(buyer_origins, creator_origin, exchanges | self.ignore)
