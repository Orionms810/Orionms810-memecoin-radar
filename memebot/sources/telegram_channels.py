"""Lecture de chaînes Telegram publiques (par ex. des chaînes qui republient les
tweets d'Elon Musk ou de Trump), avec TON compte Telegram via Telethon.

Gratuit. Il faut :
1. une clé API gratuite sur https://my.telegram.org (TG_API_ID / TG_API_HASH dans .env) ;
2. une connexion unique : « python -m memebot telegram-login ».

⚠️ Le fichier de session (telegram.session) donne accès à ton compte Telegram :
ne le partage jamais. Le bot ne lit QUE les chaînes que tu lui indiques ; les
autres messages (tes conversations privées) sont ignorés immédiatement.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

from ..models import NewsEvent
from ..storage import Storage
from . import Emit

log = logging.getLogger(__name__)
KV_KEY = "telegram_channels"
HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


@dataclass
class ChannelCfg:
    username: str  # sans @
    label: str = ""
    weight: float = 0.8
    aliases: tuple[str, ...] = ()
    announce: bool = False  # annoncer chaque message sur Telegram (spam si la chaîne poste beaucoup)
    from_config: bool = False
    chat_id: int | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"username": self.username, "label": self.label, "weight": self.weight}


def normalize_handle(raw: str) -> str | None:
    """« @Nom », « t.me/Nom », « https://t.me/Nom/123 » -> « nom » (None si invalide)."""
    h = raw.strip()
    h = re.sub(r"^(https?://)?(www\.)?(t|telegram)\.me/", "", h, flags=re.I)
    h = h.lstrip("@").split("/")[0].split("?")[0]
    return h.lower() if HANDLE_RE.match(h) else None


def message_to_event(cfg: ChannelCfg, msg_id: int, text: str) -> NewsEvent | None:
    text = (text or "").strip()
    if not text:
        return None
    return NewsEvent(
        source="telegram",
        source_name=cfg.label or f"@{cfg.username}",
        event_id=f"tg:{cfg.chat_id or cfg.username}:{msg_id}",
        text=text[:2000],
        url=f"https://t.me/{cfg.username}/{msg_id}",
        weight=cfg.weight,
        aliases=cfg.aliases,
        announce=cfg.announce,
    )


