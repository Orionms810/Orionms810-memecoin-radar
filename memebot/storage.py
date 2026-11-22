"""Base SQLite locale : tokens vus, posts déjà traités, alertes et bilan
« paper trading » (ce que tu aurais gagné/perdu en suivant les alertes)."""

from __future__ import annotations

import json
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
CREATE TABLE IF NOT EXISTS trades(
    id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT, mint TEXT, symbol TEXT, name TEXT,
    opened_at REAL, closed_at REAL, entry_sol REAL, exit_sol REAL, pnl_sol REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS census(
    mint TEXT PRIMARY KEY, created_at REAL, symbol TEXT, name TEXT, creator TEXT,
    dev_buy_pct REAL, dev_sol REAL, mcap0 REAL, narrative TEXT, creator_24h INTEGER,
    creator_prev INTEGER, points INTEGER, path TEXT, mcap_5m REAL, mcap_15m REAL,
    mcap_30m REAL, max_mcap REAL, graduated INTEGER);
CREATE INDEX IF NOT EXISTS idx_census_created ON census(created_at);
CREATE INDEX IF NOT EXISTS idx_census_creator ON census(creator);
CREATE TABLE IF NOT EXISTS candidates(
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL, mint TEXT, symbol TEXT, name TEXT,
    narrative TEXT, source TEXT, match_score REAL, safety INTEGER, momentum INTEGER,
    dev_buy_pct REAL, top10_pct REAL, early_buyers INTEGER, buyers INTEGER, buy_sol REAL,
    sell_sol REAL, age_s REAL, decision TEXT, detail TEXT, mcap_eval REAL, mcap_5 REAL,
    mcap_15 REAL, mcap_30 REAL, max_mcap_30 REAL, creator_sold_30 INTEGER DEFAULT 0,
    traded INTEGER DEFAULT 0, trade_detail TEXT);
"""


class Storage:
    def __init__(self, path: str | Path = "memebot.db"):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        # WAL + NORMAL : pas de synchronisation disque à chaque écriture (le disque du
        # serveur gratuit est très lent) ; la base reste cohérente en cas de coupure.
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self) -> None:
        """Ajoute les colonnes apparues dans les nouvelles versions (bases existantes)."""
        for table, added in (("candidates", self.ADDED_CANDIDATE_COLUMNS),
                             ("trades", self.ADDED_TRADE_COLUMNS)):
            existing = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            for col, kind in added:
                if col not in existing:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")

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

    def creator_tokens(self, creator: str, exclude_mint: str, limit: int = 10) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM tokens WHERE creator=? AND mint<>? ORDER BY created_at DESC LIMIT ?",
            (creator, exclude_mint, limit)).fetchall()

    def candidate_outcomes(self, mints: list[str]) -> list[sqlite3.Row]:
        if not mints:
            return []
        marks = ",".join("?" * len(mints))
        return self.db.execute(
            f"SELECT mint, creator_sold_30, mcap_eval, mcap_30, max_mcap_30 FROM candidates "
            f"WHERE mint IN ({marks})", mints).fetchall()

    CENSUS_COLUMNS = ("mint", "created_at", "symbol", "name", "creator", "dev_buy_pct", "dev_sol",
                      "mcap0", "narrative", "creator_24h", "creator_prev", "points", "path",
                      "mcap_5m", "mcap_15m", "mcap_30m", "max_mcap", "graduated")

    def census_add(self, row: dict[str, Any]) -> None:
        cols = [c for c in self.CENSUS_COLUMNS if c in row]
        self.db.execute(
            f"INSERT OR REPLACE INTO census({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [row[c] for c in cols])
        self.db.commit()

    def census_rows(self, since: float = 0) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM census WHERE created_at>=? ORDER BY created_at",
                               (since,)).fetchall()

    def creator_graduations(self, creator: str, exclude_mint: str = "") -> int:
        """Nombre de tokens de ce créateur qui ont gradué (d'après le recensement)."""
        row = self.db.execute("SELECT COUNT(*) FROM census WHERE creator=? AND graduated=1 AND mint!=?",
                              (creator, exclude_mint)).fetchone()
        return int(row[0])

    def census_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM census").fetchone()[0])

    def prune_census(self, keep_days: float) -> None:
        self.db.execute("DELETE FROM census WHERE created_at<?", (time.time() - keep_days * 86400,))
        self.db.commit()

    def prune(self, older_than_days: float = 14) -> None:
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

    # ------------------------------------------------------- état persistant
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, json.dumps(value)))
        self.db.commit()

    # ---------------------------------------------------------------- trades
    # Ajoutées pour le suivi des sorties : multiples par rapport au prix d'achat
    ADDED_TRADE_COLUMNS = (("exit_x", "REAL"), ("peak_x", "REAL"), ("after_max_x", "REAL"),
                           ("after_end_x", "REAL"), ("graduated", "INTEGER"), ("source", "TEXT"))

    def add_trade(self, *, mode: str, mint: str, symbol: str, name: str, opened_at: float,
                  entry_sol: float, exit_sol: float, pnl_sol: float, reason: str,
                  **extra: Any) -> int:
        values = {"mode": mode, "mint": mint, "symbol": symbol, "name": name,
                  "opened_at": opened_at, "closed_at": time.time(), "entry_sol": entry_sol,
                  "exit_sol": exit_sol, "pnl_sol": pnl_sol, "reason": reason}
        values.update({k: v for k, v in extra.items() if k in dict(self.ADDED_TRADE_COLUMNS)})
        cur = self.db.execute(
            f"INSERT INTO trades({','.join(values)}) VALUES ({','.join('?' * len(values))})",
            list(values.values()))
        self.db.commit()
        return int(cur.lastrowid or 0)

    def update_trade(self, trade_id: int, values: dict[str, Any]) -> None:
        cols = [c for c in values if c in dict(self.ADDED_TRADE_COLUMNS)]
        if not cols:
            return
        self.db.execute(f"UPDATE trades SET {', '.join(c + '=?' for c in cols)} WHERE id=?",
                        [values[c] for c in cols] + [trade_id])
        self.db.commit()

    def trade_rows(self, mode: str | None = None) -> list[sqlite3.Row]:
        if mode is None:
            return self.db.execute("SELECT * FROM trades ORDER BY id").fetchall()
        return self.db.execute("SELECT * FROM trades WHERE mode=? ORDER BY id", (mode,)).fetchall()

    def recent_trades(self, mode: str, limit: int = 10) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM trades WHERE mode=? ORDER BY id DESC LIMIT ?",
                               (mode, limit)).fetchall()

    def trade_stats(self, mode: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl_sol),0), COALESCE(SUM(pnl_sol>=0),0) "
            "FROM trades WHERE mode=?", (mode,)).fetchone()
        return {"count": int(row[0]), "pnl": float(row[1]), "wins": int(row[2])}

    # ------------------------------------------------- journal des candidats
    CANDIDATE_COLUMNS = (
        "created_at", "mint", "symbol", "name", "narrative", "source", "match_score", "safety",
        "momentum", "dev_buy_pct", "top10_pct", "early_buyers", "buyers", "buy_sol", "sell_sol",
        "age_s", "decision", "detail", "mcap_eval", "mcap_5", "mcap_15", "mcap_30", "max_mcap_30",
        "creator_sold_30", "traded", "trade_detail",
        "funding_checked", "funding_shared", "funding_creator", "funding_fresh_pct",
        "creator", "creator_prev", "creator_exited_prev", "creator_dead_prev", "creator_graduated_prev",
        "flow_trades", "flow_avg_buy", "flow_small_pct", "flow_max_sell", "flow_accel",
        "flow_early_sellers", "flow_roundtrip")
    ADDED_CANDIDATE_COLUMNS = (("funding_checked", "INTEGER"), ("funding_shared", "INTEGER"),
                               ("funding_creator", "INTEGER"), ("funding_fresh_pct", "REAL"),
                               ("creator", "TEXT"), ("creator_prev", "INTEGER"),
                               ("creator_exited_prev", "INTEGER"), ("creator_dead_prev", "INTEGER"),
                               ("creator_graduated_prev", "INTEGER"), ("flow_trades", "INTEGER"),
                               ("flow_avg_buy", "REAL"), ("flow_small_pct", "REAL"),
                               ("flow_max_sell", "REAL"), ("flow_accel", "REAL"),
                               ("flow_early_sellers", "INTEGER"), ("flow_roundtrip", "INTEGER"))

    def add_candidate(self, row: dict[str, Any]) -> int:
        cols = [c for c in row if c in self.CANDIDATE_COLUMNS]
        cur = self.db.execute(
            f"INSERT INTO candidates({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [row[c] for c in cols])
        self.db.commit()
        return int(cur.lastrowid or 0)

    def update_candidate(self, row_id: int, values: dict[str, Any]) -> None:
        cols = [c for c in values if c in self.CANDIDATE_COLUMNS]
        if not cols:
            return
        self.db.execute(f"UPDATE candidates SET {', '.join(c + '=?' for c in cols)} WHERE id=?",
                        [values[c] for c in cols] + [row_id])
        self.db.commit()

    def candidate_rows(self, since: float = 0) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM candidates WHERE created_at>=? ORDER BY id",
                               (since,)).fetchall()
