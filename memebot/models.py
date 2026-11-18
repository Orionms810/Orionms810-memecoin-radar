"""Structures de données partagées par tout le bot."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# Tous les tokens pump.fun ont la même supply : 1 milliard (6 décimales).
PUMP_TOTAL_SUPPLY = 1_000_000_000.0
# Paramètres de la bonding curve pump.fun (réserves virtuelles initiales).
PUMP_INITIAL_VIRTUAL_TOKENS = 1_073_000_000.0
PUMP_INITIAL_REAL_TOKENS = 793_100_000.0


@dataclass
class NewsEvent:
    """Un post / une news qui peut lancer un narratif."""

    source: str  # "x", "rss", "manual"
    source_name: str  # "@realDonaldTrump", nom du flux RSS, "toi"
    event_id: str
    text: str
    url: str = ""
    weight: float = 0.5  # importance de la source (0..1)
    aliases: tuple[str, ...] = ()  # mots trop liés à l'auteur (ex: "trump")
    announce: bool | None = None  # annoncer le narratif ? (None = selon le poids de la source)
    created_at: float = field(default_factory=time.time)


@dataclass
class Narrative:
    """Un narratif actif : des mots-clés que les créateurs de tokens risquent d'utiliser."""

    id: str
    event: NewsEvent
    keywords: dict[str, float]  # mot-clé normalisé -> poids (0..1)
    created_at: float
    expires_at: float
    hype_score: int | None = None  # rempli par l'IA (0..100)
    memeable: bool | None = None
    summary: str = ""
    tickers: list[str] = field(default_factory=list)
    active: bool = True
    alerts_sent: int = 0
    matched_mints: set[str] = field(default_factory=set)

    @property
    def weight(self) -> float:
        """Importance du narratif : poids de la source, corrigé par l'IA si dispo."""
        base = self.event.weight
        if self.hype_score is not None:
            base = 0.4 * base + 0.6 * (self.hype_score / 100)
        return max(0.0, min(1.0, base))

    def is_live(self, now: float | None = None) -> bool:
        return self.active and (now or time.time()) < self.expires_at


@dataclass
class NewToken:
    """Un token qui vient d'être créé sur pump.fun (événement PumpPortal)."""

    mint: str
    name: str
    symbol: str
    creator: str
    uri: str = ""
    initial_buy: float = 0.0  # nb de tokens achetés par le dev à la création
    sol_amount: float = 0.0  # SOL dépensés par le dev à la création
    market_cap_sol: float = 0.0
    bonding_curve: str = ""
    v_tokens: float | None = None
    signature: str = ""
    pool: str = "pump"
    created_at: float = field(default_factory=time.time)

    @property
    def dev_buy_pct(self) -> float:
        return self.initial_buy / PUMP_TOTAL_SUPPLY * 100

    @classmethod
    def from_pumpportal(cls, msg: dict[str, Any]) -> NewToken | None:
        mint = msg.get("mint")
        creator = msg.get("traderPublicKey")
        if not mint or not creator:
            return None
        return cls(
            mint=str(mint),
            name=str(msg.get("name") or ""),
            symbol=str(msg.get("symbol") or ""),
            creator=str(creator),
            uri=str(msg.get("uri") or ""),
            initial_buy=_f(msg.get("initialBuy")),
            sol_amount=_f(msg.get("solAmount")),
            market_cap_sol=_f(msg.get("marketCapSol")),
            bonding_curve=str(msg.get("bondingCurveKey") or ""),
            v_tokens=_f_or_none(msg.get("vTokensInBondingCurve")),
            signature=str(msg.get("signature") or ""),
            pool=str(msg.get("pool") or "pump"),
        )


@dataclass
class Trade:
    mint: str
    trader: str
    is_buy: bool
    sol_amount: float
    token_amount: float
    new_balance: float | None
    market_cap_sol: float
    v_tokens: float | None = None
    ts: float = field(default_factory=time.time)

    @classmethod
    def from_pumpportal(cls, msg: dict[str, Any]) -> Trade | None:
        tx = msg.get("txType")
        if tx not in ("buy", "sell") or not msg.get("mint"):
            return None
        return cls(
            mint=str(msg["mint"]),
            trader=str(msg.get("traderPublicKey") or ""),
            is_buy=tx == "buy",
            sol_amount=_f(msg.get("solAmount")),
            token_amount=_f(msg.get("tokenAmount")),
            new_balance=_f_or_none(msg.get("newTokenBalance")),
            market_cap_sol=_f(msg.get("marketCapSol")),
            v_tokens=_f_or_none(msg.get("vTokensInBondingCurve")),
        )


