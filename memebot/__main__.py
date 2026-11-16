"""Point d'entrée :

    python -m memebot              # mode réel
    python -m memebot --demo       # démo hors-ligne
    python -m memebot check <mint> # analyse d'un token dans le terminal
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import httpx

from .ai import AIAnalyst
from .config import Settings, load_settings
from .data import LiveData
from .engine import Engine
from .pumpportal import PumpPortalClient
from .sources.rss import Feed, RSSSource
from .sources.x_api import XAccount, XSource
from .storage import Storage
from .telegram import Notifier, strip_tags
from .trading.executor import LiveExecutor, PaperExecutor, load_keypair
from .trading.trader import Trader

log = logging.getLogger("memebot")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s : %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "websockets", "anthropic", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_live(settings: Settings, http: httpx.AsyncClient) -> tuple[Engine, Notifier]:
    notifier = Notifier(http, settings.telegram_token, settings.telegram_chat_id)
    data = LiveData(http, settings.rpc_url, ipfs_gateway=settings.ipfs_gateway,
                    use_pumpfun_api=settings.use_pumpfun_api, use_rugcheck=settings.use_rugcheck)
    ai = AIAnalyst(settings.ai_model, settings.ai_effort) if settings.ai_active else None
    engine = Engine(settings, Storage(settings.db_path), notifier, data, None, ai)
    return engine, notifier


def startup_warnings(s: Settings) -> list[str]:
    w = []
    if not s.telegram_token:
        w.append("Pas de TELEGRAM_BOT_TOKEN : alertes uniquement dans ce terminal.")
    elif not s.telegram_chat_id:
        w.append("Pas de TELEGRAM_CHAT_ID : envoie /start à ton bot Telegram pour l'obtenir.")
    if s.x_accounts and not s.x_bearer:
        w.append("Pas de X_BEARER_TOKEN : les comptes X ne sont pas surveillés (RSS et /narratif seulement).")
    if not s.ai_active:
        w.append("IA désactivée (pas d'ANTHROPIC_API_KEY) : extraction de mots-clés heuristique.")
    if "api.mainnet-beta.solana.com" in s.rpc_url:
        w.append("RPC Solana public : lent et limité. Prends une clé gratuite (Helius, etc.) pour SOLANA_RPC_URL.")
    return w


async def build_trader(settings: Settings, engine: Engine, http: httpx.AsyncClient,
                       feed: PumpPortalClient) -> Trader | None:
    ts = settings.trading
    if ts.mode not in ("paper", "live"):
        log.info("Trading automatique désactivé (trading.mode: %s)", ts.mode)
        return None
    if ts.mode == "live":
        if not settings.wallet_secret:
            log.error("trading.mode = live mais WALLET_PRIVATE_KEY est vide : trading désactivé.")
            return None
        try:
            keypair = load_keypair(settings.wallet_secret)
        except Exception:  # noqa: BLE001 - ne jamais afficher la clé dans les logs
            log.error("WALLET_PRIVATE_KEY illisible (format base58 ou tableau JSON attendu).")
            return None
        executor = LiveExecutor(http, settings.rpc_url, keypair,
                                slippage_pct=ts.slippage_pct, priority_fee_sol=ts.priority_fee_sol)
        try:
            balance = await executor.balance()
        except Exception as e:  # noqa: BLE001
            log.error("Impossible de lire le solde du wallet (%s) : trading désactivé.", e)
            return None
        if balance > ts.wallet_max_balance_sol:
            log.error("Le wallet contient %.3f SOL (> %.3f autorisés). Utilise un wallet dédié "
                      "avec peu d'argent. Trading désactivé.", balance, ts.wallet_max_balance_sol)
            return None
        log.warning("💸 MODE RÉEL ACTIF — wallet %s — solde %.4f SOL", executor.pubkey, balance)
        trader = Trader(ts, executor, engine.storage, engine.notifier, feed=feed,
                        sol_usd=lambda: engine.sol_usd)
    else:
        trader = Trader(ts, None, engine.storage, engine.notifier, feed=feed,
                        sol_usd=lambda: engine.sol_usd)
        trader.ex = PaperExecutor(trader.price_of, fee_pct=ts.fee_pct, tx_fee_sol=ts.tx_fee_sol,
                                  latency=ts.paper_latency_seconds)
        log.info("Trading en mode PAPIER (argent virtuel) — capital %.4f SOL", trader.cash)
    trader.release = engine.release_mint
    engine.trader = trader
    return trader


async def run_live(settings: Settings) -> None:
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=50)) as http:
        engine, notifier = build_live(settings, http)
        feed = PumpPortalClient(engine.on_new_token, engine.on_trade)
        engine.feed = feed
        coros = [feed.run(), engine.housekeeping()]
        trader = await build_trader(settings, engine, http, feed)
        if trader:
            coros.append(trader.run())

        if settings.x_bearer and settings.x_accounts:
            accounts = [XAccount(username=a["username"].lstrip("@"),
                                 weight=float(a.get("weight", 0.8)),
                                 aliases=tuple(a.get("aliases") or ()))
                        for a in settings.x_accounts if a.get("username")]
            coros.append(XSource(http, settings.x_bearer, accounts, engine.on_event,
                                 poll_seconds=settings.x_poll_seconds,
                                 include_replies=settings.x_include_replies).run())
        if settings.rss_feeds:
            feeds = [Feed(name=f.get("name") or f["url"], url=f["url"],
                          weight=float(f.get("weight", 0.4)), aliases=tuple(f.get("aliases") or ()))
                     for f in settings.rss_feeds if f.get("url")]
            coros.append(RSSSource(http, feeds, engine.on_event,
                                   poll_seconds=settings.rss_poll_seconds,
                                   max_age_seconds=settings.ttl_minutes * 60,
                                   already_seen=engine.storage.event_seen).run())
        if settings.telegram_token:
            coros.append(notifier.poll_commands(engine.handle_command))

        for w in startup_warnings(settings):
            log.warning(w)
        mode = {"live": "💸 trading RÉEL", "paper": "🧪 trading papier"}.get(
            trader.mode if trader else "", "trading coupé")
        await notifier.send(f"🟢 <b>Memecoin Radar démarré</b> ({mode}). Tape /aide pour les commandes.")
        try:
            await asyncio.gather(*coros)
        finally:
            for t in list(engine.tasks) + (list(trader.tasks) if trader else []):
                t.cancel()
            engine.storage.close()


async def run_check(settings: Settings, mint: str) -> None:
    async with httpx.AsyncClient() as http:
        engine, _ = build_live(settings, http)
        engine.sol_usd = await engine.data.sol_usd()
        print(strip_tags(await engine.handle_command(f"/check {mint}") or ""))
        engine.storage.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memebot", description="Radar à memecoins pump.fun")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "check"])
    parser.add_argument("mint", nargs="?", help="adresse du token pour « check »")
    parser.add_argument("--demo", action="store_true", help="démo hors-ligne")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--env", default=".env")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    try:
        if args.demo:
            from .demo import run_demo
            asyncio.run(run_demo())
            return 0
        settings = load_settings(args.config, args.env)
        if args.command == "check":
            if not args.mint:
                parser.error("donne l'adresse du token : python -m memebot check <mint>")
            asyncio.run(run_check(settings, args.mint))
            return 0
        asyncio.run(run_live(settings))
    except KeyboardInterrupt:
        print("\nArrêt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
