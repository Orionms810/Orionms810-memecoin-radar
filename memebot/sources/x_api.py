"""Surveillance de comptes X (Twitter) via l'API officielle v2.

Il faut un « Bearer Token » du portail développeur X (developer.x.com).
L'API est payante (facturation à l'usage) : avec `since_id`, une requête qui
ne renvoie aucun nouveau post ne coûte en principe rien, mais vérifie la
grille tarifaire actuelle dans ta console développeur avant de lancer le bot.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from ..models import NewsEvent
from . import Emit

log = logging.getLogger(__name__)
API = "https://api.x.com/2"


@dataclass
class XAccount:
    username: str
    weight: float = 0.8
    aliases: tuple[str, ...] = ()
    user_id: str | None = None
    since_id: str | None = None
    seeded_at: str | None = None  # date ISO du premier passage
    next_allowed: float = 0.0


class XSource:
    def __init__(self, http: httpx.AsyncClient, bearer: str, accounts: list[XAccount],
                 emit: Emit, *, poll_seconds: float = 20, include_replies: bool = False):
        self.http = http
        self.headers = {"Authorization": f"Bearer {bearer}"}
        self.accounts = accounts
        self.emit = emit
        self.poll_seconds = poll_seconds
        self.include_replies = include_replies

    async def run(self) -> None:
        if not self.accounts:
            return
        log.info("X : surveillance de %s", ", ".join("@" + a.username for a in self.accounts))
        while True:
            for acc in self.accounts:
                if time.time() < acc.next_allowed:
                    continue
                try:
                    await self._poll(acc)
                except asyncio.CancelledError:
                    raise
                except httpx.HTTPError as e:
                    log.warning("X : @%s injoignable (%s)", acc.username, e or type(e).__name__)
                    acc.next_allowed = time.time() + 30
                except Exception:  # noqa: BLE001 - une source ne doit jamais tuer le bot
                    log.exception("X : erreur sur @%s", acc.username)
                    acc.next_allowed = time.time() + 60
            await asyncio.sleep(self.poll_seconds)

    def _rate_limited(self, acc: XAccount, r: httpx.Response) -> bool:
        if r.status_code != 429:
            return False
        reset = r.headers.get("x-rate-limit-reset")
        wait = 900.0
        if reset and reset.isdigit():
            wait = max(5.0, min(900.0, int(reset) - time.time()))
        acc.next_allowed = time.time() + wait
        log.warning("X : limite de requêtes atteinte pour @%s, pause %.0f s", acc.username, wait)
        return True

    async def _poll(self, acc: XAccount) -> None:
        if acc.user_id is None:
            r = await self.http.get(f"{API}/users/by/username/{acc.username}",
                                    headers=self.headers, timeout=10)
            if self._rate_limited(acc, r):
                return
            if r.status_code != 200:
                log.error("X : impossible de trouver @%s (HTTP %s) : %s",
                          acc.username, r.status_code, r.text[:200])
                acc.next_allowed = time.time() + 600
                return
            acc.user_id = r.json()["data"]["id"]

        params: dict[str, str | int] = {
            "max_results": 5,
            "tweet.fields": "created_at,note_tweet",
        }
        if not self.include_replies:
            params["exclude"] = "replies"
        if acc.since_id:
            params["since_id"] = acc.since_id
        elif acc.seeded_at:
            params["start_time"] = acc.seeded_at  # aucun post vu au 1er passage
        r = await self.http.get(f"{API}/users/{acc.user_id}/tweets", params=params,
                                headers=self.headers, timeout=10)
        if self._rate_limited(acc, r):
            return
        if r.status_code != 200:
            log.warning("X : HTTP %s pour @%s : %s", r.status_code, acc.username, r.text[:200])
            acc.next_allowed = time.time() + 60
            return
        data = r.json()
        tweets = data.get("data") or []
        newest = (data.get("meta") or {}).get("newest_id")
        first_run = acc.seeded_at is None
        if newest:
            acc.since_id = newest
        if first_run:
            # Premier passage : on mémorise juste le dernier post, pas d'alerte sur l'historique.
            acc.seeded_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            return
        for tw in reversed(tweets):  # du plus ancien au plus récent
            text = (tw.get("note_tweet") or {}).get("text") or tw.get("text") or ""
            await self.emit(NewsEvent(
                source="x",
                source_name="@" + acc.username,
                event_id=f"x:{tw['id']}",
                text=text,
                url=f"https://x.com/{acc.username}/status/{tw['id']}",
                weight=acc.weight,
                aliases=acc.aliases,
            ))
