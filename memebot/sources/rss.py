"""Flux RSS : news du monde, miroirs Truth Social, Google News, etc."""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import html
import logging
import re
import time
from dataclasses import dataclass, field

import feedparser
import httpx

from ..models import NewsEvent
from . import Emit

log = logging.getLogger(__name__)
TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class Feed:
    name: str
    url: str
    weight: float = 0.4
    aliases: tuple[str, ...] = ()
    seeded: bool = False
    seen: set[str] = field(default_factory=set)


def clean_html(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", s))).strip()


class RSSSource:
    def __init__(self, http: httpx.AsyncClient, feeds: list[Feed], emit: Emit, *,
                 poll_seconds: float = 60, max_age_seconds: float = 3600,
                 already_seen=None):
        self.http = http
        self.feeds = feeds
        self.emit = emit
        self.poll_seconds = poll_seconds
        self.max_age = max_age_seconds
        self.already_seen = already_seen or (lambda _id: False)

    async def run(self) -> None:
        if not self.feeds:
            return
        log.info("RSS : %d flux surveillés", len(self.feeds))
        while True:
            await asyncio.gather(*(self._poll_safe(f) for f in self.feeds))
            await asyncio.sleep(self.poll_seconds)

    async def _poll_safe(self, feed: Feed) -> None:
        try:
            await self.poll(feed)
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as e:
            log.warning("RSS : %s injoignable (%s)", feed.name, e or type(e).__name__)
        except Exception:  # noqa: BLE001
            log.exception("RSS : erreur sur %s", feed.name)

    async def poll(self, feed: Feed) -> list[NewsEvent]:
        r = await self.http.get(feed.url, timeout=15, follow_redirects=True,
                                headers={"User-Agent": "Mozilla/5.0 (compatible; memecoin-radar/0.1)"})
        if r.status_code != 200:
            log.debug("RSS %s -> HTTP %s", feed.name, r.status_code)
            return []
        parsed = feedparser.parse(r.content)
        events: list[NewsEvent] = []
        now = time.time()
        for entry in parsed.entries:
            raw_id = entry.get("id") or entry.get("link") or entry.get("title") or ""
            eid = "rss:" + hashlib.sha1(f"{feed.url}|{raw_id}".encode()).hexdigest()[:16]
            if eid in feed.seen:
                continue
            feed.seen.add(eid)
            if not feed.seeded or self.already_seen(eid):
                continue
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published and now - calendar.timegm(published) > self.max_age:
                continue
            title = clean_html(entry.get("title", ""))
            summary = clean_html(entry.get("summary", ""))
            if summary and summary.startswith(title):
                text = summary  # ex. Truth Social : le titre est le début du post
            elif summary:
                text = f"{title} — {summary}"
            else:
                text = title
            events.append(NewsEvent(
                source="rss",
                source_name=feed.name,
                event_id=eid,
                text=text[:1200],
                url=entry.get("link", ""),
                weight=feed.weight,
                aliases=feed.aliases,
            ))
        if not feed.seeded:
            # Premier passage : on ignore l'historique du flux.
            feed.seeded = True
            return []
        for ev in events:
            await self.emit(ev)
        return events
