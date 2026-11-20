"""Trades pump.fun lus directement sur la blockchain Solana (gratuit).

Depuis mai 2026, le flux de trades de PumpPortal exige une clé payante. Ici on
s'abonne, via le websocket RPC (Helius gratuit : 5 connexions, 1000
abonnements chacune), aux transactions qui mentionnent chaque token suivi
(`logsSubscribe`), et on décode l'événement « TradeEvent » que le programme
pump.fun écrit dans les logs (« Program data: <base64> »).

Format vérifié dans l'IDL officiel de pump.fun (programme
6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P) :
discriminant bddb7fd34ee661ee, puis mint (32 octets), sol_amount (u64),
token_amount (u64), is_buy (bool), user (32), timestamp (i64),
virtual_sol_reserves (u64), virtual_token_reserves (u64), ...

Nouveaux tokens : on s'abonne aussi au compte « mint_authority » de pump.fun
(TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM, PDA ["mint-authority"]), qui
n'apparaît que dans les instructions create et create_v2 : on reçoit chaque
création (CreateEvent, discriminant 1b72a94ddeeb6376) sans le flot des trades.

Limite : après la migration d'un token vers PumpSwap, ses trades ne passent
plus par ce programme et ne sont plus vus.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
import time
from typing import Any, Callable

import websockets

from .models import NewToken, Trade

log = logging.getLogger(__name__)

TRADE_DISCRIMINATOR = bytes.fromhex("bddb7fd34ee661ee")
CREATE_DISCRIMINATOR = bytes.fromhex("1b72a94ddeeb6376")
MINT_AUTHORITY = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"
PROGRAM_DATA = "Program data: "
_MIN_LEN = 8 + 32 + 8 + 8 + 1 + 32 + 8 + 8 + 8  # jusqu'à virtual_token_reserves
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(data) - len(data.lstrip(b"\0"))
    return "1" * pad + out


def ws_url_from_rpc(rpc_url: str) -> str:
    if rpc_url.startswith("https://"):
        return "wss://" + rpc_url[len("https://"):]
    if rpc_url.startswith("http://"):
        return "ws://" + rpc_url[len("http://"):]
    return rpc_url


def decode_trades(logs: list[str], now: float | None = None) -> list[Trade]:
    """Extrait les trades pump.fun des logs d'une transaction."""
    now = now or time.time()
    trades: list[Trade] = []
    for line in logs or []:
        if not isinstance(line, str) or not line.startswith(PROGRAM_DATA):
            continue
        try:
            data = base64.b64decode(line[len(PROGRAM_DATA):].strip(), validate=True)
        except (ValueError, TypeError):
            continue
        if len(data) < _MIN_LEN or data[:8] != TRADE_DISCRIMINATOR:
            continue
        o = 8
        mint = b58encode(data[o:o + 32]); o += 32
        sol_amount, token_amount = struct.unpack_from("<QQ", data, o); o += 16
        is_buy_byte = data[o]; o += 1
        user = b58encode(data[o:o + 32]); o += 32
        timestamp, v_sol, v_tok = struct.unpack_from("<qQQ", data, o)
        # Garde-fous : si le format change un jour, on ignore plutôt que d'inventer
        if is_buy_byte not in (0, 1) or v_tok <= 0 or abs(timestamp - now) > 86400:
            continue
        trades.append(Trade(
            mint=mint,
            trader=user,
            is_buy=is_buy_byte == 1,
            sol_amount=sol_amount / 1e9,
            token_amount=token_amount / 1e6,
            new_balance=None,
            market_cap_sol=v_sol * 1e6 / v_tok,  # prix x 1 milliard de tokens
            v_tokens=v_tok / 1e6,
            ts=now,
        ))
    return trades


def _program_data(logs: list[str]) -> list[bytes]:
    out = []
    for line in logs or []:
        if isinstance(line, str) and line.startswith(PROGRAM_DATA):
            try:
                out.append(base64.b64decode(line[len(PROGRAM_DATA):].strip(), validate=True))
            except (ValueError, TypeError):
                continue
    return out


