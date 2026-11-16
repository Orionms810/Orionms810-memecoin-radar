import time

from memebot.models import (
    CreatorInfo,
    HolderInfo,
    NewToken,
    RugcheckInfo,
    TokenMetadata,
    Trade,
    TradeStats,
)
from memebot.risk import assess_risk, momentum_score

CREATE_MSG = {
    "signature": "sig", "mint": "MintAAA", "traderPublicKey": "DevWallet", "txType": "create",
    "initialBuy": 50_000_000, "solAmount": 1.5, "bondingCurveKey": "Curve",
    "vTokensInBondingCurve": 1_023_000_000, "vSolInBondingCurve": 31.5, "marketCapSol": 30.8,
    "name": "Test", "symbol": "TST", "uri": "https://ipfs.io/ipfs/Qm", "pool": "pump",
}


def test_new_token_from_pumpportal():
    t = NewToken.from_pumpportal(CREATE_MSG)
    assert t.mint == "MintAAA" and t.creator == "DevWallet" and t.bonding_curve == "Curve"
    assert round(t.dev_buy_pct, 2) == 5.0
    assert NewToken.from_pumpportal({"txType": "create"}) is None


def test_trade_parsing_and_stats():
    t = NewToken.from_pumpportal(CREATE_MSG)
    stats = TradeStats.for_token(t)
    buy = Trade.from_pumpportal({"mint": "MintAAA", "txType": "buy", "traderPublicKey": "A",
                                 "solAmount": 2, "tokenAmount": 1e7, "newTokenBalance": 1e7,
                                 "marketCapSol": 40, "vTokensInBondingCurve": 900_000_000})
    assert Trade.from_pumpportal({"txType": "create", "mint": "x"}) is None
    buy.ts = t.created_at + 0.5
    stats.apply(buy)
    assert stats.buys == 1 and stats.buy_sol == 2 and "A" in stats.early_buyers
    assert stats.max_mcap == 40 and round(stats.curve_progress) == 22

    sell = Trade(mint="MintAAA", trader="DevWallet", is_buy=False, sol_amount=3,
                 token_amount=5e7, new_balance=0.0, market_cap_sol=20)
    stats.apply(sell)
    assert stats.creator_sold and stats.creator_balance == 0.0
    assert stats.last_mcap == 20 and stats.max_mcap == 40


def test_clean_token_is_rated_moderate():
    r = assess_risk(dev_buy_pct=1.5, stats=None,
                    holders=HolderInfo(15, 3, "w", 1.5, 20), rugcheck=RugcheckInfo(5),
                    creator=CreatorInfo(0), metadata=TokenMetadata(twitter="x"))
    assert r.safety == 100 and not r.critical and r.verdict[1] == "RISQUE MODÉRÉ"


def test_creator_sold_is_critical():
    stats = TradeStats(created_at=time.time(), creator="Dev")
    stats.creator_sold, stats.creator_sold_sol = True, 4.0
    r = assess_risk(dev_buy_pct=1, stats=stats, holders=None, rugcheck=None, creator=None,
                    metadata=None)
    assert r.critical and r.verdict == ("🔴", "DANGER")
    assert any("vendu (4.00 SOL)" in txt for _, txt in r.flags)


def test_creator_exited_detected_from_holders():
    r = assess_risk(dev_buy_pct=8, stats=None, holders=HolderInfo(20, 4, "w", 0.0, 20),
                    rugcheck=None, creator=None, metadata=None)
    assert any(lvl == "danger" and "vendu ou transféré" in txt for lvl, txt in r.flags)


def test_red_flags_accumulate():
    stats = TradeStats(created_at=time.time(), creator="Dev")
    stats.early_buyers = {f"s{i}" for i in range(10)}
    r = assess_risk(dev_buy_pct=20, stats=stats, holders=HolderInfo(55, 20, "w", 20, 20),
                    rugcheck=RugcheckInfo(80, dangers=["Freeze authority"]),
                    creator=CreatorInfo(launches_24h=5), metadata=TokenMetadata())
    levels = [lvl for lvl, _ in r.flags]
    assert levels.count("danger") >= 5 and r.safety == 0 and r.verdict[1] == "DANGER"


def test_serial_creator_from_api_count():
    r = assess_risk(dev_buy_pct=None, stats=None, holders=None, rugcheck=None,
                    creator=CreatorInfo(0, launches_total_api=25), metadata=None)
    assert any(lvl == "danger" and "série" in txt for lvl, txt in r.flags)


def test_momentum():
    stats = TradeStats(created_at=time.time(), creator="Dev", first_mcap=30, last_mcap=45)
    assert momentum_score(None) == 0
    assert momentum_score(stats) == 10  # seule la hausse de mcap compte
    stats.buyers = {f"b{i}" for i in range(25)} | {"Dev"}
    stats.buy_sol = 12
    assert momentum_score(stats) == 90  # 40 acheteurs + 25 volume + 10 hausse + 15 flux net
