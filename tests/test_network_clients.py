"""Clients réseau testés avec des réponses simulées (aucun accès internet)."""

import json

import httpx

from memebot.data import LiveData
from memebot.models import NewToken, Trade
from memebot.pumpportal import PumpPortalClient
from memebot.sources.rss import Feed, RSSSource
from memebot.sources.x_api import XAccount, XSource
from memebot.telegram import Notifier


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_holders_excludes_bonding_curve():
    def handler(req: httpx.Request):
        body = json.loads(req.content)
        m = body["method"]
        if m == "getTokenLargestAccounts":
            value = [{"address": "acc_curve", "uiAmount": 700_000_000},
                     {"address": "acc_dev", "uiAmount": 40_000_000},
                     {"address": "acc_w1", "uiAmount": 20_000_000}]
            return httpx.Response(200, json={"result": {"value": value}})
        if m == "getTokenSupply":
            return httpx.Response(200, json={"result": {"value": {"uiAmountString": "1000000000"}}})
        if m == "getMultipleAccounts":
            owners = ["CURVE", "DEV", "W1"]
            value = [{"data": {"parsed": {"info": {"owner": o}}}} for o in owners]
            return httpx.Response(200, json={"result": {"value": value}})
        return httpx.Response(400)

    async with client(handler) as http:
        h = await LiveData(http, "https://rpc").holders("MINT", exclude_owners={"CURVE"},
                                                         creator="DEV")
    assert round(h.top10_pct) == 6 and h.max_holder == "DEV"
    assert round(h.creator_pct) == 4 and h.accounts_checked == 3


async def test_rugcheck_and_dexscreener_parsing():
    def handler(req: httpx.Request):
        if "rugcheck" in req.url.host:
            return httpx.Response(200, json={"score_normalised": 42, "risks": [
                {"name": "Freeze Authority still enabled", "level": "danger", "value": ""},
                {"name": "Low Liquidity", "level": "warn", "value": "$900"},
                {"name": "Info", "level": "info"}]})
        if "dexscreener" in req.url.host:
            return httpx.Response(200, json=[
                {"dexId": "pumpswap", "pairAddress": "P1", "priceUsd": "0.0001",
                 "marketCap": 120000, "liquidity": {"usd": 30000},
                 "volume": {"h1": 5000, "h24": 90000}, "priceChange": {"h1": -5, "h24": 120},
                 "txns": {"h1": {"buys": 50, "sells": 40}}, "pairCreatedAt": 1700000000000,
                 "baseToken": {"name": "Nugget", "symbol": "NUGGET"}},
                {"dexId": "raydium", "pairAddress": "P2", "liquidity": {"usd": 10}}])
        return httpx.Response(404)

    async with client(handler) as http:
        data = LiveData(http, "https://rpc")
        rc = await data.rugcheck("MINT")
        dex = await data.dexscreener("MINT")
    assert rc.score_normalised == 42
    assert rc.dangers == ["Freeze Authority still enabled"]
    assert rc.warnings == ["Low Liquidity ($900)"]
    assert dex.pair_address == "P1" and dex.symbol == "NUGGET" and dex.buys_h1 == 50
    assert dex.pair_created_at == 1700000000


async def test_live_data_survives_network_errors():
    def handler(req):
        raise httpx.ConnectError("boom")

    async with client(handler) as http:
        data = LiveData(http, "https://rpc")
        assert await data.rugcheck("M") is None
        assert await data.holders("M", exclude_owners=set()) is None
        assert await data.metadata("https://ipfs.io/ipfs/x") is None
        assert await data.sol_usd() is None


async def test_x_source_seeds_then_emits_new_posts():
    calls = {"n": 0}

    def handler(req: httpx.Request):
        assert req.headers["Authorization"] == "Bearer TOKEN"
        if "/users/by/username/" in req.url.path:
            return httpx.Response(200, json={"data": {"id": "42"}})
        calls["n"] += 1
        if calls["n"] == 1:
            assert "since_id" not in req.url.params
            return httpx.Response(200, json={"data": [{"id": "100", "text": "old"}],
                                             "meta": {"newest_id": "100"}})
        assert req.url.params["since_id"] == "100"
        return httpx.Response(200, json={
            "data": [{"id": "102", "text": "short", "note_tweet": {"text": "LONG NUGGET post"}},
                     {"id": "101", "text": "first new"}],
            "meta": {"newest_id": "102"}})

    events = []

    async def emit(ev):
        events.append(ev)

    async with client(handler) as http:
        src = XSource(http, "TOKEN", [], emit)
        acc = XAccount("realDonaldTrump", weight=1.0)
        await src._poll(acc)
        assert events == []
        await src._poll(acc)
    assert [e.text for e in events] == ["first new", "LONG NUGGET post"]
    assert events[1].url == "https://x.com/realDonaldTrump/status/102"
    assert acc.since_id == "102"


async def test_x_source_rate_limit():
    def handler(req):
        return httpx.Response(429, headers={"x-rate-limit-reset": "0"})

    async def emit(ev):
        raise AssertionError

    async with client(handler) as http:
        acc = XAccount("elonmusk")
        await XSource(http, "T", [], emit)._poll(acc)
    assert acc.next_allowed > 0 and acc.user_id is None


