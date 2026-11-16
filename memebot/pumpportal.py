"""Flux temps réel pump.fun via la Data API (gratuite) de PumpPortal.

Important : PumpPortal demande d'utiliser UNE SEULE connexion websocket et
d'y ajouter les abonnements, sinon ton IP peut être bannie. Ce client
respecte ça et se reconnecte tout seul.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

import websockets

from .models import NewToken, Trade

log = logging.getLogger(__name__)
URL = "wss://pumpportal.fun/api/data"


class PumpPortalClient:
    def __init__(self, on_new_token: Callable[[NewToken], None],
                 on_trade: Callable[[Trade], None], url: str = URL):
        self.on_new_token = on_new_token
        self.on_trade = on_trade
        self.url = url
        self._ws = None
        self._trade_keys: set[str] = set()
        self.connected = False
        self.messages = 0

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=20, ping_timeout=20,
                                              max_size=2**21) as ws:
                    self._ws = ws
                    self.connected = True
                    log.info("PumpPortal : connecté")
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    if self._trade_keys:
                        await ws.send(json.dumps({"method": "subscribeTokenTrade",
                                                  "keys": sorted(self._trade_keys)}))
                    backoff = 1.0
                    async for raw in ws:
                        self.messages += 1
                        self.dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("PumpPortal : connexion perdue (%s), reconnexion dans %.0f s", e, backoff)
            finally:
                self._ws = None
                self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def dispatch(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(msg, dict):
            return
        tx = msg.get("txType")
        try:
            if tx == "create":
                token = NewToken.from_pumpportal(msg)
                if token:
                    self.on_new_token(token)
            elif tx in ("buy", "sell"):
                trade = Trade.from_pumpportal(msg)
                if trade:
                    self.on_trade(trade)
        except Exception:  # noqa: BLE001 - un message bizarre ne doit pas couper le flux
            log.exception("PumpPortal : erreur en traitant %s", str(msg)[:200])

    async def _send(self, payload: dict) -> None:
        ws = self._ws
        if ws is None:
            return  # sera (ré)envoyé à la reconnexion
        try:
            await ws.send(json.dumps(payload))
        except Exception as e:  # noqa: BLE001
            log.debug("PumpPortal : envoi impossible (%s)", e)

    async def subscribe_trades(self, mints: list[str]) -> None:
        new = [m for m in mints if m not in self._trade_keys]
        if not new:
            return
        self._trade_keys.update(new)
        await self._send({"method": "subscribeTokenTrade", "keys": new})

    async def unsubscribe_trades(self, mints: list[str]) -> None:
        old = [m for m in mints if m in self._trade_keys]
        if not old:
            return
        self._trade_keys.difference_update(old)
        await self._send({"method": "unsubscribeTokenTrade", "keys": old})