def _read_str(data: bytes, o: int) -> tuple[str, int]:
    (n,) = struct.unpack_from("<I", data, o)
    if n > 512:
        raise ValueError("chaîne trop longue")
    raw = data[o + 4:o + 4 + n]
    if len(raw) != n:
        raise ValueError("données tronquées")
    return raw.decode("utf-8", errors="replace"), o + 4 + n


def decode_creations(logs: list[str], signature: str = "",
                     now: float | None = None) -> list[NewToken]:
    """Extrait les nouveaux tokens (CreateEvent) et l'achat initial du dev."""
    now = now or time.time()
    trades = decode_trades(logs, now)
    tokens: list[NewToken] = []
    for data in _program_data(logs):
        if data[:8] != CREATE_DISCRIMINATOR:
            continue
        try:
            o = 8
            name, o = _read_str(data, o)
            symbol, o = _read_str(data, o)
            uri, o = _read_str(data, o)
            mint = b58encode(data[o:o + 32]); o += 32
            bonding_curve = b58encode(data[o:o + 32]); o += 32
            user = b58encode(data[o:o + 32]); o += 32
            o += 32  # creator (on garde « user » : c'est lui qui détient l'achat du dev)
            timestamp, v_tok, v_sol = struct.unpack_from("<qQQ", data, o)
        except (ValueError, struct.error):
            continue
        if v_tok <= 0 or abs(timestamp - now) > 86400:
            continue
        dev = [t for t in trades if t.mint == mint and t.trader == user and t.is_buy]
        last = dev[-1] if dev else None
        tokens.append(NewToken(
            mint=mint, name=name, symbol=symbol, creator=user, uri=uri,
            initial_buy=sum(t.token_amount for t in dev),
            sol_amount=sum(t.sol_amount for t in dev),
            market_cap_sol=last.market_cap_sol if last else v_sol * 1e6 / v_tok,
            bonding_curve=bonding_curve,
            v_tokens=last.v_tokens if last else v_tok / 1e6,
            signature=signature, pool="pump", created_at=now,
        ))
    return tokens


