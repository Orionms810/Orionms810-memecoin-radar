"""Base SQLite locale : tokens vus, posts déjà traités, alertes et bilan
« paper trading » (ce que tu aurais gagné/perdu en suivant les alertes)."""

from __future__ import annotations

import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any

from .models import NewToken

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens(
    mint TEXT PRIMARY KEY, name TEXT, symbol TEXT, creator TEXT, uri TEXT,
    initial_buy REAL, market_cap_sol REAL, bonding_curve TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS idx_tokens_creator ON tokens(creator, created_at);
CREATE TABLE IF NOT EXISTS events(
    event_id TEXT PRIMARY KEY, source TEXT, source_name TEXT, text TEXT,
    url TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT, name TEXT, symbol TEXT,
    narrative TEXT, match_score REAL, safety INTEGER, momentum INTEGER,
    verdict TEXT, entry_mcap_sol REAL, max_mcap_sol REAL, end_mcap_sol REAL,
    creator_sold INTEGER DEFAULT 0, created_at REAL, closed_at REAL);
"""


class Storage:
    def __init__(self, path: str | Path = "memebot.db"):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ----------------------------------------------------------------- tokens
    def add_token(self, t: NewToken) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO tokens VALUES (?,?,?,?,?,?,?,?,?)",
            (t.mint, t.name, t.symbol, t.creator, t.uri, t.initial_buy,
             t.market_cap_sol, t.bonding_curve, t.created_at),
        )
        self.db.commit()

    def get_token(self, mint: str) -> NewToken | None:
        row = self.db.execute("SELECT * FROM tokens WHERE mint=?", (mint,)).fetchone()
        if not row:
            return None
        return NewToken(
            mint=row["mint"], name=row["name"], symbol=row["symbol"],
            creator=row["creator"], uri=row["uri"], initial_buy=row["initial_buy"],
            market_cap_sol=row["market_cap_sol"], bonding_curve=row["bonding_curve"],
            created_at=row["created_at"],
        )

    def creator_launches(self, creator: str, since: float) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) FROM tokens WHERE creator=? AND created_at>=?",
            (creator, since)).fetchone()
        return int(row[0])

    def count_tokens(self, since: float = 0) -> int:
        return int(self.db.execute(
            "SELECT COUNT(*) FROM tokens WHERE created_at>=?", (since,)).fetchone()[0])

    def prune(self, older_than_days: float = 3) -> None:
        now = time.time()
        self.db.execute("DELETE FROM tokens WHERE created_at<?",
                        (now - older_than_days * 86400,))
        self.db.execute("DELETE FROM events WHERE created_at<?", (now - 30 * 86400,))
        self.db.commit()

    # ----------------------------------------------------------------- events
    def event_seen(self, event_id: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone() is not None

    def add_event(self, event_id: str, source: str, source_name: str, text: str,
                  url: str = "") -> None:
        self.db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)",
                        (event_id, source, source_name, text[:2000], url, time.time()))
        self.db.commit()

    # ----------------------------------------------------------------- alerts
    def add_alert(self, *, mint: str, name: str, symbol: str, narrative: str,
                  match_score: float, safety: int, momentum: int, verdict: str,
                  entry_mcap_sol: float) -> int:
        cur = self.db.execute(
            "INSERT INTO alerts(mint,name,symbol,narrative,match_score,safety,momentum,"
            "verdict,entry_mcap_sol,max_mcap_sol,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (mint, name, symbol, narrative, match_score, safety, momentum, verdict,
             entry_mcap_sol, entry_mcap_sol, time.time()))
        self.db.commit()
        return int(cur.lastrowid or 0)

    def close_alert(self, alert_id: int, *, max_mcap_sol: float, end_mcap_sol: float,
                    creator_sold: bool) -> None:
        self.db.execute(
            "UPDATE alerts SET max_mcap_sol=?, end_mcap_sol=?, creator_sold=?, closed_at=? "
            "WHERE id=?",
            (max_mcap_sol, end_mcap_sol, int(creator_sold), time.time(), alert_id))
        self.db.commit()

    def creator_sold_seen(self, mint: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM alerts WHERE mint=? AND creator_sold=1 LIMIT 1", (mint,)).fetchone()
        return row is not None

    def paper_stats(self, since: float = 0) -> dict[str, Any]:
        rows = self.db.execute(
            "SELECT * FROM alerts WHERE closed_at IS NOT NULL AND created_at>=? "
            "AND entry_mcap_sol>0", (since,)).fetchall()
        open_count = int(self.db.execute(
            "SELECT COUNT(*) FROM alerts WHERE closed_at IS NULL").fetchone()[0])
        if not rows:
            return {"closed": 0, "open": open_count}
        end_x = [r["end_mcap_sol"] / r["entry_mcap_sol"] for r in rows]
        max_x = [r["max_mcap_sol"] / r["entry_mcap_sol"] for r in rows]
        best = max(rows, key=lambda r: r["max_mcap_sol"] / r["entry_mcap_sol"])
        by_verdict: dict[str, list[float]] = {}
        for r, x in zip(rows, end_x):
            by_verdict.setdefault(r["verdict"], []).append(x)
        return {
            "closed": len(rows),
            "open": open_count,
            "winners_at_end": sum(1 for x in end_x if x > 1.0),
            "median_end_x": statistics.median(end_x),
            "mean_end_x": statistics.fmean(end_x),
            "median_max_x": statistics.median(max_x),
            "rugs": sum(1 for r in rows if r["creator_sold"]),
            "best": (best["symbol"], best["max_mcap_sol"] / best["entry_mcap_sol"]),
            "by_verdict": {k: statistics.median(v) for k, v in by_verdict.items()},
        }
