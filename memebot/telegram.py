"""Notifications Telegram + réception des commandes (/check, /narratif...)."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Awaitable, Callable

import httpx

log = logging.getLogger(__name__)
MAX_LEN = 4000
TAG_RE = re.compile(r"<[^>]+>")

CommandHandler = Callable[[str], Awaitable[str | None]]


def strip_tags(s: str) -> str:
    return html.unescape(TAG_RE.sub("", s))


class Notifier:
    """Envoie sur Telegram si configuré, et affiche toujours dans la console."""

    def __init__(self, http: httpx.AsyncClient | None = None, token: str | None = None,
                 chat_id: str | None = None, console: bool = True):
        self.http = http
        self.token = token
        self.chat_id = chat_id
        self.console = console
        self.muted = False

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.http and self.token and self.chat_id)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    async def send(self, text: str, *, reply_to: int | None = None,
                   force: bool = False) -> int | None:
        """Retourne l'id du message Telegram (pour répondre dessous ensuite)."""
        if self.console:
            print("\n" + "─" * 60 + "\n" + strip_tags(text) + "\n" + "─" * 60, flush=True)
        if self.muted and not force:
            return None
        if not self.telegram_enabled:
            return None
        if len(text) > MAX_LEN:
            text = text[: MAX_LEN - 20] + "\n…(tronqué)"
        payload: dict = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to,
                                           "allow_sending_without_reply": True}
        try:
            assert self.http is not None
            r = await self.http.post(self._url("sendMessage"), json=payload, timeout=10)
            data = r.json()
            if not data.get("ok"):
                log.warning("Telegram : %s", data.get("description"))
                return None
            return int(data["result"]["message_id"])
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("Telegram : envoi impossible (%s)", e)
            return None

    async def send_document(self, filename: str, content: bytes, caption: str = "") -> bool:
        if not self.telegram_enabled:
            return False
        try:
            assert self.http is not None
            r = await self.http.post(self._url("sendDocument"), timeout=30,
                                     data={"chat_id": self.chat_id, "caption": caption[:1000]},
                                     files={"document": (filename, content, "text/csv")})
            data = r.json()
            if not data.get("ok"):
                log.warning("Telegram (fichier) : %s", data.get("description"))
            return bool(data.get("ok"))
        except (httpx.HTTPError, ValueError) as e:
            log.warning("Telegram : envoi du fichier impossible (%s)", e)
            return False

    async def poll_commands(self, handler: CommandHandler) -> None:
        """Long polling des messages reçus par le bot Telegram."""
        if not (self.http and self.token):
            return
        offset = 0
        while True:
            try:
                r = await self.http.get(self._url("getUpdates"),
                                        params={"offset": offset, "timeout": 30},
                                        timeout=40)
                data = r.json()
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, ValueError) as e:
                log.debug("Telegram getUpdates : %s", e)
                await asyncio.sleep(5)
                continue
            if not data.get("ok"):
                log.warning("Telegram getUpdates : %s", data.get("description"))
                await asyncio.sleep(10)
                continue
            for upd in data.get("result", []):
                offset = max(offset, int(upd["update_id"]) + 1)
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                if not text or not chat_id:
                    continue
                if not self.chat_id:
                    # Mode configuration : on donne son chat id à l'utilisateur.
                    log.warning("Message reçu du chat %s. Mets TELEGRAM_CHAT_ID=%s dans .env",
                                chat_id, chat_id)
                    await self._reply(chat_id, f"Ton chat id est <code>{chat_id}</code>. "
                                      "Mets-le dans le fichier .env (TELEGRAM_CHAT_ID) "
                                      "puis relance le bot.")
                    continue
                if chat_id != str(self.chat_id):
                    log.warning("Commande ignorée d'un chat inconnu (%s)", chat_id)
                    continue
                try:
                    reply = await handler(text)
                except Exception:  # noqa: BLE001
                    log.exception("Erreur pendant la commande %r", text)
                    reply = "❌ Erreur interne pendant la commande (voir les logs)."
                if reply:
                    await self.send(reply, force=True)

    async def _reply(self, chat_id: str, text: str) -> None:
        try:
            assert self.http is not None
            await self.http.post(self._url("sendMessage"),
                                 json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                                 timeout=10)
        except httpx.HTTPError:
            pass
