"""Démo hors-ligne : simule un post + des lancements pump.fun pour voir à quoi
ressemblent les alertes, sans aucune clé ni connexion internet.

    python -m memebot --demo
"""

from __future__ import annotations

import asyncio
import random
import string
import tempfile
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .engine import Engine
from .funding import FundingInfo
from .models import (
    DexInfo,
    HolderInfo,
    NewsEvent,
    NewToken,
    RugcheckInfo,
    TokenMetadata,
    Trade,
)
from .storage import Storage
from .telegram import Notifier, strip_tags
from .trading.paper import PaperExecutor
from .trading.settings import TradingSettings
from .trading.trader import Trader

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def fake_address(seed: str) -> str:
    rnd = random.Random(seed)
    return "".join(rnd.choice(B58) for _ in range(44))


class FakeData:
    def __init__(self) -> None:
        self.holders_by_mint: dict[str, HolderInfo] = {}
        self.meta_by_uri: dict[str, TokenMetadata] = {}
        self.rug_by_mint: dict[str, RugcheckInfo] = {}
        self.funding_by_creator: dict[str, FundingInfo] = {}
        self.coins_by_creator: dict[str, list[dict[str, Any]]] = {}
        self.curves_by_address: dict[str, Any] = {}
        self.exited: set[tuple[str, str]] = set()

    async def metadata(self, uri: str) -> TokenMetadata | None:
        return self.meta_by_uri.get(uri)

    async def holders(self, mint: str, *, exclude_owners: set[str],
                      creator: str | None = None) -> HolderInfo | None:
        return self.holders_by_mint.get(mint)

    async def rugcheck(self, mint: str) -> RugcheckInfo | None:
        return self.rug_by_mint.get(mint)

    async def curves(self, addresses: list[str]) -> dict[str, Any]:
        return {a: self.curves_by_address[a] for a in addresses if a in self.curves_by_address}

    async def creator_coins(self, creator: str) -> list[dict[str, Any]] | None:
        return self.coins_by_creator.get(creator)

    async def creator_exits(self, creator: str, mints: list[str]) -> dict[str, bool] | None:
        return {m: True for m in mints if (creator, m) in self.exited}

    async def pump_coin(self, mint: str) -> dict[str, Any] | None:
        return None

    async def dexscreener(self, mint: str) -> DexInfo | None:
        return None

    async def sol_usd(self) -> float | None:
        return 150.0

    async def funding(self, buyers: list[str], *, creator: str | None = None) -> FundingInfo | None:
        return self.funding_by_creator.get(creator or "")


class FakeFeed:
    connected = True

    async def subscribe_trades(self, mints: list[str]) -> None:
        pass

    async def unsubscribe_trades(self, mints: list[str]) -> None:
        pass


def make_token(name: str, symbol: str, creator: str, dev_pct: float, mcap: float = 28.0) -> NewToken:
    mint = fake_address(name + symbol)[:40] + "pump"
    return NewToken(mint=mint, name=name, symbol=symbol, creator=creator,
                    uri=f"https://ipfs.io/ipfs/{symbol}", initial_buy=dev_pct / 100 * 1e9,
                    sol_amount=dev_pct / 3, market_cap_sol=mcap,
                    bonding_curve=fake_address("curve" + symbol),
                    v_tokens=1_073_000_000 - dev_pct / 100 * 1e9)


def buys(engine: Engine, token: NewToken, n: int, sol: float, start_mcap: float,
         step: float) -> float:
    mcap = start_mcap
    for i in range(n):
        mcap += step
        engine.on_trade(Trade(mint=token.mint, trader=fake_address(f"{token.symbol}-b{i}"),
                              is_buy=True, sol_amount=sol, token_amount=sol * 1e6,
                              new_balance=sol * 1e6, market_cap_sol=mcap))
    return mcap


