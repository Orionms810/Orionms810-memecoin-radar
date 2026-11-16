"""Chargement de la configuration : secrets dans .env, réglages dans config.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .risk import RiskThresholds
from .trading.settings import TradingSettings

DEFAULT_GENERIC = [
    "trump", "donald", "elon", "musk", "coin", "token", "sol", "solana", "pump", "moon",
    "meme", "memecoin", "crypto", "bitcoin", "btc", "eth", "official", "the", "inu", "usa",
    "america", "american", "president", "news", "world", "people", "great", "today",
]


@dataclass
class Settings:
    # secrets (.env)
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    x_bearer: str | None = None
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    ai_available: bool = False

    # sources
    x_accounts: list[dict[str, Any]] = field(default_factory=list)
    x_poll_seconds: float = 20
    x_include_replies: bool = False
    rss_feeds: list[dict[str, Any]] = field(default_factory=list)
    rss_poll_seconds: float = 60

    # narratifs
    ttl_minutes: float = 45
    generic_keywords: list[str] = field(default_factory=lambda: list(DEFAULT_GENERIC))

    # matching
    min_match_score: float = 0.55
    observe_seconds: float = 20
    retro_minutes: float = 10
    max_watched_tokens: int = 60
    max_alerts_per_narrative: int = 3

    # alertes
    min_safety_to_alert: int = 40
    min_momentum: int = 10
    show_dangerous: bool = False
    watch_minutes: float = 30
    notify_narrative_min_weight: float = 0.8
    notify_narrative_min_hype: int = 60
    rug_drop_alert_pct: float = 50

    # IA
    ai_enabled: bool = True
    ai_model: str = "claude-opus-5-5"
    ai_effort: str = "low"
    ai_min_hype: int = 25
    ai_review_alerts: bool = True

    # données
    ipfs_gateway: str | None = None
    use_pumpfun_api: bool = True
    use_rugcheck: bool = True
    db_path: str = "memebot.db"

    risk: RiskThresholds = field(default_factory=RiskThresholds)
    trading: TradingSettings = field(default_factory=TradingSettings)
    wallet_secret: str | None = None  # clé privée du wallet dédié (mode réel uniquement)

    @property
    def ai_active(self) -> bool:
        return self.ai_enabled and self.ai_available


SECTIONS = {
    "x": {"poll_seconds": "x_poll_seconds", "include_replies": "x_include_replies",
          "accounts": "x_accounts"},
    "rss": {"poll_seconds": "rss_poll_seconds", "feeds": "rss_feeds"},
    "narratives": {"ttl_minutes": "ttl_minutes", "generic_keywords": "generic_keywords"},
    "matching": {"min_match_score": "min_match_score", "observe_seconds": "observe_seconds",
                 "retro_minutes": "retro_minutes", "max_watched_tokens": "max_watched_tokens",
                 "max_alerts_per_narrative": "max_alerts_per_narrative"},
    "alerts": {"min_safety_to_alert": "min_safety_to_alert", "show_dangerous": "show_dangerous",
               "min_momentum": "min_momentum",
               "watch_minutes": "watch_minutes",
               "notify_narrative_min_weight": "notify_narrative_min_weight",
               "notify_narrative_min_hype": "notify_narrative_min_hype",
               "rug_drop_alert_pct": "rug_drop_alert_pct"},
    "ai": {"enabled": "ai_enabled", "model": "ai_model", "effort": "ai_effort",
           "min_hype": "ai_min_hype", "review_alerts": "ai_review_alerts"},
    "data": {"ipfs_gateway": "ipfs_gateway", "use_pumpfun_api": "use_pumpfun_api",
             "use_rugcheck": "use_rugcheck", "db_path": "db_path"},
}


def load_settings(config_path: str | Path = "config.yaml", env_path: str | Path = ".env") -> Settings:
    load_dotenv(env_path)
    s = Settings()
    path = Path(config_path)
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for section, mapping in SECTIONS.items():
            for key, attr in mapping.items():
                value = (raw.get(section) or {}).get(key)
                if value is not None:
                    setattr(s, attr, value)
        for key, value in (raw.get("risk") or {}).items():
            if hasattr(s.risk, key) and value is not None:
                setattr(s.risk, key, value)
        for key, value in (raw.get("trading") or {}).items():
            if hasattr(s.trading, key) and value is not None:
                setattr(s.trading, key, value)
        s.trading.mode = str(s.trading.mode).lower()

    s.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN") or None
    s.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID") or None
    s.x_bearer = os.getenv("X_BEARER_TOKEN") or None
    s.rpc_url = os.getenv("SOLANA_RPC_URL") or s.rpc_url
    s.wallet_secret = os.getenv("WALLET_PRIVATE_KEY") or None
    s.ai_available = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
    return s
