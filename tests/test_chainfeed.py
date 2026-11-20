import asyncio
import base64
import json
import struct
import time

import pytest
import websockets

from memebot.chainfeed import (
    TRADE_DISCRIMINATOR,
    SolanaTradeFeed,
    b58encode,
    decode_trades,
    ws_url_from_rpc,
)

MINT = bytes(range(1, 33))
USER = bytes(range(101, 133))
OTHER_MINT = bytes([7] * 32)


def trade_log(mint=MINT, user=USER, sol=1_500_000_000, tokens=2_000_000_000_000, is_buy=1,
              ts=None, v_sol=60_000_000_000, v_tok=536_500_000_000_000, extra=b"\0" * 200,
              disc=TRADE_DISCRIMINATOR):
    ts = int(time.time()) if ts is None else ts
    payload = (disc + mint + struct.pack("<QQ", sol, tokens) + bytes([is_buy]) + user
               + struct.pack("<qQQQQ", ts, v_sol, v_tok, 1, 1) + extra)
    return "Program data: " + base64.b64encode(payload).decode()


def test_b58_and_ws_url():
    assert b58encode(b"\0\0\x01") == "112"
    solders = pytest.importorskip("solders.pubkey")
    for raw in (MINT, USER, bytes(32), bytes([0, 0, 5] + [9] * 29)):
        assert b58encode(raw) == str(solders.Pubkey(raw))
    assert ws_url_from_rpc("https://mainnet.helius-rpc.com/?api-key=K") == \
        "wss://mainnet.helius-rpc.com/?api-key=K"
    assert ws_url_from_rpc("http://localhost:8899") == "ws://localhost:8899"


def test_decode_trade_event():
    logs = ["Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]",
            "Program log: Instruction: Buy", trade_log(), "Program data: ???pas-du-base64",
            trade_log(disc=b"\x01" * 8), "Program data: " + base64.b64encode(b"court").decode()]
    trades = decode_trades(logs)
    assert len(trades) == 1
    t = trades[0]
    assert t.mint == b58encode(MINT) and t.trader == b58encode(USER) and t.is_buy
    assert t.sol_amount == 1.5 and t.token_amount == 2_000_000
    assert t.market_cap_sol == pytest.approx(60e9 * 1e6 / 536.5e12)  # ~111.8 SOL
    assert t.v_tokens == pytest.approx(536_500_000)
    assert t.new_balance is None


def test_decode_launch_market_cap_and_sanity_checks():
    launch = decode_trades([trade_log(v_sol=30_000_000_000, v_tok=1_073_000_000_000_000)])[0]
    assert launch.market_cap_sol == pytest.approx(27.96, abs=0.01)  # mcap de départ pump.fun
    assert decode_trades([trade_log(is_buy=0)])[0].is_buy is False
    assert decode_trades([trade_log(is_buy=2)]) == []  # format inattendu : ignoré
    assert decode_trades([trade_log(ts=0)]) == []
    assert decode_trades([trade_log(v_tok=0)]) == []


@pytest.fixture
def no_proxy(monkeypatch):
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


async def test_feed_against_fake_rpc_websocket(no_proxy):
    requests, received = [], []
    mint_b58 = b58encode(MINT)

    async def handler(ws):
        async for raw in ws:
            req = json.loads(raw)
            requests.append(req)
            if req["method"] == "logsSubscribe":
                sub_id = 1000 + req["id"]
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": sub_id}))
                if req["params"][0]["mentions"] == [mint_b58]:
                    note = {"jsonrpc": "2.0", "method": "logsNotification", "params": {
                        "subscription": sub_id, "result": {"value": {"err": None, "logs": [
                            trade_log(), trade_log(mint=OTHER_MINT)]}}}}
                    failed = {"jsonrpc": "2.0", "method": "logsNotification", "params": {
                        "subscription": sub_id, "result": {"value": {
                            "err": {"InstructionError": [0, "x"]}, "logs": [trade_log()]}}}}
                    await ws.send(json.dumps(failed))
                    await ws.send(json.dumps(note))
            elif req["method"] == "logsUnsubscribe":
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": True}))

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        feed = SolanaTradeFeed(f"ws://127.0.0.1:{port}", received.append,
                               connect_kwargs={"proxy": None})
        await feed.subscribe_trades([mint_b58])  # avant la connexion : gardé pour plus tard
        task = asyncio.ensure_future(feed.run())
        for _ in range(100):
            await asyncio.sleep(0.02)
            if received:
                break
        assert feed.connected
        assert len(received) == 1  # tx en erreur ignorée, autre token ignoré
        assert received[0].mint == mint_b58 and received[0].is_buy
        assert feed._sub_of[mint_b58] >= 1000

        await feed.unsubscribe_trades([mint_b58])
        await asyncio.sleep(0.05)
        assert requests[-1]["method"] == "logsUnsubscribe"
        assert requests[-1]["params"][0] >= 1000
        assert mint_b58 not in feed._sub_of
        task.cancel()


async def test_subscription_limit():
    feed = SolanaTradeFeed("ws://unused", lambda t: None, max_subs=2)
    await feed.subscribe_trades(["A", "B", "C"])
    assert feed._wanted == {"A", "B"}
    await feed.unsubscribe_trades(["A"])
    await feed.subscribe_trades(["C"])
    assert feed._wanted == {"B", "C"}