@dataclass
class TradeStats:
    """Statistiques de trading accumulées pour un token surveillé."""

    created_at: float
    creator: str
    creator_initial: float = 0.0
    first_mcap: float = 0.0
    last_mcap: float = 0.0
    max_mcap: float = 0.0
    last_v_tokens: float | None = None
    buys: int = 0
    sells: int = 0
    buy_sol: float = 0.0
    sell_sol: float = 0.0
    buyers: set[str] = field(default_factory=set)
    sellers: set[str] = field(default_factory=set)
    early_buyers: set[str] = field(default_factory=set)
    creator_sold: bool = False
    creator_sold_sol: float = 0.0
    creator_balance: float | None = None
    balances: dict[str, float] = field(default_factory=dict)

    @classmethod
    def for_token(cls, token: NewToken) -> TradeStats:
        return cls(
            created_at=token.created_at,
            creator=token.creator,
            creator_initial=token.initial_buy,
            creator_balance=token.initial_buy,
            first_mcap=token.market_cap_sol,
            last_mcap=token.market_cap_sol,
            max_mcap=token.market_cap_sol,
            last_v_tokens=token.v_tokens,
        )

    def apply(self, t: Trade, early_window: float = 2.0) -> None:
        if t.is_buy:
            self.buys += 1
            self.buy_sol += t.sol_amount
            self.buyers.add(t.trader)
            if t.ts - self.created_at <= early_window and t.trader != self.creator:
                self.early_buyers.add(t.trader)
        else:
            self.sells += 1
            self.sell_sol += t.sol_amount
            self.sellers.add(t.trader)
            if t.trader == self.creator:
                self.creator_sold = True
                self.creator_sold_sol += t.sol_amount
        if t.new_balance is not None:
            self.balances[t.trader] = t.new_balance
            if t.trader == self.creator:
                self.creator_balance = t.new_balance
        if t.market_cap_sol > 0:
            if self.first_mcap <= 0:
                self.first_mcap = t.market_cap_sol
            self.last_mcap = t.market_cap_sol
            self.max_mcap = max(self.max_mcap, t.market_cap_sol)
        if t.v_tokens is not None:
            self.last_v_tokens = t.v_tokens

    @property
    def curve_progress(self) -> float | None:
        """Avancement de la bonding curve (100 % = migration vers l'AMM)."""
        if self.last_v_tokens is None:
            return None
        sold = PUMP_INITIAL_VIRTUAL_TOKENS - self.last_v_tokens
        return max(0.0, min(100.0, sold / PUMP_INITIAL_REAL_TOKENS * 100))


@dataclass
class TokenMetadata:
    description: str = ""
    twitter: str = ""
    telegram: str = ""
    website: str = ""
    image: str = ""

    @property
    def socials(self) -> list[str]:
        return [n for n, v in (("twitter", self.twitter), ("telegram", self.telegram),
                               ("site", self.website)) if v]


@dataclass
class HolderInfo:
    top10_pct: float
    max_holder_pct: float
    max_holder: str
    creator_pct: float | None
    accounts_checked: int


@dataclass
class RugcheckInfo:
    score_normalised: int | None
    dangers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class DexInfo:
    dex: str
    pair_address: str
    name: str
    symbol: str
    price_usd: float | None
    market_cap_usd: float | None
    liquidity_usd: float | None
    volume_h1: float | None
    volume_h24: float | None
    change_h1: float | None
    change_h24: float | None
    buys_h1: int | None
    sells_h1: int | None
    pair_created_at: float | None  # epoch secondes


@dataclass
class CreatorInfo:
    launches_24h: int  # AUTRES tokens du même créateur vus par le bot sur 24 h
    launches_total_api: int | None = None  # via l'API pump.fun si dispo


@dataclass
class RiskReport:
    safety: int  # 0..100, plus c'est haut moins c'est risqué
    flags: list[tuple[str, str]]  # (niveau, texte) niveau: ok|warn|danger|critical
    critical: bool

    @property
    def verdict(self) -> tuple[str, str]:
        if self.critical or self.safety < 40:
            return "🔴", "DANGER"
        if self.safety < 70:
            return "🟡", "RISQUÉ"
        return "🟢", "RISQUE MODÉRÉ"


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _f_or_none(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
