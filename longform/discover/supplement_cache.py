"""Supplemental Stax-compatible cache for longform price enrichment.

The local Stax DB is still the primary source of truth. This module owns the
small per-report cache used when a report needs a frozen Yahoo snapshot for
symbols that are absent from Stax.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


DISCOVER_DIR = Path(__file__).resolve().parent
BOOKMARK_ROOT = DISCOVER_DIR.parents[1]
DATA_ROOT = BOOKMARK_ROOT / "data" / "longform"


def supplement_db_path(report_id: str) -> Path:
    return DATA_ROOT / report_id / "inputs" / "stax_supplement.sqlite3"


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(db_path)


def init_db(db_path: Path) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS candles_cache (
                symbol TEXT NOT NULL,
                range TEXT NOT NULL,
                fetched_at INTEGER NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (symbol, range)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quote_cache (
                symbol TEXT PRIMARY KEY,
                fetched_at INTEGER NOT NULL,
                market_cap REAL,
                shares_outstanding REAL,
                currency TEXT,
                exchange TEXT,
                payload TEXT
            )
            """
        )


def _read_only_connect(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def read_candles(symbol: str, range_name: str, db_path: Path) -> list[dict[str, Any]] | None:
    if not db_path.exists():
        return None
    try:
        with _read_only_connect(db_path) as conn:
            row = conn.execute(
                "SELECT payload FROM candles_cache WHERE symbol=? AND range=?",
                (symbol, range_name),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    try:
        payload = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    return [c for c in payload if isinstance(c, dict)]


def upsert_candles(
    symbol: str,
    range_name: str,
    candles: list[dict[str, Any]],
    fetched_at: int | None,
    db_path: Path,
) -> None:
    init_db(db_path)
    fetched = int(fetched_at if fetched_at is not None else time.time())
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO candles_cache(symbol, range, fetched_at, payload)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol, range) DO UPDATE SET
                fetched_at=excluded.fetched_at,
                payload=excluded.payload
            """,
            (symbol, range_name, fetched, json.dumps(candles, separators=(",", ":"))),
        )


def read_quote(symbol: str, db_path: Path) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    try:
        with _read_only_connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT symbol, fetched_at, market_cap, shares_outstanding,
                       currency, exchange, payload
                FROM quote_cache
                WHERE symbol=?
                """,
                (symbol,),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    payload: Any = None
    if row[6]:
        try:
            payload = json.loads(row[6])
        except json.JSONDecodeError:
            payload = row[6]
    return {
        "symbol": row[0],
        "fetched_at": row[1],
        "market_cap": row[2],
        "shares_outstanding": row[3],
        "currency": row[4],
        "exchange": row[5],
        "payload": payload,
    }


def upsert_quote(
    symbol: str,
    market_cap: float | None,
    shares: float | None,
    currency: str | None,
    exchange: str | None,
    payload: dict[str, Any] | None,
    db_path: Path,
    fetched_at: int | None = None,
) -> None:
    init_db(db_path)
    fetched = int(fetched_at if fetched_at is not None else time.time())
    payload_json = json.dumps(payload or {}, separators=(",", ":"))
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO quote_cache(
                symbol, fetched_at, market_cap, shares_outstanding,
                currency, exchange, payload
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                fetched_at=excluded.fetched_at,
                market_cap=excluded.market_cap,
                shares_outstanding=excluded.shares_outstanding,
                currency=excluded.currency,
                exchange=excluded.exchange,
                payload=excluded.payload
            """,
            (symbol, fetched, market_cap, shares, currency, exchange, payload_json),
        )


# ----- FX rates -----
#
# Stored as `pair=<FROM><TO>`, e.g. `USDSEK`. `rate` is units of TO per 1 unit
# of FROM (so USDSEK rate of 9.6 means 1 USD = 9.6 SEK). All conversions go
# through USD as a hub: to convert EUR→SEK we'd use rate(EURUSD) * rate(USDSEK).

def init_fx_table(db_path: Path) -> None:
    init_db(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fx_cache (
                pair TEXT PRIMARY KEY,
                fetched_at INTEGER NOT NULL,
                rate REAL NOT NULL
            )
            """
        )


def read_fx(pair: str, db_path: Path) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    try:
        with _read_only_connect(db_path) as conn:
            row = conn.execute(
                "SELECT pair, fetched_at, rate FROM fx_cache WHERE pair=?",
                (pair.upper(),),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    return {"pair": row[0], "fetched_at": row[1], "rate": row[2]}


def upsert_fx(pair: str, rate: float, db_path: Path,
              fetched_at: int | None = None) -> None:
    init_fx_table(db_path)
    fetched = int(fetched_at if fetched_at is not None else time.time())
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO fx_cache(pair, fetched_at, rate)
            VALUES (?, ?, ?)
            ON CONFLICT(pair) DO UPDATE SET
                fetched_at=excluded.fetched_at,
                rate=excluded.rate
            """,
            (pair.upper(), fetched, float(rate)),
        )


def fx_convert(amount: float, from_currency: str, to_currency: str,
               db_path: Path) -> tuple[float | None, str | None]:
    """Convert `amount` from one currency to another using cached FX rates.
    Returns (converted_amount, rate_string_for_audit) or (None, None) if no
    path is available. Routes through USD as a hub for cross pairs.
    """
    fc = (from_currency or "").upper()
    tc = (to_currency or "").upper()
    if not fc or not tc or amount is None:
        return None, None
    if fc == tc:
        return float(amount), f"{fc}=1.0"

    def _direct(a: str, b: str) -> float | None:
        if a == b:
            return 1.0
        rec = read_fx(f"{a}{b}", db_path)
        if rec and rec["rate"]:
            return float(rec["rate"])
        rec = read_fx(f"{b}{a}", db_path)
        if rec and rec["rate"]:
            return 1.0 / float(rec["rate"])
        return None

    direct = _direct(fc, tc)
    if direct is not None:
        return float(amount) * direct, f"{fc}{tc}={direct:.6f}"

    # Hub through USD
    if fc != "USD" and tc != "USD":
        leg1 = _direct(fc, "USD")
        leg2 = _direct("USD", tc)
        if leg1 is not None and leg2 is not None:
            rate = leg1 * leg2
            return float(amount) * rate, f"{fc}USD={leg1:.6f}*USD{tc}={leg2:.6f}"
    return None, None