async def run_demo() -> None:
    random.seed(1)
    tmp = Path(tempfile.mkdtemp()) / "demo.db"
    settings = Settings(observe_seconds=3, watch_minutes=0.15, min_momentum=10,
                        notify_narrative_min_weight=0.8, db_path=str(tmp))
    data = FakeData()
    notifier = Notifier(console=True)
    engine = Engine(settings, Storage(tmp), notifier, data, FakeFeed(), ai=None)
    engine.sol_usd = 150.0
    # Trading en mode papier avec ~7 $ virtuels
    trader = Trader(TradingSettings(paper_latency_seconds=0.3), None, engine.storage, notifier,
                    feed=engine.feed, sol_usd=lambda: engine.sol_usd)
    trader.ex = PaperExecutor(trader.price_of, fee_pct=1.75, tx_fee_sol=0.0003, latency=0.3)
    trader.release = engine.release_mint
    engine.trader = trader

    print("=== DÉMO : un compte surveillé poste quelque chose de « memeable » ===")
    await engine.on_event(NewsEvent(
        source="x", source_name="@realDonaldTrump", event_id="demo:1",
        text=("Just had lunch with the smartest hamster in the world, his name is NUGGET. "
              "Beautiful animal, tremendous energy! #NuggetNation"),
        url="https://x.com/realDonaldTrump/status/0", weight=1.0, aliases=("trump", "donald")))

    serial = fake_address("serial-dev")
    for i in range(4):  # un créateur en série a déjà lancé 4 tokens, et les a tous vidés
        old = make_token(f"Old scam {i}", "SCAM" + string.ascii_uppercase[i], serial, 20)
        old.created_at = time.time() - 3600 * (i + 1)
        engine.on_new_token(old)
        data.exited.add((serial, old.mint))

    good = make_token("Nugget the Hamster", "NUGGET", fake_address("dev-good"), 2.0)
    data.meta_by_uri[good.uri] = TokenMetadata(
        description="The hamster who had lunch with the president", twitter="https://x.com/nugget",
        telegram="https://t.me/nugget")
    data.holders_by_mint[good.mint] = HolderInfo(18.0, 3.1, fake_address("w1"), 2.0, 20)
    data.rug_by_mint[good.mint] = RugcheckInfo(score_normalised=12)

    rug = make_token("NUGGET", "NUGG", serial, 25.0)
    data.holders_by_mint[rug.mint] = HolderInfo(48.0, 25.0, serial, 25.0, 20)

    snipe = make_token("Nugget Nation", "NN", fake_address("dev-snipe"), 4.0)
    data.meta_by_uri[snipe.uri] = TokenMetadata(description="nugget nation")
    data.holders_by_mint[snipe.mint] = HolderInfo(34.0, 9.0, fake_address("w2"), 4.0, 20)
    # Les 9 « acheteurs » de Nugget Nation ont tous été financés par le créateur : faux acheteurs
    data.funding_by_creator[snipe.creator] = FundingInfo(
        analyzed=8, fresh=8, max_shared=8, shared_funder=snipe.creator, linked_to_creator=8,
        shared_is_creator_source=True)

    unrelated = make_token("Random Pepe", "RPEPE", fake_address("dev-pepe"), 1.0)

    print("\n=== 4 nouveaux tokens arrivent sur pump.fun ===")
    for t in (good, rug, snipe, unrelated):
        t.created_at = time.time()
        engine.on_new_token(t)
    # Snipers : 9 achats dans la même seconde sur « Nugget Nation »
    buys(engine, snipe, 9, 0.8, snipe.market_cap_sol, 1.2)
    # Le vrai engouement arrive sur NUGGET, un peu plus tard
    await asyncio.sleep(2.2)
    mcap_good = buys(engine, good, 25, 0.6, good.market_cap_sol, 0.9)
    buys(engine, rug, 3, 0.2, rug.market_cap_sol, 0.2)

    await asyncio.sleep(settings.observe_seconds + 1.5)

    print("\n=== Quelques secondes après l'alerte : le dev de NUGGET vend tout ===")
    engine.on_trade(Trade(mint=good.mint, trader=good.creator, is_buy=False, sol_amount=3.4,
                          token_amount=good.initial_buy, new_balance=0.0,
                          market_cap_sol=mcap_good * 0.45))
    await asyncio.sleep(settings.watch_minutes * 60 + 1)

    for cmd in ("/narratifs", "/bilan", f"/check {good.mint}", "/trading", "/journal", "/status"):
        print(f"\n>>> {cmd}")
        print(strip_tags(await engine.handle_command(cmd) or ""))
    for task in list(engine.tasks) + list(trader.tasks):
        task.cancel()
    print("\nFin de la démo. Lance « python -m memebot » pour le mode réel.")
