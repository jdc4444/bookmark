"""Live-refresh timeline predictions against the Stax headless cache.

The Stax headless server (`127.0.0.1:1421`) proxies Yahoo + Finnhub with
TTL'd caching. We hit `/yf/v8/finance/chart/<sym>?range=1d&interval=1d`
per unique resolved symbol, multiply the live `regularMarketPrice` by the
cached `shares_outstanding` from the supplement DB, and reclassify status
using the same rules as the offline pipeline.

The output is intentionally a slim per-prediction diff so the frontend
can patch existing Timeline cards in place. We deliberately do NOT
refresh the Companies appendix rollups or the chapter chips — only the
Timeline tab.

Returned shape:
{
  "report_id": "<id>",
  "refreshed_at": "<iso-utc>",
  "predictions": {
    "<prediction_id>": {
      "current_price": <float|null>,
      "current_market_cap": <float|null>,
      "return_pct_current": <float|null>,
      "progress_pct_current": <float|null>,
      "status": "<status>",
      "stale": <bool>,            # true if stax couldn't quote
    },
    ...
  },
  "status_counts": { "<status>": <int>, ... }
}
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from supplement_cache import supplement_db_path  # noqa: E402
from timeline_progress import (  # noqa: E402
    MARKET_CAP_BASES,
    QUALITATIVE_BASES,
    classify_status,
    market_cap_threshold,
    market_cap_progress_value,
    parse_target_end,
)
from target_parser import progress_pct  # noqa: E402

STAX_BASE = "http://127.0.0.1:1421"
CHART_PATH = "/yf/v8/finance/chart"
HTTP_TIMEOUT = 6


def _http_get_json(path: str, params: dict[str, str]) -> dict[str, Any] | None:
    qs = urllib.parse.urlencode(params)
    url = f"{STAX_BASE}{path}?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def fetch_chart_meta(symbol: str) -> dict[str, Any] | None:
    """Pull regularMarketPrice + currency + exchange via stax chart proxy."""
    body = _http_get_json(
        f"{CHART_PATH}/{urllib.parse.quote(symbol)}",
        {"range": "5d", "interval": "1d"},
    )
    if not body:
        return None
    result = (body.get("chart") or {}).get("result") or []
    if not result:
        return None
    return result[0].get("meta") or None


def _shares_outstanding_lookup(report_id: str) -> dict[str, float]:
    """Read shares_outstanding from the per-report supplement DB so live
    market caps don't depend on Yahoo's quoteSummary endpoint (which
    requires a crumb and gets blocked through the stax proxy)."""
    db_path = supplement_db_path(report_id)
    if not db_path.exists():
        return {}
    import sqlite3
    out: dict[str, float] = {}
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute(
                "SELECT symbol, shares_outstanding FROM quote_cache "
                "WHERE shares_outstanding IS NOT NULL"
            )
            for sym, shares in cur.fetchall():
                if sym and shares:
                    out[str(sym).upper()] = float(shares)
    except Exception:
        pass
    return out


def _entry_market_cap(row: dict[str, Any]) -> float | None:
    """Pre-existing entry_market_cap is authoritative; fall back to a
    fresh shares × entry_price calculation if the row never had one."""
    val = row.get("entry_market_cap")
    if isinstance(val, (int, float)):
        return float(val)
    shares = row.get("shares_outstanding")
    entry = row.get("entry_price")
    if isinstance(shares, (int, float)) and isinstance(entry, (int, float)):
        return float(shares) * float(entry)
    return None


def refresh_one(
    row: dict[str, Any],
    quotes: dict[str, dict[str, Any] | None],
    shares_lookup: dict[str, float],
    today: date,
) -> dict[str, Any]:
    """Recompute price, market cap, progress, and status for a single
    prediction row using the live quote we already fetched."""
    prediction_id = row.get("prediction_id")
    base = {
        "current_price": row.get("current_price"),
        "current_market_cap": row.get("current_market_cap"),
        "return_pct_current": row.get("return_pct_current"),
        "progress_pct_current": row.get("progress_pct_current"),
        "status": row.get("status"),
        "stale": True,
    }
    symbol = row.get("price_symbol")
    if not symbol:
        return {prediction_id: base}
    meta = quotes.get(symbol)
    if not meta:
        return {prediction_id: base}

    live_price = meta.get("regularMarketPrice")
    if not isinstance(live_price, (int, float)):
        return {prediction_id: base}

    target = row.get("target") or {}
    basis = target.get("basis")
    entry_price = row.get("entry_price")
    return_pct = None
    if isinstance(entry_price, (int, float)) and entry_price:
        return_pct = round(((float(live_price) - float(entry_price))
                            / float(entry_price)) * 100.0, 4)

    current_market_cap = row.get("current_market_cap")
    if basis in MARKET_CAP_BASES:
        shares = shares_lookup.get(symbol.upper()) or row.get("shares_outstanding")
        if isinstance(shares, (int, float)):
            current_market_cap = float(shares) * float(live_price)

    progress = None
    if basis in MARKET_CAP_BASES:
        threshold = market_cap_threshold(target)
        if isinstance(row.get("target_market_cap"), (int, float)):
            threshold = float(row["target_market_cap"])
        entry_mc = _entry_market_cap(row)
        progress = market_cap_progress_value(
            target, entry_mc, current_market_cap, threshold,
        )
    else:
        if isinstance(entry_price, (int, float)):
            progress = progress_pct(target, float(entry_price), float(live_price))

    fresh_row = {
        **row,
        "current_price": round(float(live_price), 4),
        "current_market_cap": (round(float(current_market_cap), 2)
                                if isinstance(current_market_cap, (int, float))
                                else current_market_cap),
        "return_pct_current": return_pct,
        "progress_pct_current": (round(float(progress), 2)
                                  if progress is not None else None),
        "price_status": "ok",
    }
    fresh_row["status"] = classify_status(fresh_row, today)

    return {
        prediction_id: {
            "current_price": fresh_row["current_price"],
            "current_market_cap": fresh_row["current_market_cap"],
            "return_pct_current": fresh_row["return_pct_current"],
            "progress_pct_current": fresh_row["progress_pct_current"],
            "status": fresh_row["status"],
            "stale": False,
        }
    }


def refresh_timeline_live(timeline: dict[str, Any]) -> dict[str, Any]:
    predictions = timeline.get("predictions") or []
    report_id = timeline.get("report_id") or ""
    today = datetime.now(timezone.utc).date()

    symbols = sorted({
        p["price_symbol"] for p in predictions
        if isinstance(p.get("price_symbol"), str) and p.get("price_symbol")
    })
    quotes: dict[str, dict[str, Any] | None] = {}
    if symbols:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for sym, meta in zip(symbols,
                                 pool.map(fetch_chart_meta, symbols)):
                quotes[sym] = meta

    shares_lookup = _shares_outstanding_lookup(report_id) if report_id else {}

    diff: dict[str, Any] = {}
    status_counts: dict[str, int] = {}
    for row in predictions:
        merged = refresh_one(row, quotes, shares_lookup, today)
        diff.update(merged)
        for v in merged.values():
            st = v.get("status") or "unknown"
            status_counts[st] = status_counts.get(st, 0) + 1

    return {
        "report_id": report_id,
        "refreshed_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "predictions": diff,
        "status_counts": dict(sorted(status_counts.items())),
    }