class TelegramChannelsSource:
    def __init__(self, api_id: int, api_hash: str, session: str, channels: list[ChannelCfg],
                 emit: Emit, storage: Storage, *, join: bool = True):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session = session
        self.emit = emit
        self.storage = storage
        self.join = join
        self.client: Any = None
        self.connected = False
        self.channels: dict[str, ChannelCfg] = {}
        self.by_chat: dict[int, ChannelCfg] = {}
        for cfg in channels:
            cfg.from_config = True
            self.channels[cfg.username] = cfg
        for d in storage.kv_get(KV_KEY, []) or []:
            if d.get("username") and d["username"] not in self.channels:
                self.channels[d["username"]] = ChannelCfg(
                    username=d["username"], label=d.get("label", ""),
                    weight=float(d.get("weight", 0.8)))

    def _save(self) -> None:
        self.storage.kv_set(KV_KEY, [c.to_dict() for c in self.channels.values()
                                     if not c.from_config])

    # --------------------------------------------------------------- boucle
    async def run(self) -> None:
        try:
            from telethon import TelegramClient, events
        except ImportError:
            log.error("Chaînes Telegram : module « telethon » manquant (pip install telethon).")
            return
        client = TelegramClient(self.session, self.api_id, self.api_hash)
        client.add_event_handler(self._on_message, events.NewMessage())
        backoff = 5.0
        while True:
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    log.error("Chaînes Telegram : pas connecté à ton compte. Lance une fois "
                              "« python -m memebot telegram-login », puis relance le bot.")
                    await client.disconnect()
                    return
                self.client = client
                for cfg in list(self.channels.values()):
                    await self._resolve(cfg)
                self.connected = True
                ok = [c for c in self.channels.values() if c.chat_id]
                log.info("Chaînes Telegram : %d suivie(s)%s", len(ok),
                         (" — " + ", ".join(f"@{c.username}" for c in ok)) if ok else
                         " (ajoute-en avec /chaine @nom)")
                backoff = 5.0
                await client.run_until_disconnected()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - une source ne doit jamais tuer le bot
                log.warning("Chaînes Telegram : connexion perdue (%s), nouvel essai dans %.0f s",
                            e, backoff)
            finally:
                self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300.0)

    async def _resolve(self, cfg: ChannelCfg) -> str | None:
        """Trouve la chaîne et la rejoint. Retourne un message d'erreur, ou None."""
        from telethon import errors, utils
        from telethon.tl.functions.channels import JoinChannelRequest

        try:
            entity = await self.client.get_entity(cfg.username)
        except errors.FloodWaitError as e:
            cfg.error = f"Telegram demande d'attendre {e.seconds} s"
            return cfg.error
        except (ValueError, errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
            cfg.error = f"chaîne @{cfg.username} introuvable"
            return cfg.error
        if not getattr(entity, "broadcast", False) and not getattr(entity, "megagroup", False):
            cfg.error = f"@{cfg.username} n'est pas une chaîne"
            return cfg.error
        if self.join:
            try:
                await self.client(JoinChannelRequest(entity))
            except errors.UserAlreadyParticipantError:
                pass
            except errors.ChannelPrivateError:
                cfg.error = f"@{cfg.username} est privée"
                return cfg.error
            except errors.FloodWaitError as e:
                cfg.error = f"Telegram demande d'attendre {e.seconds} s avant de rejoindre"
                return cfg.error
        cfg.chat_id = utils.get_peer_id(entity)
        cfg.label = cfg.label or getattr(entity, "title", "") or f"@{cfg.username}"
        cfg.error = ""
        self.by_chat[cfg.chat_id] = cfg
        return None

    async def _on_message(self, event: Any) -> None:
        cfg = self.by_chat.get(event.chat_id)
        if cfg is None:  # conversation privée ou chaîne non suivie : ignorée
            return
        ev = message_to_event(cfg, event.message.id, event.raw_text)
        if ev:
            await self.emit(ev)

    # ------------------------------------------------------------- commandes
    async def handle_command(self, cmd: str, arg: str) -> str | None:
        if cmd == "/chaines":
            if not self.channels:
                return ("Aucune chaîne suivie. Ajoute-en une avec /chaine @nom "
                        "(cherche dans Telegram « Elon Musk tweets » par exemple).")
            state = "connecté" if self.connected else "déconnecté"
            lines = [f"📺 <b>Chaînes Telegram suivies</b> ({state})"]
            for c in self.channels.values():
                status = "✅" if c.chat_id else ("❌ " + c.error if c.error else "⏳")
                lines.append(f"{status} @{c.username} — {c.label or '?'} · poids {c.weight:g}")
            return "\n".join(lines)
        if cmd == "/chaine":
            parts = arg.split()
            handle = normalize_handle(parts[0]) if parts else None
            if not handle:
                return "Usage : /chaine @nom_de_la_chaine [poids 0-1] (ex : /chaine @elontweets 0.9)"
            if handle in self.channels:
                return f"@{handle} est déjà suivie."
            try:
                weight = min(1.0, max(0.1, float(parts[1]))) if len(parts) > 1 else 0.8
            except ValueError:
                return "Le poids doit être un nombre entre 0 et 1 (ex : 0.9)."
            cfg = ChannelCfg(username=handle, weight=weight)
            if self.connected and self.client:
                err = await self._resolve(cfg)
                if err:
                    return f"❌ {err}"
            self.channels[handle] = cfg
            self._save()
            where = "Je lis ses nouveaux messages." if cfg.chat_id else \
                "Elle sera lue dès que la connexion Telegram sera active."
            return f"✅ Chaîne ajoutée : <b>{cfg.label or '@' + handle}</b> (poids {weight:g}). {where}"
        if cmd == "/retirer":
            handle = normalize_handle(arg)
            cfg = self.channels.get(handle or "")
            if not cfg:
                return "Chaîne introuvable (voir /chaines)."
            if cfg.from_config:
                return "Cette chaîne vient de config.toml : retire-la dans ce fichier."
            del self.channels[cfg.username]
            if cfg.chat_id is not None:
                self.by_chat.pop(cfg.chat_id, None)
            self._save()
            return f"Chaîne @{cfg.username} retirée."
        return None


async def interactive_login(api_id: int, api_hash: str, session: str) -> None:
    """Connexion unique au compte Telegram (demande le numéro puis le code reçu)."""
    from getpass import getpass

    from telethon import TelegramClient

    print("Connexion à ton compte Telegram (une seule fois).")
    print("Telegram va t'envoyer un code dans l'application Telegram.\n")
    client = TelegramClient(session, api_id, api_hash)
    await client.start(
        phone=lambda: input("Ton numéro avec l'indicatif (ex : +33612345678) : "),
        code_callback=lambda: input("Code reçu dans Telegram : "),
        password=lambda: getpass("Mot de passe de la validation en deux étapes : "),
    )
    me = await client.get_me()
    print(f"\n✅ Connecté en tant que {getattr(me, 'first_name', '?')}. "
          f"Session enregistrée dans « {session}.session » : NE LA PARTAGE JAMAIS.")
    await client.disconnect()
