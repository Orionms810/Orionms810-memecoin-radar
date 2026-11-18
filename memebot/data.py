"""Accès aux données externes : métadonnées IPFS, RPC Solana, RugCheck,
DexScreener, API pump.fun, prix du SOL.

Toutes les méthodes sont « best effort » : en cas d'erreur réseau elles
renvoient None au lieu de planter, le bot continue avec moins d'infos.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .funding import FundingInfo, FundingTracer
from .models import DexInfo, HolderInfo, RugcheckInfo, TokenMetadata

log = logging.getLogger(__name__)

# Comptes qui détiennent des tokens pour le compte d'un pool (pas de vrais holders).
KNOWN_POOL_OWNERS = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM v4 authority
}

USER_AGENT = "Mozilla/5.0 (compatible; memecoin-radar/0.1)"


class LiveData:
    def __init__(
        self,
        http: httpx.AsyncClient,
        rpc_url: str,
        *,
        ipfs_gateway: str | None = None,
        use_pumpfun_api: bool = True,
        use_rugcheck: bool = True,
        funding_exchange_sol: float = 5000.0,
        ignore_funders: tuple[str, ...] | list[str] = (),
    ):
        self.http = http
        self.rpc_url = rpc_url
        self.ipfs_gateway = ipfs_gateway
        self.use_pumpfun_api = use_pumpfun_api
        self.use_rugcheck = use_rugcheck
        self._sol_usd: tuple[float, float] | None = None  # (prix, timestamp)
        self._rpc_id = 0
        self.funding_tracer = FundingTracer(self._rpc, exchange_balance_sol=funding_exchange_sol,
                                            ignore=tuple(ignore_funders))

    # ------------------------------------------------------------------ utils
    async def _get_json(self, url: str, timeout: float = 6.0, **kw: Any) -> Any:
        try:
            r = await self.http.get(url, timeout=timeout,
                                    headers={"User-Agent": USER_AGENT}, **kw)
            if r.status_code != 200:
                log.debug("GET %s -> %s", url, r.status_code)
                return None
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.debug("GET %s a échoué : %s", url, e)
            return None

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        self._rpc_id += 1
        body = {"jsonrpc": "2.0", "id": self._rpc_id, "method": method, "params": params}
        try:
            r = await self.http.post(self.rpc_url, json=body, timeout=8.0)
            if r.status_code != 200:
                log.debug("RPC %s -> HTTP %s", method, r.status_code)
                return None
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.debug("RPC %s a échoué : %s", method, e)
            return None
        if "error" in data:
            log.debug("RPC %s erreur : %s", method, data["error"])
            return None
        return data.get("result")

    # ------------------------------------------------------------ métadonnées
    async def metadata(self, uri: str) -> TokenMetadata | None:
        if not uri:
            return None
        if self.ipfs_gateway and "/ipfs/" in uri:
            uri = self.ipfs_gateway.rstrip("/") + "/ipfs/" + uri.split("/ipfs/", 1)[1]
        data = await self._get_json(uri, timeout=5.0, follow_redirects=True)
        if not isinstance(data, dict):
            return None
        return TokenMetadata(
            description=str(data.get("description") or "")[:500],
            twitter=str(data.get("twitter") or ""),
            telegram=str(data.get("telegram") or ""),
            website=str(data.get("website") or ""),
            image=str(data.get("image") or ""),
        )

    # ---------------------------------------------------------------- holders
    async def holders(self, mint: str, *, exclude_owners: set[str],
                      creator: str | None = None) -> HolderInfo | None:
        largest, supply = await asyncio.gather(
            self._rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}]),
            self._rpc("getTokenSupply", [mint, {"commitment": "confirmed"}]),
        )
        if not largest or not largest.get("value"):
            return None
        accounts = largest["value"][:20]
        try:
            total = float(supply["value"]["uiAmountString"]) if supply else 0.0
        except (KeyError, TypeError, ValueError):
            total = 0.0
        if total <= 0:
            total = 1_000_000_000.0

        multi = await self._rpc(
            "getMultipleAccounts",
            [[a["address"] for a in accounts], {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        owners: list[str | None] = [None] * len(accounts)
        if multi and isinstance(multi.get("value"), list):
            for i, acc in enumerate(multi["value"][: len(accounts)]):
                try:
                    owners[i] = acc["data"]["parsed"]["info"]["owner"]
                except (KeyError, TypeError):
                    owners[i] = None

        exclude = set(exclude_owners) | KNOWN_POOL_OWNERS
        rows: list[tuple[str, float]] = []
        for acc, owner in zip(accounts, owners):
            amount = float(acc.get("uiAmount") or acc.get("uiAmountString") or 0)
            if owner in exclude or acc.get("address") in exclude:
                continue
            rows.append((owner or acc.get("address", "?"), amount))
        rows.sort(key=lambda r: r[1], reverse=True)
        if not rows:
            return HolderInfo(0.0, 0.0, "", 0.0 if creator else None, len(accounts))
        top10 = sum(a for _, a in rows[:10]) / total * 100
        creator_pct = (sum(a for o, a in rows if o == creator) / total * 100) if creator else None
        return HolderInfo(
            top10_pct=top10,
            max_holder_pct=rows[0][1] / total * 100,
            max_holder=rows[0][0],
            creator_pct=creator_pct,
            accounts_checked=len(accounts),
        )

    # ------------------------------------------------------ faux acheteurs
    async def funding(self, buyers: list[str], *, creator: str | None = None) -> FundingInfo | None:
        return await self.funding_tracer.analyze(buyers, creator)

    # --------------------------------------------------------------- rugcheck
    async def rugcheck(self, mint: str) -> RugcheckInfo | None:
        if not self.use_rugcheck:
            return None
        data = await self._get_json(
            f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary", timeout=6.0)
        if not isinstance(data, dict):
            return None
        info = RugcheckInfo(score_normalised=_int_or_none(data.get("score_normalised")))
        for risk in data.get("risks") or []:
            if not isinstance(risk, dict):
                continue
            label = str(risk.get("name") or "")
            if risk.get("value"):
                label += f" ({risk['value']})"
            level = str(risk.get("level") or "").lower()
            if level == "danger":
                info.dangers.append(label)
            elif level == "warn":
                info.warnings.append(label)
        return info

    # ------------------------------------------------------------ pump.fun API
    async def creator_launch_count(self, creator: str) -> int | None:
        """Nb de tokens créés par ce wallet selon l'API (non officielle) de pump.fun."""
        if not self.use_pumpfun_api:
            return None
        data = await self._get_json(
            f"https://frontend-api-v3.pump.fun/coins/user-created-coins/{creator}"
            "?offset=0&limit=50&includeNsfw=true", timeout=5.0)
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            if isinstance(data.get("count"), int):
                return data["count"]
            if isinstance(data.get("coins"), list):
                return len(data["coins"])
        return None

    async def pump_coin(self, mint: str) -> dict[str, Any] | None:
        if not self.use_pumpfun_api:
            return None
        data = await self._get_json(f"https://frontend-api-v3.pump.fun/coins/{mint}", timeout=5.0)
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------ dexscreener
    async def dexscreener(self, mint: str) -> DexInfo | None:
        data = await self._get_json(f"https://api.dexscreener.com/tokens/v1/solana/{mint}")
        pairs = data if isinstance(data, list) else None
        if pairs is None:
            data = await self._get_json(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
            pairs = data.get("pairs") if isinstance(data, dict) else None
        if not pairs:
            return None
        pairs = [p for p in pairs if isinstance(p, dict)]
        if not pairs:
            return None
        p = max(pairs, key=lambda x: _num((x.get("liquidity") or {}).get("usd")) or 0)
        txh1 = (p.get("txns") or {}).get("h1") or {}
        created = _num(p.get("pairCreatedAt"))
        base = p.get("baseToken") or {}
        return DexInfo(
            dex=str(p.get("dexId") or "?"),
            pair_address=str(p.get("pairAddress") or ""),
            name=str(base.get("name") or ""),
            symbol=str(base.get("symbol") or ""),
            price_usd=_num(p.get("priceUsd")),
            market_cap_usd=_num(p.get("marketCap")) or _num(p.get("fdv")),
            liquidity_usd=_num((p.get("liquidity") or {}).get("usd")),
            volume_h1=_num((p.get("volume") or {}).get("h1")),
            volume_h24=_num((p.get("volume") or {}).get("h24")),
            change_h1=_num((p.get("priceChange") or {}).get("h1")),
            change_h24=_num((p.get("priceChange") or {}).get("h24")),
            buys_h1=_int_or_none(txh1.get("buys")),
            sells_h1=_int_or_none(txh1.get("sells")),
            pair_created_at=created / 1000 if created else None,
        )

    # ------------------------------------------------------------- prix du SOL
    async def sol_usd(self) -> float | None:
        now = time.time()
        if self._sol_usd and now - self._sol_usd[1] < 60:
            return self._sol_usd[0]
        data = await self._get_json(
            "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd")
        price = _num((data or {}).get("solana", {}).get("usd")) if isinstance(data, dict) else None
        if price is None:
            data = await self._get_json(
                "https://lite-api.jup.ag/price/v3?ids=So11111111111111111111111111111111111111112")
            if isinstance(data, dict):
                entry = data.get("So11111111111111111111111111111111111111112") or {}
                price = _num(entry.get("usdPrice"))
        if price:
            self._sol_usd = (price, now)
            return price
        return self._sol_usd[0] if self._sol_usd else None


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None
