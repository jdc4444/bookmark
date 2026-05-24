"""Track curated option predictions for longform reports.

This module is intentionally not part of the automatic longform build.
Option contracts are hand-selected in a spec file, then tracked explicitly:

    python3 longform/discover/option_tracker.py --report-id aleabit \
      --spec longform/discover/option_specs/aleabit-ewy-2028.json
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import math
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DISCOVER_DIR = Path(__file__).resolve().parent
BOOKMARK_ROOT = DISCOVER_DIR.parents[1]
DATA_ROOT = BOOKMARK_ROOT / "data" / "longform"
DEFAULT_STAX_DB = Path.home() / "Library" / "Application Support" / "stax" / "stax.db"

THETA_BASE = "http://127.0.0.1:25503"
STAX_BASE = "http://127.0.0.1:1421"
RISK_FREE_RATE = 0.045
DIVIDEND_YIELD = 0.0


@dataclass(frozen=True)
class OptionSpec:
    root: str
    expiry: str
    strike: float
    right: str
    entry_date: str
    prediction_id: str
    label: str


@dataclass(frozen=True)
class OptionTrackResult:
    path: Path
    payload: dict[str, Any]
    theta_no_data: list[str]


def _report_inputs(report_id: str) -> Path:
    return DATA_ROOT / report_id / "inputs"


def _report_prep(report_id: str) -> Path:
    return DATA_ROOT / report_id / "prep"


def theta_cache_path(report_id: str) -> Path:
    return _report_inputs(report_id) / "theta_cache.sqlite3"


def _connect_cache(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS http_cache (
            source TEXT NOT NULL,
            path TEXT NOT NULL,
            params_json TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            body TEXT NOT NULL,
            PRIMARY KEY (source, path, params_json)
        )
        """
    )
    return conn


def _cache_key(params: dict[str, Any]) -> str:
    clean = {k: str(v) for k, v in sorted(params.items()) if v is not None}
    return json.dumps(clean, sort_keys=True, separators=(",", ":"))


def _read_http_cache(
    db_path: Path,
    source: str,
    path: str,
    params: dict[str, Any],
) -> str | None:
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                """
                SELECT body FROM http_cache
                WHERE source=? AND path=? AND params_json=?
                """,
                (source, path, _cache_key(params)),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def _write_http_cache(
    db_path: Path,
    source: str,
    path: str,
    params: dict[str, Any],
    status_code: int,
    body: str,
) -> None:
    with _connect_cache(db_path) as conn:
        conn.execute(
            """
            INSERT INTO http_cache(source, path, params_json, fetched_at, status_code, body)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, path, params_json) DO UPDATE SET
                fetched_at=excluded.fetched_at,
                status_code=excluded.status_code,
                body=excluded.body
            """,
            (
                source,
                path,
                _cache_key(params),
                dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                int(status_code),
                body,
            ),
        )


def _http_get(
    base: str,
    path: str,
    params: dict[str, Any],
    cache_path: Path,
    source: str,
    retries: int = 4,
    timeout: int = 20,
    use_cache: bool = True,
) -> tuple[str, bool]:
    if use_cache:
        cached = _read_http_cache(cache_path, source, path, params)
        if cached is not None:
            return cached, True

    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    last_exc: BaseException | None = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                body = r.read().decode("utf-8", errors="replace")
                _write_http_cache(cache_path, source, path, params, r.status, body)
                return body, False
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            if exc.code == 472:
                _write_http_cache(cache_path, source, path, params, exc.code, body)
                return body, False
            if 500 <= exc.code < 600 and i < retries - 1:
                time.sleep(min(2 ** i, 8))
                continue
            raise RuntimeError(f"GET {url} failed with HTTP {exc.code}: {body[:200]}") from exc
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as exc:
            last_exc = exc
            if i < retries - 1:
                time.sleep(min(2 ** i, 8))
                continue
    raise RuntimeError(f"GET {url} failed after {retries} retries: {last_exc}")


