"""Local SQLite ledger: paper cash, positions, fills, decisions, equity, cache."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .data import Candle

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY CHECK (id = 1), cash REAL NOT NULL, started_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, venue TEXT NOT NULL, qty REAL NOT NULL,
    entry_ts REAL NOT NULL, entry_price REAL NOT NULL, entry_fee REAL NOT NULL, entry_reason TEXT NOT NULL,
    stop_price REAL NOT NULL, take_price REAL NOT NULL,
    exit_ts REAL, exit_price REAL, exit_fee REAL, exit_reason TEXT, exit_kind TEXT, pnl REAL
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, position_id INTEGER NOT NULL, symbol TEXT NOT NULL,
    side TEXT NOT NULL, qty REAL NOT NULL, ref_price REAL NOT NULL, price REAL NOT NULL, fee REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, symbol TEXT NOT NULL, price REAL, action TEXT NOT NULL,
    executed INTEGER NOT NULL, reason TEXT NOT NULL, blocked_by TEXT, features TEXT
);
CREATE TABLE IF NOT EXISTS equity (ts REAL PRIMARY KEY, cash REAL NOT NULL, positions_value REAL NOT NULL, equity REAL NOT NULL);
CREATE TABLE IF NOT EXISTS errors (id INTEGER PRIMARY KEY, ts REAL NOT NULL, symbol TEXT, message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL, ts INTEGER NOT NULL, open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, fetched_at REAL NOT NULL, value TEXT NOT NULL);
"""


@dataclass(frozen=True)
class Position:
    id: int
    symbol: str
    venue: str
    qty: float
    entry_ts: float
    entry_price: float
    entry_fee: float
    stop_price: float
    take_price: float


class Ledger:
    def __init__(self, path: Path, starting_balance: float, clock: Any = time.time) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._clock = clock
        if self.db.execute("SELECT 1 FROM account").fetchone() is None:
            self.db.execute("INSERT INTO account (id, cash, started_at) VALUES (1, ?, ?)",
                            (starting_balance, clock()))
            self.db.commit()

    def now(self) -> float:
        return self._clock()

    def close(self) -> None:
        self.db.close()

    # account / positions
    def cash(self) -> float:
        return self.db.execute("SELECT cash FROM account").fetchone()["cash"]

    def open_positions(self) -> list[Position]:
        rows = self.db.execute(
            "SELECT id, symbol, venue, qty, entry_ts, entry_price, entry_fee, stop_price, take_price "
            "FROM positions WHERE exit_ts IS NULL ORDER BY id").fetchall()
        return [Position(**dict(r)) for r in rows]

    def open_position(self, symbol: str) -> Position | None:
        return next((p for p in self.open_positions() if p.symbol == symbol), None)

    def record_entry(self, symbol: str, venue: str, qty: float, ref_price: float, price: float,
                     fee: float, stop: float, take: float, reason: str) -> int:
        ts = self._clock()
        cost = qty * price + fee
        if cost > self.cash() + 1e-9:
            raise ValueError("paper entry exceeds cash: no leverage")
        cur = self.db.execute(
            "INSERT INTO positions (symbol, venue, qty, entry_ts, entry_price, entry_fee, entry_reason, stop_price, take_price) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (symbol, venue, qty, ts, price, fee, reason, stop, take))
        pid = cur.lastrowid
        self.db.execute("INSERT INTO fills (ts, position_id, symbol, side, qty, ref_price, price, fee) VALUES (?, ?, ?, 'BUY', ?, ?, ?, ?)",
                        (ts, pid, symbol, qty, ref_price, price, fee))
        self.db.execute("UPDATE account SET cash = cash - ?", (cost,))
        self.db.commit()
        return pid

    def record_exit(self, pos: Position, ref_price: float, price: float, fee: float, reason: str, kind: str) -> float:
        ts = self._clock()
        proceeds = pos.qty * price - fee
        pnl = proceeds - (pos.qty * pos.entry_price + pos.entry_fee)
        self.db.execute("UPDATE positions SET exit_ts=?, exit_price=?, exit_fee=?, exit_reason=?, exit_kind=?, pnl=? WHERE id=?",
                        (ts, price, fee, reason, kind, pnl, pos.id))
        self.db.execute("INSERT INTO fills (ts, position_id, symbol, side, qty, ref_price, price, fee) VALUES (?, ?, ?, 'SELL', ?, ?, ?, ?)",
                        (ts, pos.id, pos.symbol, pos.qty, ref_price, price, fee))
        self.db.execute("UPDATE account SET cash = cash + ?", (proceeds,))
        self.db.commit()
        return pnl

    # journal
    def record_decision(self, symbol: str, price: float | None, action: str, executed: bool,
                        reason: str, blocked_by: str | None, features: dict[str, Any] | None) -> None:
        self.db.execute("INSERT INTO decisions (ts, symbol, price, action, executed, reason, blocked_by, features) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (self._clock(), symbol, price, action, int(executed), reason, blocked_by,
                         json.dumps(features) if features is not None else None))
        self.db.commit()

    def record_error(self, symbol: str | None, message: str) -> None:
        self.db.execute("INSERT INTO errors (ts, symbol, message) VALUES (?, ?, ?)", (self._clock(), symbol, message[:2000]))
        self.db.commit()

    def record_equity(self, marks: dict[str, float]) -> float:
        value = sum(p.qty * marks.get(p.symbol, p.entry_price) for p in self.open_positions())
        cash = self.cash()
        self.db.execute("INSERT OR REPLACE INTO equity (ts, cash, positions_value, equity) VALUES (?, ?, ?, ?)",
                        (self._clock(), cash, value, cash + value))
        self.db.commit()
        return cash + value

    def last_equity(self) -> float | None:
        row = self.db.execute("SELECT equity FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
        return row["equity"] if row else None

    # candle history for DEX-only assets (keyless DexPaprika only reaches back 24h)
    def upsert_candles(self, symbol: str, candles: list[Candle]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO candles VALUES (?, ?, ?, ?, ?, ?, ?)",
                            [(symbol, c.ts, c.open, c.high, c.low, c.close, c.volume) for c in candles])
        self.db.commit()

    def candles(self, symbol: str, since_ts: float) -> list[Candle]:
        rows = self.db.execute("SELECT ts, open, high, low, close, volume FROM candles WHERE symbol=? AND ts>=? ORDER BY ts",
                               (symbol, since_ts)).fetchall()
        return [Candle(*tuple(r)) for r in rows]

    # cache (data.Cache protocol)
    def cache_get(self, key: str, max_age_s: float) -> Any | None:
        row = self.db.execute("SELECT fetched_at, value FROM cache WHERE key=?", (key,)).fetchone()
        if row is None or self._clock() - row["fetched_at"] > max_age_s:
            return None
        return json.loads(row["value"])

    def cache_put(self, key: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)", (key, self._clock(), json.dumps(value)))
        self.db.commit()
