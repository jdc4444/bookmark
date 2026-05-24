"""Seed the per-report supplement cache from Yahoo Finance.

This script only fetches the explicit symbols requested, or the timeline
symbols that need supplemental data: current `missing_cache` rows plus
market-cap targets that need quote snapshots. It writes after each symbol so a
blocked or interrupted batch can be resumed without losing prior work.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _paths import report_prep, validate_report_id  # noqa: E402
from supplement_cache import (  # noqa: E402
    init_db,
    supplement_db_path,
    upsert_candles,
    upsert_quote,
)


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.6 Safari/605.1.15"
)
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_QUOTE_SUMMARY = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
YAHOO_COOKIE_BOOTSTRAP = "https://fc.yahoo.com"
YAHOO_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
CACHE_FRESH_SECONDS = 60 * 60


class YahooHTTPError(RuntimeError):
    def __init__(self, code: int, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"HTTP {code}: {detail}")


class YahooRateLimitError(YahooHTTPError):
    pass


def make_opener() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [
        ("User-Agent", USER_AGENT),
        ("Accept", "*/*"),
        ("Accept-Language", "en-US,en;q=0.9"),
    ]
    return opener


def get_crumb(opener: urllib.request.OpenerDirector) -> str | None:
    try:
        opener.open(YAHOO_COOKIE_BOOTSTRAP, timeout=8).read()
    except Exception:
        pass  # fc.yahoo.com commonly returns 404; Set-Cookie is the payload.

    try:
        with opener.open(YAHOO_CRUMB_URL, timeout=8) as resp:
            crumb = resp.read().decode("utf-8", errors="replace").strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        if exc.code == 429:
            raise YahooRateLimitError(exc.code, detail) from exc
        return None
    except Exception:
        return None
    return crumb if crumb and len(crumb) <= 80 else None


def _append_crumb(url: str, crumb: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if key != "crumb"
    ]
    query.append(("crumb", crumb))
    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urllib.parse.urlencode(query),
            parts.fragment,
        )
    )


def _http_error_from(exc: urllib.error.HTTPError) -> YahooHTTPError:
    detail = exc.read().decode("utf-8", errors="replace")[:300]
    if exc.code == 429:
        return YahooRateLimitError(exc.code, detail)
    return YahooHTTPError(exc.code, detail)


class YahooClient:
    def __init__(self) -> None:
        self.opener = make_opener()
        self.crumb: str | None = None

    def _refresh_crumb(self) -> str:
        crumb = get_crumb(self.opener)
        if not crumb:
            raise RuntimeError("Yahoo crumb bootstrap failed")
        self.crumb = crumb
        return crumb

    def refresh_auth(self) -> None:
        self.opener = make_opener()
        self.crumb = None
        self._refresh_crumb()

    def fetch_json(self, url: str) -> dict[str, Any]:
        for attempt in range(2):
            crumb = self.crumb or self._refresh_crumb()
            authed_url = _append_crumb(url, crumb)
            try:
                with self.opener.open(authed_url, timeout=30) as resp:
                    body = resp.read().decode("utf-8")
                return json.loads(body)
            except urllib.error.HTTPError as exc:
                error = _http_error_from(exc)
                if error.code in (401, 403) and attempt == 0:
                    self.refresh_auth()
                    continue
                raise error from exc
        raise RuntimeError("Yahoo auth retry exhausted")


def yahoo_chart_url(symbol: str) -> str:
    quoted = urllib.parse.quote(symbol, safe="")
    return f"{YAHOO_CHART.format(symbol=quoted)}?range=5y&interval=1d"


def yahoo_quote_url(symbol: str) -> str:
    quoted = urllib.parse.quote(symbol, safe="")
    query = urllib.parse.urlencode({"modules": "price,defaultKeyStatistics,summaryDetail"})
    return f"{YAHOO_QUOTE_SUMMARY.format(symbol=quoted)}?{query}"


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("raw")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _field(obj: dict[str, Any], *path: str) -> Any:
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    if isinstance(cur, dict) and "raw" in cur:
        return cur.get("raw")
    return cur


def fetch_candles(symbol: str, client: YahooClient) -> list[dict[str, Any]]:
    """5Y daily candles via yfinance.

    Our raw urllib + crumb client got stuck behind Yahoo's rate-limiter
    (cumulative penalty from earlier no-auth attempts kept query1/query2
    returning 429 even after the crumb dance). yfinance handles cookies,
    crumb rotation, and backoff differently and reliably gets through.
    `client` is unused here but kept for signature compatibility.
    """
    del client  # unused
    import yfinance as yf
    t = yf.Ticker(symbol)
    hist = t.history(period="5y", interval="1d", auto_adjust=False, raise_errors=False)
    if hist is None or hist.empty:
        raise RuntimeError("yfinance returned empty history")
    candles: list[dict[str, Any]] = []
    for ts, row in hist.iterrows():
        close = _number(row.get("Close"))
        if close is None:
            continue
        # ts is a tz-aware pandas Timestamp; .timestamp() returns epoch seconds.
        candles.append({
            "time": int(ts.timestamp()),
            "open": _number(row.get("Open")),
            "high": _number(row.get("High")),
            "low": _number(row.get("Low")),
            "close": close,
            "volume": _int_or_none(row.get("Volume")),
        })
    return candles


def fetch_quote(symbol: str, client: YahooClient) -> dict[str, Any]:
    """Quote (market cap, shares, currency, exchange) via yfinance.fast_info."""
    del client  # unused
    import yfinance as yf
    t = yf.Ticker(symbol)
    fi = t.fast_info
    market_cap = None
    shares = None
    currency = None
    exchange = None
    payload: dict[str, Any] = {}
    try:
        market_cap = _number(getattr(fi, "market_cap", None))
    except Exception:
        pass
    try:
        shares = _number(getattr(fi, "shares", None))
    except Exception:
        pass
    try:
        currency = getattr(fi, "currency", None)
    except Exception:
        pass
    try:
        exchange = getattr(fi, "exchange", None)
    except Exception:
        pass
    # Pull the slow-path .info as supplementary payload (handles edge cases
    # like ADRs and OTC where fast_info is incomplete). One-time per symbol;
    # not retried on failure.
    try:
        info = t.info or {}
        payload = {k: info.get(k) for k in (
            "shortName", "longName", "marketCap", "sharesOutstanding",
            "currency", "financialCurrency", "exchange", "fullExchangeName",
            "regularMarketPrice", "regularMarketPreviousClose",
            "fiftyTwoWeekHigh", "fiftyTwoWeekLow",
        ) if info.get(k) is not None}
        if market_cap is None:
            market_cap = _number(info.get("marketCap"))
        if shares is None:
            shares = _number(info.get("sharesOutstanding"))
        if not currency:
            currency = info.get("currency") or info.get("financialCurrency")
        if not exchange:
            exchange = info.get("fullExchangeName") or info.get("exchange")
    except Exception:
        pass
    return {
        "market_cap": market_cap,
        "shares_outstanding": shares,
        "currency": str(currency).upper() if currency else None,
        "exchange": str(exchange) if exchange else None,
        "payload": payload,
    }


def timeline_symbols_to_seed(report_id: str) -> list[str]:
    path = report_prep(report_id) / "timeline.json"
    data = json.loads(path.read_text())
    symbols: list[str] = []
    seen: set[str] = set()
    for row in data.get("predictions") or []:
        if not isinstance(row, dict):
            continue
        target = row.get("target") if isinstance(row.get("target"), dict) else {}
        needs_supplement = (
            row.get("price_status") == "missing_cache"
            or target.get("basis") in {"market_cap", "market_cap_floor"}
        )
        if not needs_supplement:
            continue
        symbol = str(row.get("price_symbol") or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


def _date_from_epoch(epoch: int | float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).date().isoformat()


def _datetime_from_epoch(epoch: int | float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _compact_money(value: float | None) -> str:
    if value is None:
        return "null"
    abs_v = abs(value)
    for suffix, div in (("T", 1_000_000_000_000), ("B", 1_000_000_000), ("M", 1_000_000)):
        if abs_v >= div:
            return f"{value / div:.2f}{suffix}"
    return f"{value:.0f}"


def recent_candles_fetched_at(symbol: str, db_path: Path, now: int | None = None) -> int | None:
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT fetched_at FROM candles_cache WHERE symbol=? AND range=?",
                (symbol, "5Y"),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    fetched_at = int(row[0])
    current = int(now if now is not None else time.time())
    return fetched_at if current - fetched_at < CACHE_FRESH_SECONDS else None


def _rate_limit_backoff_seconds(retries_used: int) -> int:
    return 60 if retries_used == 0 else 120


def seed_symbol(
    symbol: str,
    db_path: Path,
    client: YahooClient,
    retry: int,
) -> tuple[bool, str]:
    fetched_at = int(time.time())
    candles: list[dict[str, Any]] = []
    quote: dict[str, Any] | None = None
    errors: dict[str, str] = {}
    needs_candles = True
    needs_quote = True
    retries_used = 0
    max_retries = max(retry, 0)

    while needs_candles or needs_quote:
        rate_limited = False

        if needs_candles:
            try:
                candles = fetch_candles(symbol, client)
                upsert_candles(symbol, "5Y", candles, fetched_at, db_path)
                needs_candles = False
                errors.pop("candles", None)
            except YahooRateLimitError as exc:
                errors["candles"] = str(exc)
                rate_limited = True
            except Exception as exc:  # noqa: BLE001 - keep batch resumable
                errors["candles"] = str(exc)
                needs_candles = False

        if needs_quote and not rate_limited:
            try:
                quote = fetch_quote(symbol, client)
                upsert_quote(
                    symbol,
                    quote.get("market_cap"),
                    quote.get("shares_outstanding"),
                    quote.get("currency"),
                    quote.get("exchange"),
                    quote.get("payload"),
                    db_path,
                    fetched_at=fetched_at,
                )
                needs_quote = False
                errors.pop("quote", None)
            except YahooRateLimitError as exc:
                errors["quote"] = str(exc)
                rate_limited = True
            except Exception as exc:  # noqa: BLE001 - keep batch resumable
                errors["quote"] = str(exc)
                needs_quote = False

        if rate_limited and retries_used < max_retries:
            delay = _rate_limit_backoff_seconds(retries_used)
            print(
                f"{symbol}: Yahoo 429; sleeping {delay}s "
                f"before retry {retries_used + 1}/{max_retries}",
                flush=True,
            )
            time.sleep(delay)
            retries_used += 1
            continue
        break

    if needs_quote and "quote" not in errors:
        errors["quote"] = "skipped after Yahoo rate limit"

    first = _date_from_epoch(candles[0]["time"]) if candles else None
    last = _date_from_epoch(candles[-1]["time"]) if candles else None
    market_cap = quote.get("market_cap") if quote else None
    currency = quote.get("currency") if quote else None
    summary = (
        f"{symbol}: n_candles={len(candles)}, "
        f"{first or 'null'}->{last or 'null'}, "
        f"market_cap={_compact_money(market_cap)}, currency={currency or 'null'}"
    )
    if errors:
        ordered_errors = [
            f"{kind}: {errors[kind]}"
            for kind in ("candles", "quote")
            if kind in errors
        ]
        summary = f"{summary}, errors={'; '.join(ordered_errors)}"
    return not errors, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-id", required=True)
    parser.add_argument("--symbols", nargs="*", help="Explicit Yahoo symbols to seed")
    parser.add_argument("--supplement-db", help="Override supplement SQLite path")
    parser.add_argument("--retry", type=int, default=2, help="Max Yahoo 429 retry attempts per symbol")
    parser.add_argument(
        "--rate-limit-sleep",
        "--sleep",
        dest="rate_limit_sleep",
        type=float,
        default=0.5,
        help="Seconds between symbols",
    )
    parser.add_argument("--force", action="store_true", help="Refetch even if the 5Y cache is fresh")
    args = parser.parse_args(argv)
    validate_report_id(args.report_id)

    symbols = [s.strip().upper() for s in (args.symbols or []) if s.strip()]
    if not symbols:
        symbols = timeline_symbols_to_seed(args.report_id)

    db_path = Path(args.supplement_db) if args.supplement_db else supplement_db_path(args.report_id)
    init_db(db_path)

    print(f"supplement_db: {db_path}")
    print(f"symbols: {', '.join(symbols) if symbols else '(none)'}")
    failures = 0
    client = YahooClient()
    for idx, symbol in enumerate(symbols):
        fetched_recently = None if args.force else recent_candles_fetched_at(symbol, db_path)
        if fetched_recently is not None:
            print(f"{symbol}: skipped recent 5Y cache, fetched_at={_datetime_from_epoch(fetched_recently)}")
            continue

        ok, summary = seed_symbol(symbol, db_path, client, args.retry)
        print(summary)
        if not ok:
            failures += 1
        if idx < len(symbols) - 1:
            time.sleep(max(args.rate_limit_sleep, 0.0))

    # FX rates — fetch the four currencies our targets/quotes commonly use
    # vs USD. The progress calculator routes cross-pairs through USD.
    fx_pairs = [
        ("USDSEK", "SEK=X"),    # USDSEK=X is also valid; both work
        ("USDEUR", "EUR=X"),
        ("USDJPY", "JPY=X"),
        ("USDTWD", "TWD=X"),
        ("USDGBP", "GBP=X"),
        ("USDKRW", "KRW=X"),
        ("USDHKD", "HKD=X"),
    ]
    print()
    seed_fx_rates(fx_pairs, db_path, sleep=max(args.rate_limit_sleep, 0.0))
    return 1 if failures else 0


def seed_fx_rates(pairs: list[tuple[str, str]], db_path: Path,
                  sleep: float = 0.5) -> None:
    """Fetch a small set of USD-X FX rates via yfinance and upsert into the
    supplement DB's fx_cache table. Quiet failures — FX is best-effort; if a
    rate can't be fetched, the conversion path will still fall back to
    `currency_mismatch` for predictions that need that pair."""
    try:
        import yfinance as yf
    except Exception as e:
        print(f"[fx] yfinance not available: {e}")
        return
    from supplement_cache import upsert_fx
    for idx, (pair, yf_symbol) in enumerate(pairs):
        try:
            t = yf.Ticker(yf_symbol)
            hist = t.history(period="5d", interval="1d", raise_errors=False)
            if hist is None or hist.empty:
                print(f"[fx] {pair}: empty history")
                continue
            rate = float(hist["Close"].iloc[-1])
            upsert_fx(pair, rate, db_path)
            print(f"[fx] {pair} = {rate:.6f}")
        except Exception as e:
            print(f"[fx] {pair}: error {e}")
        if idx < len(pairs) - 1:
            time.sleep(sleep)


if __name__ == "__main__":
    sys.exit(main())