def _parse_table(body: str) -> list[dict[str, Any]]:
    text = (body or "").strip()
    if not text or "No data" in text or "BAD REQUEST" in text or text.startswith("<html"):
        return []
    if text[0] in "[{":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("response", "data", "rows", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                if value and isinstance(value[0], dict):
                    return [r for r in value if isinstance(r, dict)]
                header = payload.get("header") or payload.get("columns")
                if header and all(isinstance(r, list) for r in value):
                    return [dict(zip(header, row)) for row in value]
        return [payload]

    try:
        reader = csv.DictReader(io.StringIO(text))
        return [dict(row) for row in reader if row]
    except csv.Error:
        return []


def _float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if math.isfinite(float(value)):
            return float(value)
        return None
    s = str(value).strip().strip('"')
    if not s or s.lower() in {"null", "nan", "none"}:
        return None
    try:
        out = float(s)
    except ValueError:
        return None
    return out if math.isfinite(out) else None


def _date_from_any(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip().strip('"')
    if not s:
        return None
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 8:
        try:
            return dt.date(int(digits[:4]), int(digits[4:6]), int(digits[6:8])).isoformat()
        except ValueError:
            return None
    return None


def _yyyymmdd(date_s: str) -> str:
    return dt.date.fromisoformat(date_s).strftime("%Y%m%d")


def _iso_from_yyyymmdd(date_s: str) -> str:
    return dt.date(int(date_s[:4]), int(date_s[4:6]), int(date_s[6:8])).isoformat()


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes_price(
    spot: float,
    strike: float,
    years: float,
    rate: float,
    vol: float,
    right: str,
    dividend_yield: float = 0.0,
) -> float:
    """European Black-Scholes price with continuous dividend yield."""
    right = right.upper()
    if years <= 0 or vol <= 0:
        intrinsic = max(0.0, spot - strike) if right == "C" else max(0.0, strike - spot)
        return intrinsic
    sqrt_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate - dividend_yield + 0.5 * vol * vol) * years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    disc_q = math.exp(-dividend_yield * years)
    disc_r = math.exp(-rate * years)
    if right == "C":
        return spot * disc_q * _norm_cdf(d1) - strike * disc_r * _norm_cdf(d2)
    return strike * disc_r * _norm_cdf(-d2) - spot * disc_q * _norm_cdf(-d1)


def implied_volatility(
    price: float,
    spot: float,
    strike: float,
    years: float,
    rate: float,
    right: str,
    dividend_yield: float = 0.0,
    low: float = 1e-6,
    high: float = 5.0,
) -> float | None:
    """Invert Black-Scholes by bisection. Returns decimal IV, or None."""
    if price <= 0 or spot <= 0 or strike <= 0 or years <= 0:
        return None
    intrinsic = max(0.0, spot - strike) if right.upper() == "C" else max(0.0, strike - spot)
    if price < intrinsic - 1e-6:
        return None
    lo_price = black_scholes_price(spot, strike, years, rate, low, right, dividend_yield)
    hi_price = black_scholes_price(spot, strike, years, rate, high, right, dividend_yield)
    if price < lo_price - 1e-6 or price > hi_price + 1e-6:
        return None
    lo = low
    hi = high
    for _ in range(100):
        mid = (lo + hi) / 2.0
        mid_price = black_scholes_price(spot, strike, years, rate, mid, right, dividend_yield)
        if abs(mid_price - price) < 1e-6:
            return mid
        if mid_price < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def realized_vol(candles: list[dict[str, Any]], window: int) -> list[dict[str, float | str]]:
    """Rolling annualized realized volatility from daily close log returns."""
    dated: list[tuple[str, float]] = []
    for c in candles:
        date_s = c.get("date")
        close = _float(c.get("close"))
        if not date_s or close is None or close <= 0:
            continue
        dated.append((str(date_s), close))
    dated.sort(key=lambda x: x[0])

    returns: list[tuple[str, float]] = []
    for i in range(1, len(dated)):
        prev = dated[i - 1][1]
        cur = dated[i][1]
        if prev > 0 and cur > 0:
            returns.append((dated[i][0], math.log(cur / prev)))

    out: list[dict[str, float | str]] = []
    for i in range(window - 1, len(returns)):
        sample = [r for _, r in returns[i - window + 1:i + 1]]
        if len(sample) < window:
            continue
        mean = sum(sample) / len(sample)
        var = sum((r - mean) ** 2 for r in sample) / (len(sample) - 1) if len(sample) > 1 else 0.0
        out.append({"date": returns[i][0], "rv": math.sqrt(var) * math.sqrt(252.0)})
    return out


def has_doubled(entry_close: float | None, current_close: float | None) -> bool:
    if entry_close is None or current_close is None or entry_close <= 0:
        return False
    return current_close >= 2.0 * entry_close


def _occ_symbol(root: str, expiry_iso: str, right: str, strike: float) -> str:
    exp = dt.date.fromisoformat(expiry_iso).strftime("%y%m%d")
    strike_int = int(round(float(strike) * 1000))
    return f"{root.upper().ljust(6)}{exp}{right.upper()}{strike_int:08d}"


def _fetch_theta_table(
    report_id: str,
    path: str,
    params: dict[str, Any],
    no_data: list[str],
    use_cache: bool = True,
) -> list[dict[str, Any]]:
    cache = theta_cache_path(report_id)
    body, from_cache = _http_get(THETA_BASE, path, params, cache, "theta", use_cache=use_cache)
    rows = _parse_table(body)
    if not rows:
        suffix = " (cached)" if from_cache else ""
        no_data.append(f"{path}?{urllib.parse.urlencode(params)}{suffix}")
    return rows


def _theta_expirations(report_id: str, root: str, no_data: list[str]) -> list[str]:
    rows = _fetch_theta_table(report_id, "/v3/option/list/expirations", {"symbol": root}, no_data)
    out: set[str] = set()
    for row in rows:
        for key in ("expiration", "exp", "date"):
            val = row.get(key)
            if val:
                s = str(val).strip().strip('"')
                # v3 returns YYYY-MM-DD; normalize to YYYYMMDD.
                s = s.replace("-", "")
                if len(s) == 8 and s.isdigit():
                    out.add(s)
    return sorted(out)


def _theta_strikes(report_id: str, root: str, exp: str, right: str, no_data: list[str]) -> list[int]:
    rows = _fetch_theta_table(
        report_id,
        "/v3/option/list/strikes",
        {"symbol": root, "expiration": exp, "right": right.upper()},
        no_data,
    )
    strikes: set[int] = set()
    for row in rows:
        for key in ("strike", "strike_price"):
            value = _float(row.get(key))
            if value is None:
                continue
            # v3 returns dollar strikes (e.g., 160.0). Store as integer
            # thousandths so OCC formatting downstream works unchanged.
            strikes.add(int(round(value * 1000)))
    return sorted(strikes)


def _field(row: dict[str, Any], names: tuple[str, ...]) -> Any:
    lower = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        if name in row:
            return row[name]
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def _parse_option_candles(
    rows: list[dict[str, Any]],
    spec: OptionSpec,
    spot_by_date: dict[str, float],
) -> list[dict[str, Any]]:
    expiry = dt.date.fromisoformat(spec.expiry)
    out: list[dict[str, Any]] = []
    for row in rows:
        # v3 history/eod returns "created" (UTC server timestamp) and
        # "last_trade" (last fill at EOD). Prefer last_trade since it's
        # session-aligned, then fall back to created/legacy names.
        date_s = _date_from_any(_field(row, ("date", "last_trade", "created", "trade_date", "quote_date")))
        if not date_s:
            continue
        open_v = _float(_field(row, ("open", "open_price")))
        high_v = _float(_field(row, ("high", "high_price")))
        low_v = _float(_field(row, ("low", "low_price")))
        close_v = _float(_field(row, ("close", "close_price", "mark", "mid")))
        bid_v = _float(_field(row, ("bid", "bid_price")))
        ask_v = _float(_field(row, ("ask", "ask_price")))
        # ThetaData EOD returns 0 OHLC for days with no trades on the
        # contract — common for OTM LEAPs. Fall back to the bid/ask mid
        # so the price series reflects the actual quote, not a missing
        # trade. We also fall back when the trade close is far below the
        # bid (a stale print on a quiet contract).
        mid_v = ((bid_v + ask_v) / 2.0) if (bid_v is not None and ask_v is not None
                                            and bid_v >= 0 and ask_v > bid_v) else None
        if (close_v is None or close_v <= 0
                or (mid_v is not None and bid_v is not None and close_v < bid_v * 0.5)):
            if mid_v is not None and mid_v > 0:
                close_v = mid_v
        if (open_v is None or open_v <= 0) and mid_v is not None:
            open_v = mid_v
        if (high_v is None or high_v <= 0) and mid_v is not None:
            high_v = mid_v
        if (low_v is None or low_v <= 0) and mid_v is not None:
            low_v = mid_v
        iv_v = _float(_field(row, ("iv", "implied_vol", "implied_volatility")))
        if iv_v is not None and iv_v > 3.0:
            iv_v = iv_v / 100.0

        spot = spot_by_date.get(date_s)
        if iv_v is None and spot:
            mid = ((bid_v + ask_v) / 2.0) if bid_v is not None and ask_v is not None and bid_v >= 0 and ask_v > 0 else None
            iv_price = mid if mid and mid > 0 else close_v
            days = (expiry - dt.date.fromisoformat(date_s)).days
            if iv_price and days > 0:
                iv_v = implied_volatility(
                    iv_price,
                    spot,
                    float(spec.strike),
                    days / 365.0,
                    RISK_FREE_RATE,
                    spec.right,
                    DIVIDEND_YIELD,
                )

        out.append({
            "date": date_s,
            "open": open_v,
            "high": high_v,
            "low": low_v,
            "close": close_v,
            "bid": bid_v,
            "ask": ask_v,
            "iv": round(iv_v, 6) if iv_v is not None else None,
        })
    out.sort(key=lambda c: c["date"])
    return out


def _fetch_option_eod(
    report_id: str,
    spec: OptionSpec,
    no_data: list[str],
) -> list[dict[str, Any]]:
    """ThetaData v3 EOD: /v3/option/history/eod with dollar strikes,
    `symbol`/`expiration` param names, and `interval=1d`."""
    exp = _yyyymmdd(spec.expiry)
    strike = float(spec.strike)
    start = _yyyymmdd(spec.entry_date)
    end = dt.date.today().strftime("%Y%m%d")
    return _fetch_theta_table(
        report_id,
        "/v3/option/history/eod",
        {
            "symbol": spec.root.upper(),
            "expiration": exp,
            "strike": strike,
            "right": spec.right.upper(),
            "start_date": start,
            "end_date": end,
            "interval": "1d",
        },
        no_data,
    )


def _business_days(start: str, end: str) -> list[str]:
    cur = dt.date.fromisoformat(start)
    stop = dt.date.fromisoformat(end)
    out: list[str] = []
    while cur <= stop:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += dt.timedelta(days=1)
    return out


def _fetch_option_at_time_fallback(
    report_id: str,
    spec: OptionSpec,
    no_data: list[str],
    time_of_day: str = "15:30:00",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    exp = _yyyymmdd(spec.expiry)
    # v3 takes dollar strikes; keep the int-thousandths form for the
    # local row filter so OCC formatting downstream is unchanged.
    strike_dollars = float(spec.strike)
    strike = int(round(strike_dollars * 1000))
    for day in _business_days(spec.entry_date, dt.date.today().isoformat()):
        day_rows = _fetch_theta_table(
            report_id,
            "/v3/option/at_time/quote",
            {
                "symbol": spec.root.upper(),
                "expiration": exp,
                "strike": strike_dollars,
                "right": spec.right.upper(),
                "start_date": _yyyymmdd(day),
                "end_date": _yyyymmdd(day),
                "time_of_day": time_of_day,
            },
            no_data,
        )
        if not day_rows:
            # Reference pattern from fin_v1: fetch the full expiration chain at
            # the time-of-day, then filter locally. This is slower but has
            # worked across ThetaTerminal versions when contract-level params
            # differ by endpoint.
            day_rows = _fetch_theta_table(
                report_id,
                "/v3/option/at_time/quote",
                {
                    "symbol": spec.root.upper(),
                    "expiration": exp,
                    "start_date": _yyyymmdd(day),
                    "end_date": _yyyymmdd(day),
                    "time_of_day": time_of_day,
                },
                no_data,
            )
            day_rows = _filter_contract_rows(day_rows, strike, spec.right)
        if day_rows:
            for row in day_rows:
                row.setdefault("date", _yyyymmdd(day))
                bid = _float(_field(row, ("bid", "bid_price")))
                ask = _float(_field(row, ("ask", "ask_price")))
                if bid is not None and ask is not None:
                    row.setdefault("close", (bid + ask) / 2.0)
                rows.append(row)
        # Fallback is one request per day; keep it under ThetaTerminal's
        # chain-rate guidance unless the response was already cached.
        time.sleep(0.9)
    return rows


def _filter_contract_rows(rows: list[dict[str, Any]], strike_int: int, right: str) -> list[dict[str, Any]]:
    right = right.upper()
    full_right = "CALL" if right == "C" else "PUT"
    out: list[dict[str, Any]] = []
    for row in rows:
        row_right = str(_field(row, ("right", "put_call")) or "").strip().upper()
        row_strike = _float(_field(row, ("strike", "strike_price")))
        if row_strike is None:
            continue
        row_strike_int = int(round(row_strike if row_strike > 1000 else row_strike * 1000))
        if row_strike_int == strike_int and row_right in {right, full_right}:
            out.append(row)
    return out


def _parse_stax_chart(body: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return []
    result = ((payload.get("chart") or {}).get("result") or [])
    if not result:
        return []
    item = result[0]
    timestamps = item.get("timestamp") or []
    quote = ((item.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    out: list[dict[str, Any]] = []
    for ts, close in zip(timestamps, closes):
        close_f = _float(close)
        if close_f is None:
            continue
        try:
            date_s = dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc).date().isoformat()
        except (TypeError, ValueError, OSError):
            continue
        out.append({"date": date_s, "close": close_f})
    out.sort(key=lambda c: c["date"])
    return out


def _load_spot_from_stax_db(symbol: str) -> list[dict[str, Any]]:
    if not DEFAULT_STAX_DB.exists():
        return []
    try:
        with sqlite3.connect(f"file:{DEFAULT_STAX_DB}?mode=ro", uri=True) as conn:
            row = conn.execute(
                """
                SELECT payload FROM candles_cache
                WHERE symbol=?
                ORDER BY CASE range
                  WHEN '5Y' THEN 0 WHEN '3Y' THEN 1 WHEN '10Y' THEN 2
                  WHEN '1Y' THEN 3 ELSE 9 END
                LIMIT 1
                """,
                (symbol.upper(),),
            ).fetchone()
    except sqlite3.OperationalError:
        return []
    if not row:
        return []
    try:
        payload = json.loads(row[0])
    except json.JSONDecodeError:
        return []
    out: list[dict[str, Any]] = []
    for candle in payload if isinstance(payload, list) else []:
        if not isinstance(candle, dict):
            continue
        close = _float(candle.get("close"))
        ts = candle.get("time")
        if close is None or ts is None:
            continue
        try:
            date_s = dt.datetime.fromtimestamp(int(float(ts)), tz=dt.timezone.utc).date().isoformat()
        except (TypeError, ValueError, OSError):
            continue
        out.append({"date": date_s, "close": close})
    out.sort(key=lambda c: c["date"])
    return out


def _fetch_spot_candles(report_id: str, symbol: str, entry_date: str) -> tuple[list[dict[str, Any]], str]:
    cache = theta_cache_path(report_id)
    params = {"range": "1y", "interval": "1d"}
    path = f"/yf/v8/finance/chart/{urllib.parse.quote(symbol.upper())}"
    try:
        body, _ = _http_get(STAX_BASE, path, params, cache, "stax", retries=2, timeout=8)
        candles = _parse_stax_chart(body)
        if candles:
            return _trim_spot_window(candles, entry_date), "stax_headless"
    except RuntimeError:
        pass
    candles = _load_spot_from_stax_db(symbol)
    if candles:
        return _trim_spot_window(candles, entry_date), "stax_sqlite_fallback"
    raise RuntimeError(f"No spot candles for {symbol}; Stax headless and local Stax DB were unavailable")


def _trim_spot_window(candles: list[dict[str, Any]], entry_date: str) -> list[dict[str, Any]]:
    start = dt.date.fromisoformat(entry_date) - dt.timedelta(days=150)
    stop = dt.date.today()
    return [c for c in candles if start.isoformat() <= str(c.get("date")) <= stop.isoformat()]


def _spot_lookup(candles: list[dict[str, Any]]) -> dict[str, float]:
    return {str(c["date"]): float(c["close"]) for c in candles if c.get("date") and _float(c.get("close")) is not None}


def _latest_on_or_before(rows: list[dict[str, Any]], date_s: str, value_key: str) -> float | None:
    candidates = [r for r in rows if str(r.get("date") or "") <= date_s and _float(r.get(value_key)) is not None]
    if not candidates:
        return None
    candidates.sort(key=lambda r: str(r.get("date") or ""))
    return _float(candidates[-1].get(value_key))


def _first_option_entry(candles: list[dict[str, Any]], entry_date: str) -> dict[str, Any] | None:
    for candle in candles:
        if str(candle.get("date") or "") >= entry_date and _float(candle.get("close")) is not None:
            return candle
    for candle in candles:
        if _float(candle.get("close")) is not None:
            return candle
    return None


def _last_option_current(candles: list[dict[str, Any]]) -> dict[str, Any] | None:
    for candle in reversed(candles):
        if _float(candle.get("close")) is not None:
            return candle
    return None


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _build_thesis(
    spec: OptionSpec,
    option_candles: list[dict[str, Any]],
    rv30: list[dict[str, Any]],
    rv90: list[dict[str, Any]],
) -> dict[str, Any]:
    entry = _first_option_entry(option_candles, spec.entry_date)
    current = _last_option_current(option_candles)
    entry_date = str(entry.get("date")) if entry else spec.entry_date
    current_date = str(current.get("date")) if current else dt.date.today().isoformat()
    entry_close = _float(entry.get("close")) if entry else None
    current_close = _float(current.get("close")) if current else None
    doubled = has_doubled(entry_close, current_close)

    entry_iv = _latest_on_or_before(option_candles, entry_date, "iv")
    today_iv = _latest_on_or_before(option_candles, current_date, "iv")
    entry_rv30 = _latest_on_or_before(rv30, entry_date, "rv")
    entry_rv90 = _latest_on_or_before(rv90, entry_date, "rv")
    today_rv30 = _latest_on_or_before(rv30, current_date, "rv")
    today_rv90 = _latest_on_or_before(rv90, current_date, "rv")
    summary = (
        f"{spec.label}: doubled? {str(doubled).lower()}. "
        f"IV/RV30 at entry {_pct(entry_iv)} vs {_pct(entry_rv30)}; "
        f"today {_pct(today_iv)} vs {_pct(today_rv30)}."
    )
    return {
        "summary": summary,
        "doubled": doubled,
        "iv_vs_rv_at_entry": {"iv": entry_iv, "rv_30d": entry_rv30, "rv_90d": entry_rv90},
        "iv_vs_rv_today": {"iv": today_iv, "rv_30d": today_rv30, "rv_90d": today_rv90},
    }


def track_option(spec: OptionSpec, report_id: str) -> OptionTrackResult:
    """Fetch and write one option-tracking JSON for a curated prediction."""
    spec = OptionSpec(
        root=spec.root.upper(),
        expiry=spec.expiry,
        strike=float(spec.strike),
        right=spec.right.upper(),
        entry_date=spec.entry_date,
        prediction_id=spec.prediction_id,
        label=spec.label,
    )
    no_data: list[str] = []
    exp = _yyyymmdd(spec.expiry)

    expirations = _theta_expirations(report_id, spec.root, no_data)
    if expirations and exp not in expirations:
        jan_2028 = [e for e in expirations if e.startswith("202801")]
        raise RuntimeError(
            f"ThetaData does not list {spec.root} expiry {exp}; "
            f"available Jan 2028 expiries: {jan_2028 or 'none'}"
        )

    strikes = _theta_strikes(report_id, spec.root, exp, spec.right, no_data)
    strike_int = int(round(spec.strike * 1000))
    if strikes and strike_int not in strikes:
        near = sorted(strikes, key=lambda s: abs(s - strike_int))[:5]
        raise RuntimeError(
            f"ThetaData does not list {spec.root} {exp} {spec.right} strike {strike_int}; "
            f"nearest strikes: {near}"
        )

    spot_candles, spot_source = _fetch_spot_candles(report_id, spec.root, spec.entry_date)
    spot_by_date = _spot_lookup(spot_candles)
    option_rows = _fetch_option_eod(report_id, spec, no_data)
    fallback_used = False
    if not option_rows:
        option_rows = _fetch_option_at_time_fallback(report_id, spec, no_data)
        fallback_used = True

    option_candles = _parse_option_candles(option_rows, spec, spot_by_date)
    if not option_candles:
        raise RuntimeError(
            f"No option candles parsed for {spec.root} {spec.expiry} "
            f"{spec.right}{spec.strike:g}. Theta no-data calls: {no_data[:5]}"
        )

    rv30 = realized_vol(spot_candles, 30)
    rv90 = realized_vol(spot_candles, 90)
    if not rv30 or not rv90:
        raise RuntimeError(f"Insufficient spot candles to compute RV30/RV90 for {spec.root}")

    entry = _first_option_entry(option_candles, spec.entry_date)
    current = _last_option_current(option_candles)
    entry_close = _float(entry.get("close")) if entry else None
    current_close = _float(current.get("close")) if current else None
    return_pct = (
        ((current_close / entry_close) - 1.0) * 100.0
        if entry_close is not None and current_close is not None and entry_close > 0
        else None
    )

    payload: dict[str, Any] = {
        "spec": asdict(spec),
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "spot": {
            "symbol": spec.root,
            "currency": "USD",
            "source": spot_source,
            "candles": spot_candles,
            "realized_vol_30d": [{"date": r["date"], "rv": round(float(r["rv"]), 6)} for r in rv30],
            "realized_vol_90d": [{"date": r["date"], "rv": round(float(r["rv"]), 6)} for r in rv90],
        },
        "option": {
            "occ_symbol": _occ_symbol(spec.root, spec.expiry, spec.right, spec.strike),
            "expiry": spec.expiry,
            "strike": spec.strike,
            "right": spec.right,
            "candles": option_candles,
            "entry_close": entry_close,
            "current_close": current_close,
            "return_pct": round(return_pct, 4) if return_pct is not None else None,
        },
        "thesis": _build_thesis(spec, option_candles, rv30, rv90),
        "assumptions": {
            "risk_free_rate": RISK_FREE_RATE,
            "risk_free_rate_note": "Constant 4.5% rate for visualization; not a live SOFR/UST curve.",
            "dividend_yield": DIVIDEND_YIELD,
            "dividend_yield_note": "EWY distributions ignored for this chart.",
            "iv_source": "theta_eod" if any(_field(r, ("iv", "implied_vol", "implied_volatility")) for r in option_rows) else "black_scholes_inversion",
        },
        "data_quality": {
            "theta_no_data": no_data,
            "option_fallback": "at_time_quote" if fallback_used else "eod",
        },
    }

    out_dir = _report_prep(report_id) / "options"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{spec.prediction_id}.json"
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return OptionTrackResult(path=out_path, payload=payload, theta_no_data=no_data)


def _load_specs(path: Path) -> list[OptionSpec]:
    raw = json.loads(path.read_text())
    items = raw if isinstance(raw, list) else [raw]
    specs: list[OptionSpec] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"Spec entries must be objects: {item!r}")
        specs.append(OptionSpec(**item))
    return specs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-id", required=True)
    parser.add_argument("--spec", required=True, help="JSON file with OptionSpec object(s)")
    args = parser.parse_args(argv)

    spec_path = Path(args.spec)
    if not spec_path.is_absolute():
        spec_path = BOOKMARK_ROOT / spec_path
    results: list[OptionTrackResult] = []
    for spec in _load_specs(spec_path):
        result = track_option(spec, args.report_id)
        results.append(result)
        opt = result.payload["option"]
        thesis = result.payload["thesis"]
        print(
            f"{spec.prediction_id}: {opt['occ_symbol']} "
            f"entry={opt['entry_close']} current={opt['current_close']} "
            f"doubled={thesis['doubled']} -> {result.path}"
        )
        if result.theta_no_data:
            print("ThetaData no-data calls:")
            for call in result.theta_no_data:
                print(f"  {call}")
    print(f"wrote {len(results)} option track file(s)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"option_tracker failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
