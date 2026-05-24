"""Read native-price candles from the local Stax SQLite cache.

This intentionally has no yfinance/network fallback. A ticker either resolves
to a native Stax cache symbol and has cached candles, or the caller gets an
explicit status describing why price progress cannot be computed.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from supplement_cache import read_candles as read_supplement_candles
    from supplement_cache import read_quote as read_cached_quote
except ImportError:  # pragma: no cover - package import fallback
    from .supplement_cache import read_candles as read_supplement_candles
    from .supplement_cache import read_quote as read_cached_quote


DISCOVER_DIR = Path(__file__).resolve().parent
DEFAULT_ALIASES_PATH = DISCOVER_DIR / "symbol_aliases.json"
DEFAULT_STAX_DB = Path.home() / "Library" / "Application Support" / "stax" / "stax.db"

RANGE_PREFERENCE = ("5Y", "3Y", "2Y", "1Y", "10Y", "ALL", "YTD", "6M", "3M", "1M", "1W", "1D")

SUFFIX_CURRENCIES = {
    ".PA": "EUR",
    ".ST": "SEK",
    ".T": "JPY",
    ".TW": "TWD",
    ".TWO": "TWD",
    ".KS": "KRW",
    ".HK": "HKD",
}


@dataclass(frozen=True)
class SymbolResolution:
    ticker: str
    symbol: str | None
    currency: str | None
    status: str
    name: str | None = None
    notes: str | None = None


def normalize_ticker(ticker: str | None) -> str:
    return (ticker or "").strip().lstrip("$").upper()


def compact_ticker(ticker: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", normalize_ticker(ticker))


def load_aliases(path: Path = DEFAULT_ALIASES_PATH) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text())
    out: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        norm = normalize_ticker(key)
        out[norm] = value
        out[compact_ticker(norm)] = value
    return out


def default_currency_for_symbol(symbol: str) -> str:
    sym = symbol.upper()
    for suffix, currency in sorted(SUFFIX_CURRENCIES.items(), key=lambda x: len(x[0]), reverse=True):
        if sym.endswith(suffix):
            return currency
    return "USD"


def resolve_symbol(ticker: str, aliases: dict[str, dict[str, Any]] | None = None) -> SymbolResolution:
    aliases = aliases if aliases is not None else load_aliases()
    norm = normalize_ticker(ticker)
    compact = compact_ticker(norm)
    alias = aliases.get(norm) or aliases.get(compact)
    if alias:
        symbol = alias.get("symbol")
        return SymbolResolution(
            ticker=norm,
            symbol=symbol,
            currency=alias.get("currency") or (default_currency_for_symbol(symbol) if symbol else None),
            status="ok" if symbol else "symbol_unresolved",
            name=alias.get("name"),
            notes=alias.get("notes"),
        )
    if re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,11}", norm):
        return SymbolResolution(
            ticker=norm,
            symbol=norm,
            currency=default_currency_for_symbol(norm),
            status="ok",
        )
    return SymbolResolution(ticker=norm, symbol=None, currency=None, status="symbol_unresolved")


def candle_date(epoch: int | float | str | None) -> str | None:
    if epoch is None:
        return None
    try:
        ts = int(float(epoch))
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


def _connect(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def cache_symbols(db_path: Path = DEFAULT_STAX_DB) -> set[str]:
    if not db_path.exists():
        return set()
    with _connect(db_path) as conn:
        cur = conn.execute("SELECT DISTINCT symbol FROM candles_cache")
        return {str(row[0]) for row in cur.fetchall()}


def _load_candles_from_db(
    symbol: str,
    db_path: Path,
    ranges: tuple[str, ...],
) -> tuple[list[dict[str, Any]], str | None]:
    if not db_path.exists():
        return [], None
    with _connect(db_path) as conn:
        for range_name in ranges:
            row = conn.execute(
                "SELECT payload FROM candles_cache WHERE symbol=? AND range=?",
                (symbol, range_name),
            ).fetchone()
            if not row:
                continue
            try:
                candles = json.loads(row[0])
            except json.JSONDecodeError:
                continue
            if isinstance(candles, list) and candles:
                parsed = [c for c in candles if isinstance(c, dict) and c.get("close") is not None]
                if parsed:
                    parsed.sort(key=lambda c: int(c.get("time") or 0))
                    return parsed, range_name
    return [], None


def load_candles(
    symbol: str,
    db_path: Path = DEFAULT_STAX_DB,
    ranges: tuple[str, ...] = RANGE_PREFERENCE,
    supplement_db: Path | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    candles, range_name = _load_candles_from_db(symbol, db_path, ranges)
    if candles:
        return candles, range_name

    if supplement_db:
        for supplement_range in ranges:
            supplement_candles = read_supplement_candles(symbol, supplement_range, supplement_db)
            if not supplement_candles:
                continue
            parsed = [
                c for c in supplement_candles
                if isinstance(c, dict) and c.get("close") is not None
            ]
            if parsed:
                parsed.sort(key=lambda c: int(c.get("time") or 0))
                return parsed, supplement_range
    return [], None


def read_quote(
    symbol: str,
    supplement_db: Path | None = None,
    db_path: Path = DEFAULT_STAX_DB,
) -> dict[str, Any] | None:
    quote = read_cached_quote(symbol, db_path)
    if quote:
        return quote
    if supplement_db:
        return read_cached_quote(symbol, supplement_db)
    return None


def price_snapshot(symbol: str, prediction_date: str,
                   db_path: Path = DEFAULT_STAX_DB,
                   supplement_db: Path | None = None) -> dict[str, Any]:
    candles, range_name = load_candles(symbol, db_path=db_path, supplement_db=supplement_db)
    if not candles:
        return {"price_status": "missing_cache"}

    dated: list[dict[str, Any]] = []
    for candle in candles:
        date = candle_date(candle.get("time"))
        close = candle.get("close")
        if date is None or close is None:
            continue
        try:
            close_f = float(close)
        except (TypeError, ValueError):
            continue
        dated.append({"date": date, "close": close_f, "time": int(candle.get("time") or 0)})
    if not dated:
        return {"price_status": "missing_cache"}

    dated.sort(key=lambda c: c["time"])
    entry = next((c for c in dated if c["date"] >= prediction_date), None)
    if entry is None:
        prior = [c for c in dated if c["date"] <= prediction_date]
        entry = prior[-1] if prior else None
    current = dated[-1]
    if entry is None:
        return {"price_status": "missing_cache"}

    entry_price = entry["close"]
    current_price = current["close"]
    return {
        "price_status": "ok",
        "price_range": range_name,
        "entry_date": entry["date"],
        "entry_price": entry_price,
        "current_date": current["date"],
        "current_price": current_price,
        "return_pct_current": ((current_price / entry_price) - 1.0) * 100.0 if entry_price else None,
    }


def target_currency_mismatch(target: dict[str, Any], price_currency: str | None) -> bool:
    target_currency = target.get("target_currency")
    if not target_currency or not price_currency:
        return False
    return str(target_currency).upper() != str(price_currency).upper()


def rounded_price_fields(snapshot: dict[str, Any]) -> dict[str, Any]:
    out = dict(snapshot)
    for key in ("entry_price", "current_price", "return_pct_current"):
        if isinstance(out.get(key), float):
            out[key] = round(out[key], 4)
    return out