class SolanaTradeFeed:
    """Même interface que PumpPortalClient pour les trades (subscribe/unsubscribe)."""

    def __init__(self, ws_url: str, on_trade: Callable[[Trade], None], *, max_subs: int = 900,
                 connect_kwargs: dict[str, Any] | None = None,
                 on_new_token: Callable[[NewToken], None] | None = None):
        self.ws_url = ws_url
        self.on_trade = on_trade
        self.on_new_token = on_new_token
        self._create_sub: int | None = None
        self.creations = 0
        self.max_subs = max_subs
        self.connect_kwargs = connect_kwargs or {}
        self.connected = False
        self.messages = 0
        self._ws: Any = None
        self._wanted: set[str] = set()
        self._sub_of: dict[str, int] = {}  # mint -> id d'abonnement
        self._mint_of: dict[int, str] = {}  # id d'abonnement -> mint
        self._pending: dict[int, tuple[str, str]] = {}  # id de requête -> (action, mint)
        self._req = 0
        self._warned_full = False

    # --------------------------------------------------------------- boucle
    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20,
                                              max_size=2**22, **self.connect_kwargs) as ws:
                    self._ws = ws
                    self._sub_of.clear()
                    self._mint_of.clear()
                    self._pending.clear()
                    self._create_sub = None
                    self.connected = True
                    if self.on_new_token:
                        req = await self._send({"method": "logsSubscribe", "params": [
                            {"mentions": [MINT_AUTHORITY]}, {"commitment": "confirmed"}]})
                        if req is not None:
                            self._pending[req] = ("create", MINT_AUTHORITY)
                    log.info("Trades Solana : connecté (%d tokens suivis)", len(self._wanted))
                    for mint in list(self._wanted):
                        await self._send_subscribe(mint)
                    backoff = 1.0
                    async for raw in ws:
                        self.messages += 1
                        self.handle_message(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Trades Solana : connexion perdue (%s), reconnexion dans %.0f s",
                            e, backoff)
            finally:
                self._ws = None
                self.connected = False
                self._create_sub = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def handle_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(msg, dict):
            return
        if "id" in msg and msg.get("id") in self._pending:
            action, mint = self._pending.pop(msg["id"])
            if action == "create":
                if isinstance(msg.get("result"), int):
                    self._create_sub = msg["result"]
                    log.info("Nouveaux tokens : lus sur la blockchain")
                else:
                    log.warning("Nouveaux tokens : abonnement refusé (%s)",
                                (msg.get("error") or {}).get("message"))
                return
            if action == "sub":
                if "error" in msg:
                    log.warning("Trades Solana : abonnement refusé pour %s (%s)", mint,
                                (msg["error"] or {}).get("message"))
                    return
                sub = msg.get("result")
                if isinstance(sub, int):
                    if mint in self._wanted:
                        self._sub_of[mint] = sub
                        self._mint_of[sub] = mint
                    else:  # désabonné entre-temps
                        asyncio.ensure_future(self._send({"method": "logsUnsubscribe",
                                                          "params": [sub]}))
            return
        if msg.get("method") != "logsNotification":
            return
        params = msg.get("params") or {}
        value = ((params.get("result") or {}).get("value")) or {}
        if self._create_sub is not None and params.get("subscription") == self._create_sub:
            if value.get("err") is None and self.on_new_token:
                for token in decode_creations(value.get("logs") or [], value.get("signature") or ""):
                    self.creations += 1
                    try:
                        self.on_new_token(token)
                    except Exception:  # noqa: BLE001
                        log.exception("Nouveaux tokens : erreur en traitant %s", token.mint)
            return
        mint = self._mint_of.get(params.get("subscription"))
        if mint is None or value.get("err") is not None:
            return
        for trade in decode_trades(value.get("logs") or []):
            if trade.mint == mint:  # un même tx peut concerner d'autres tokens
                try:
                    self.on_trade(trade)
                except Exception:  # noqa: BLE001 - un trade bizarre ne doit pas couper le flux
                    log.exception("Trades Solana : erreur en traitant un trade")

    @property
    def creations_ok(self) -> bool:
        return self.connected and self._create_sub is not None

    # -------------------------------------------------------- abonnements
    async def _send(self, payload: dict[str, Any]) -> int | None:
        ws = self._ws
        if ws is None:
            return None
        self._req += 1
        payload = {"jsonrpc": "2.0", "id": self._req, **payload}
        try:
            await ws.send(json.dumps(payload))
        except Exception as e:  # noqa: BLE001
            log.debug("Trades Solana : envoi impossible (%s)", e)
            return None
        return self._req

    async def _send_subscribe(self, mint: str) -> None:
        req = await self._send({"method": "logsSubscribe",
                                "params": [{"mentions": [mint]}, {"commitment": "confirmed"}]})
        if req is not None:
            self._pending[req] = ("sub", mint)

    async def subscribe_trades(self, mints: list[str]) -> None:
        for mint in mints:
            if mint in self._wanted:
                continue
            if len(self._wanted) >= self.max_subs:
                if not self._warned_full:
                    self._warned_full = True
                    log.warning("Trades Solana : %d tokens suivis, limite atteinte", self.max_subs)
                return
            self._wanted.add(mint)
            await self._send_subscribe(mint)

    async def unsubscribe_trades(self, mints: list[str]) -> None:
        for mint in mints:
            if mint not in self._wanted:
                continue
            self._wanted.discard(mint)
            self._warned_full = False
            sub = self._sub_of.pop(mint, None)
            if sub is not None:
                self._mint_of.pop(sub, None)
                await self._send({"method": "logsUnsubscribe", "params": [sub]})