RSS_1 = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><guid>a</guid><title>Old news</title><link>https://n/a</link></item>
</channel></rss>"""
RSS_2 = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><guid>b</guid><title>Breaking: GIANT HAMSTER</title>
<description>&lt;p&gt;A giant hamster named &lt;b&gt;NUGGET&lt;/b&gt;&lt;/p&gt;</description>
<link>https://n/b</link></item>
<item><guid>a</guid><title>Old news</title><link>https://n/a</link></item>
</channel></rss>"""


async def test_rss_seeds_then_emits_new_items():
    pages = iter([RSS_1, RSS_2, RSS_2])

    def handler(req):
        return httpx.Response(200, content=next(pages).encode())

    events = []

    async def emit(ev):
        events.append(ev)

    async with client(handler) as http:
        src = RSSSource(http, [], emit)
        feed = Feed("News", "https://n/rss", weight=0.4)
        assert await src.poll(feed) == []
        await src.poll(feed)
        await src.poll(feed)
    assert len(events) == 1
    assert events[0].text == "Breaking: GIANT HAMSTER — A giant hamster named NUGGET"
    assert events[0].weight == 0.4 and events[0].url == "https://n/b"


def test_pumpportal_dispatch():
    tokens, trades = [], []
    pp = PumpPortalClient(tokens.append, trades.append)
    pp.dispatch(json.dumps({"message": "Successfully subscribed"}))
    pp.dispatch("not json")
    pp.dispatch(json.dumps({"txType": "create", "mint": "M", "traderPublicKey": "D",
                            "name": "N", "symbol": "S"}))
    pp.dispatch(json.dumps({"txType": "sell", "mint": "M", "traderPublicKey": "D",
                            "solAmount": 1, "marketCapSol": 30}))
    assert isinstance(tokens[0], NewToken) and isinstance(trades[0], Trade)
    assert trades[0].is_buy is False


async def test_pumpportal_subscriptions_are_tracked_while_disconnected():
    pp = PumpPortalClient(lambda t: None, lambda t: None)
    await pp.subscribe_trades(["A", "B"])
    await pp.subscribe_trades(["A"])
    await pp.unsubscribe_trades(["B"])
    assert pp._trade_keys == {"A"}


async def test_telegram_send_and_chat_filter():
    sent = []

    def handler(req: httpx.Request):
        body = json.loads(req.content) if req.content else {}
        if req.url.path.endswith("/sendMessage"):
            sent.append(body)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
        return httpx.Response(404)

    async with client(handler) as http:
        n = Notifier(http, "TOKEN", "123", console=False)
        assert await n.send("<b>hi</b> " + "x" * 5000, reply_to=3) == 7
        n.muted = True
        assert await n.send("muted") is None
        assert await n.send("forced", force=True) == 7
    assert len(sent) == 2
    assert sent[0]["parse_mode"] == "HTML" and len(sent[0]["text"]) <= 4000
    assert sent[0]["reply_parameters"]["message_id"] == 3


async def test_x_source_uses_start_time_when_first_page_empty():
    seen_params = []

    def handler(req: httpx.Request):
        if "/users/by/username/" in req.url.path:
            return httpx.Response(200, json={"data": {"id": "42"}})
        seen_params.append(dict(req.url.params))
        return httpx.Response(200, json={"meta": {"result_count": 0}})

    async def emit(ev):
        pass

    async with client(handler) as http:
        src = XSource(http, "T", [], emit)
        acc = XAccount("someone")
        await src._poll(acc)
        await src._poll(acc)
    assert "start_time" not in seen_params[0]
    assert seen_params[1]["start_time"] == acc.seeded_at


async def test_live_data_reads_bonding_curves_in_batches():
    import base64 as b64
    import struct

    from memebot.curves import CURVE_DISCRIMINATOR

    calls = []

    def account(v_tok, v_sol, real_sol, complete):
        raw = (CURVE_DISCRIMINATOR + struct.pack("<QQQQQ", v_tok, v_sol, 0, real_sol, 10**15)
               + bytes([1 if complete else 0]) + b"\0" * 40)
        return {"data": [b64.b64encode(raw).decode(), "base64"], "owner": "6EF8"}

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        addrs = body["params"][0]
        calls.append(len(addrs))
        value = [account(10**15, 60 * 10**9, 5 * 10**9, a == "C1") if a != "GONE" else None
                 for a in addrs]
        return httpx.Response(200, json={"result": {"value": value}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        data = LiveData(http, "https://rpc")
        addrs = [f"C{i}" for i in range(150)] + ["GONE", "C0"]
        curves = await data.curves(addrs)
    assert calls == [100, 51]  # 100 comptes max par requête, doublons retirés
    assert "GONE" not in curves and len(curves) == 150
    assert curves["C1"].complete and not curves["C2"].complete
    assert curves["C2"].real_sol == 5.0 and curves["C2"].market_cap_sol == 60.0
