"""Fan out and enrich extracted timeline predictions with Stax price progress.

Reads `data/longform/<report_id>/prep/timeline.json`, converts legacy
tweet-level predictions to schema_version=2 per-(tweet,ticker) records, and
writes the enriched timeline back to the same path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _paths import report_prep, validate_report_id  # noqa: E402
from stax_prices import (  # noqa: E402
    DEFAULT_STAX_DB,
    load_aliases,
    read_quote,
    resolve_symbol,
    rounded_price_fields,
    target_currency_mismatch,
    price_snapshot,
)
from supplement_cache import supplement_db_path  # noqa: E402
from target_parser import (  # noqa: E402
    compact_ticker,
    parse_target,
    progress_pct,
    resolve_price_target,
)


QUALITATIVE_BASES = {
    "comparative", "benchmark_relative", "revenue",
    "earnings", "catalyst_event", "other", "derivative", "option",
}
MARKET_CAP_BASES = {"market_cap", "market_cap_floor"}


def prediction_id(tweet_id: str, ticker: str) -> str:
    return hashlib.sha1(f"{tweet_id}|{ticker}".encode("utf-8")).hexdigest()[:12]


def normalize_ticker(ticker: str | None) -> str:
    return (ticker or "").strip().lstrip("$").upper()


def clean_ticker_list(tickers: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for ticker in tickers or []:
        tk = normalize_ticker(str(ticker))
        if not tk or tk in seen:
            continue
        seen.add(tk)
        out.append(tk)
    return out


def evaluated_tickers(row: dict[str, Any]) -> list[str]:
    """Drop obvious benchmark-only tickers while preserving source_tickers."""
    tickers = clean_ticker_list(row.get("tickers") or row.get("source_tickers") or [])
    text = f"{row.get('prediction') or ''} {row.get('target_value') or ''}".lower()
    out: list[str] = []
    for ticker in tickers:
        compact = compact_ticker(ticker)
        is_benchmark = False
        if ticker in {"SPY", "QQQ"} and "outperform" in text:
            is_benchmark = True
        if ticker == "AAPL" and ("more than apple" in text or "more than aapl" in text):
            is_benchmark = True
        if compact in {"AAPL"} and ("more than apple" in text or "more than aapl" in text):
            is_benchmark = True
        if not is_benchmark:
            out.append(ticker)
    return out or tickers


def parse_target_end(target_date: str | None) -> date | None:
    if not target_date:
        return None
    s = str(target_date).strip()
    try:
        if len(s) == 10:
            return date.fromisoformat(s)
        if len(s) == 4 and s.isdigit():
            return date(int(s), 12, 31)
        if "-Q" in s:
            year_s, q_s = s.split("-Q", 1)
            q = int(q_s[:1])
            month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}.get(q)
            if month_day:
                return date(int(year_s), *month_day)
        if "-H" in s:
            year_s, h_s = s.split("-H", 1)
            h = int(h_s[:1])
            return date(int(year_s), 6, 30) if h == 1 else date(int(year_s), 12, 31)
    except (TypeError, ValueError):
        return None
    return None


def classify_status(row: dict[str, Any], today: date) -> str:
    price_status = row.get("price_status")
    target = row.get("target") or {}
    basis = target.get("basis")
    progress = row.get("progress_pct_current")

    if price_status == "symbol_unresolved":
        return "symbol_unresolved"
    if price_status == "missing_cache":
        return "missing_cache"
    if price_status == "currency_mismatch":
        return "currency_mismatch"
    if target.get("unresolved") or basis == "unresolved":
        return "unresolved_target"
    if basis in MARKET_CAP_BASES:
        cap_status = market_cap_status(row, today)
        if cap_status:
            return cap_status
    if progress is None:
        return "qualitative" if basis in QUALITATIVE_BASES else "no_price_progress"
    if progress >= 100:
        return "hit"
    end = parse_target_end(target.get("target_date"))
    if end and end < today:
        return "miss"
    return "in_progress"


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def market_cap_threshold(target: dict[str, Any]) -> float | None:
    basis = target.get("basis")
    if (basis == "market_cap"
            and target.get("market_cap_low") is not None
            and target.get("market_cap_high") is not None):
        return _float_or_none(target.get("market_cap_high"))
    if (basis == "market_cap_floor"
            and target.get("market_cap_low") is not None
            and target.get("market_cap_high") is not None):
        return _float_or_none(target.get("market_cap_low"))
    for key in ("market_cap", "market_cap_high", "market_cap_low"):
        value = _float_or_none(target.get(key))
        if value is not None:
            return value
    return None


def _row_market_cap_target(row: dict[str, Any], key: str, target_key: str) -> float | None:
    value = _float_or_none(row.get(key))
    if value is not None:
        return value
    target = row.get("target") or {}
    return _float_or_none(target.get(target_key))


def market_cap_progress_value(
    target: dict[str, Any],
    entry_market_cap: float | None,
    current_market_cap: float | None,
    target_market_cap: float | None,
) -> float | None:
    entry = _float_or_none(entry_market_cap)
    current = _float_or_none(current_market_cap)
    threshold = _float_or_none(target_market_cap)
    if entry is None or current is None or threshold is None:
        return None

    direction = target.get("direction") or "up"
    if direction == "down":
        denom = entry - threshold
        if denom <= 0:
            return 100.0 if current <= threshold else 0.0
        return max(0.0, ((entry - current) / denom) * 100.0)

    if target.get("basis") == "market_cap_floor":
        if current >= threshold:
            return 100.0
        denom = threshold - entry
        if denom <= 0:
            return 0.0
        return max(0.0, min(((current - entry) / denom) * 100.0, 100.0))

    denom = threshold - entry
    if denom <= 0:
        return 100.0 if current >= threshold else 0.0
    return max(0.0, ((current - entry) / denom) * 100.0)


def market_cap_status(row: dict[str, Any], today: date) -> str | None:
    target = row.get("target") or {}
    basis = target.get("basis")
    direction = target.get("direction") or "up"
    current = _float_or_none(row.get("current_market_cap"))
    threshold = _float_or_none(row.get("target_market_cap"))
    if threshold is None:
        threshold = market_cap_threshold(target)
    if current is None or threshold is None:
        return None

    end = parse_target_end(target.get("target_date"))
    missed = bool(end and end < today)
    if direction == "down":
        if current <= threshold:
            return "hit"
        return "miss" if missed else "in_progress"

    if basis == "market_cap" and target.get("market_cap_low") is not None:
        low = _row_market_cap_target(row, "target_market_cap_low", "market_cap_low")
        high = _row_market_cap_target(row, "target_market_cap_high", "market_cap_high")
        if low is not None and high is not None:
            low, high = min(low, high), max(low, high)
            if current >= high:
                return "hit"
            if current >= low:
                return "in_progress"
            return "miss" if missed else "in_progress"

    if current >= threshold:
        return "hit"
    return "miss" if missed else "in_progress"


def market_cap_currency_mismatch(target: dict[str, Any], quote_currency: str | None) -> bool:
    target_currency = target.get("market_cap_currency")
    if not target_currency or not quote_currency:
        return False
    return str(target_currency).upper() != str(quote_currency).upper()


def market_cap_progress_fields(
    target: dict[str, Any],
    price_fields: dict[str, Any],
    quote: dict[str, Any] | None,
    supplement_db: Path | None = None,
) -> tuple[dict[str, Any], float | None]:
    fields: dict[str, Any] = {
        "quote_currency": None,
        "quote_exchange": None,
        "shares_outstanding": None,
        "entry_market_cap": None,
        "current_market_cap": None,
        "target_market_cap": market_cap_threshold(target),
        "target_market_cap_low": _float_or_none(target.get("market_cap_low")),
        "target_market_cap_high": _float_or_none(target.get("market_cap_high")),
    }
    if not quote:
        price_fields["price_status"] = "missing_cache"
        return fields, None

    fields["quote_currency"] = quote.get("currency")
    fields["quote_exchange"] = quote.get("exchange")
    fields["shares_outstanding"] = quote.get("shares_outstanding")
    fields["current_market_cap"] = quote.get("market_cap")

    target_currency = (target.get("market_cap_currency") or "").upper() or None
    quote_currency = (quote.get("currency") or "").upper() or None
    target_market_cap = fields["target_market_cap"]

    # If currencies differ, attempt FX conversion of the *target* market cap
    # into the quote currency. We convert target → quote (rather than the
    # other way) so all comparisons happen in the quote's native unit.
    if (target_currency and quote_currency
            and target_currency != quote_currency
            and target_market_cap is not None
            and supplement_db is not None):
        try:
            from supplement_cache import fx_convert
            fx_note = None
            for key in ("target_market_cap", "target_market_cap_low",
                        "target_market_cap_high"):
                val = fields.get(key)
                if val is None:
                    continue
                converted, fx_note = fx_convert(
                    float(val), target_currency, quote_currency, supplement_db,
                )
                if converted is None:
                    price_fields["price_status"] = "currency_mismatch"
                    return fields, None
                fields[f"{key}_native"] = float(val)
                fields[key] = converted
        except Exception:
            price_fields["price_status"] = "currency_mismatch"
            return fields, None
        target_market_cap = fields["target_market_cap"]
        fields["target_market_cap_native_currency"] = target_currency
        fields["fx_converted"] = True
        fields["fx_rate_note"] = fx_note
    elif market_cap_currency_mismatch(target, quote.get("currency")):
        # No FX cache available — preserve the prior behavior.
        price_fields["price_status"] = "currency_mismatch"
        return fields, None

    entry_price = price_fields.get("entry_price")
    shares = quote.get("shares_outstanding")
    current_market_cap = quote.get("market_cap")
    if (
        entry_price is None
        or shares is None
        or current_market_cap is None
        or target_market_cap is None
    ):
        price_fields["price_status"] = "missing_cache"
        return fields, None

    try:
        entry_market_cap = float(shares) * float(entry_price)
        current = float(current_market_cap)
        threshold = float(target_market_cap)
    except (TypeError, ValueError):
        price_fields["price_status"] = "missing_cache"
        return fields, None

    fields["entry_market_cap"] = entry_market_cap
    return fields, market_cap_progress_value(
        target, entry_market_cap, current, threshold,
    )


def enrich_prediction(
    source: dict[str, Any],
    ticker: str,
    aliases: dict[str, dict[str, Any]],
    db_path: Path,
    supplement_db: Path | None,
    today: date,
) -> dict[str, Any]:
    tweet_id = str(source.get("tweet_id") or "")
    source_tickers = clean_ticker_list(source.get("tickers") or source.get("source_tickers") or [ticker])
    existing_target = source.get("target") if isinstance(source.get("target"), dict) else None
    if source.get("prediction_id") and existing_target and existing_target.get("raw"):
        source_for_parse = {
            **source,
            "target_value": existing_target.get("raw"),
            "target_metric": existing_target.get("metric"),
            "target_date": existing_target.get("target_date"),
        }
        target = parse_target(source_for_parse, ticker=ticker)
    elif source.get("prediction_id") and existing_target:
        target = dict(existing_target)
    else:
        target = parse_target(source, ticker=ticker)

    resolution = resolve_symbol(ticker, aliases)
    price_fields: dict[str, Any] = {
        "price_symbol": resolution.symbol,
        "price_currency": resolution.currency,
        "price_status": resolution.status,
    }
    if resolution.status == "ok" and resolution.symbol:
        snap = rounded_price_fields(price_snapshot(
            resolution.symbol,
            str(source.get("date") or "")[:10],
            db_path=db_path,
            supplement_db=supplement_db,
        ))
        price_fields.update(snap)

    market_cap_progress: float | None = None
    if price_fields.get("price_status") == "ok":
        target = resolve_price_target(target, price_fields.get("entry_price"))
        if (target.get("basis") not in MARKET_CAP_BASES
                and target_currency_mismatch(target, price_fields.get("price_currency"))):
            # Try FX: convert any resolved target_price* fields to the quote currency.
            from supplement_cache import fx_convert
            tcur = (target.get("target_currency") or "").upper() or None
            qcur = (price_fields.get("price_currency") or "").upper() or None
            converted_any = False
            if supplement_db and tcur and qcur:
                for key in ("target_price", "target_price_low",
                            "target_price_high", "threshold_price"):
                    val = target.get(key)
                    if isinstance(val, (int, float)):
                        new_val, fx_note = fx_convert(float(val), tcur, qcur, supplement_db)
                        if new_val is not None:
                            target[f"{key}_native"] = float(val)
                            target[key] = new_val
                            converted_any = True
                            target["fx_rate_note"] = fx_note
                if converted_any:
                    target["fx_converted"] = True
                    target["target_currency_native"] = tcur
                    target["target_currency"] = qcur
            if not converted_any:
                price_fields["price_status"] = "currency_mismatch"

    if target.get("basis") in MARKET_CAP_BASES and resolution.symbol:
        quote = read_quote(resolution.symbol, supplement_db=supplement_db, db_path=db_path)
        cap_fields, market_cap_progress = market_cap_progress_fields(
            target, price_fields, quote, supplement_db=supplement_db,
        )
        price_fields.update(cap_fields)

    progress = None
    if price_fields.get("price_status") == "ok":
        if target.get("basis") in MARKET_CAP_BASES:
            progress = market_cap_progress
        else:
            progress = progress_pct(
                target,
                price_fields.get("entry_price"),
                price_fields.get("current_price"),
            )

    for key in (
        "price_range",
        "entry_date",
        "entry_price",
        "current_date",
        "current_price",
        "return_pct_current",
        "quote_currency",
        "quote_exchange",
        "shares_outstanding",
        "entry_market_cap",
        "current_market_cap",
        "target_market_cap",
        "target_market_cap_low",
        "target_market_cap_high",
    ):
        price_fields.setdefault(key, None)

    row: dict[str, Any] = {
        "prediction_id": prediction_id(tweet_id, ticker),
        "tweet_id": tweet_id,
        "source_tickers": source_tickers,
        "ticker": ticker,
        "date": str(source.get("date") or "")[:10],
        "url": source.get("url") or "",
        "prediction": source.get("prediction") or "",
        "stance": source.get("stance"),
        "conviction": source.get("conviction"),
        "target": round_target(target),
        **price_fields,
        "progress_pct_current": round(progress, 2) if progress is not None else None,
    }
    row["status"] = classify_status(row, today)
    return row


def round_target(target: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in target.items():
        if isinstance(value, float):
            if "market_cap" in key:
                out[key] = round(value, 2)
            else:
                out[key] = round(value, 4)
        else:
            out[key] = value
    return out


def fanout_predictions(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    predictions = timeline.get("predictions") or []
    if predictions and isinstance(predictions[0], dict) and predictions[0].get("prediction_id"):
        return predictions
    rows: list[dict[str, Any]] = []
    for source in predictions:
        if not isinstance(source, dict):
            continue
        for ticker in evaluated_tickers(source):
            rows.append({**source, "ticker": ticker})
    rows.sort(key=lambda r: (str(r.get("date") or ""), str(r.get("tweet_id") or ""), str(r.get("ticker") or "")))
    return rows


def enrich_timeline(
    report_id: str,
    db_path: Path = DEFAULT_STAX_DB,
    supplement_db: Path | None = None,
) -> dict[str, Any]:
    timeline_path = report_prep(report_id) / "timeline.json"
    raw = json.loads(timeline_path.read_text())
    aliases = load_aliases()
    today = datetime.now(timezone.utc).date()
    supplement_db = supplement_db if supplement_db is not None else supplement_db_path(report_id)

    fanout = fanout_predictions(raw)
    enriched: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in fanout:
        ticker = normalize_ticker(item.get("ticker"))
        if not ticker:
            continue
        row = enrich_prediction(item, ticker, aliases, db_path, supplement_db, today)
        if row["prediction_id"] in seen:
            continue
        seen.add(row["prediction_id"])
        enriched.append(row)

    enriched.sort(key=lambda r: (r["date"], r["tweet_id"], r["ticker"]))
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for row in enriched:
        by_ticker.setdefault(row["ticker"], []).append(row)

    status_counts = dict(sorted(Counter(row["status"] for row in enriched).items()))
    price_status_counts = dict(sorted(Counter(row["price_status"] for row in enriched).items()))

    out: dict[str, Any] = {
        "schema_version": 2,
        "report_id": raw.get("report_id") or report_id,
        "handle": raw.get("handle"),
        "extracted_at": raw.get("extracted_at"),
        "enriched_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "window": raw.get("window") or {},
        "n_candidates": raw.get("n_candidates"),
        "n_batches": raw.get("n_batches"),
        "n_source_predictions": raw.get("n_source_predictions") or len(raw.get("predictions") or []),
        "n_predictions": len(enriched),
        "status_counts": status_counts,
        "price_status_counts": price_status_counts,
        "predictions": enriched,
        "by_ticker": by_ticker,
    }
    return out


def write_timeline(report_id: str, timeline: dict[str, Any]) -> Path:
    out_path = report_prep(report_id) / "timeline.json"
    out_path.write_text(json.dumps(timeline, indent=2, ensure_ascii=False))
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-id", required=True)
    parser.add_argument("--stax-db", default=str(DEFAULT_STAX_DB),
                        help="Path to local Stax SQLite DB")
    parser.add_argument("--supplement-db",
                        help="Path to per-report supplement SQLite DB")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite timeline.json; current default already overwrites")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print summary without writing timeline.json")
    args = parser.parse_args(argv)
    validate_report_id(args.report_id)

    supplement_db = Path(args.supplement_db) if args.supplement_db else supplement_db_path(args.report_id)
    timeline = enrich_timeline(args.report_id, db_path=Path(args.stax_db), supplement_db=supplement_db)
    out_path = report_prep(args.report_id) / "timeline.json"
    if not args.dry_run:
        out_path = write_timeline(args.report_id, timeline)

    print(f"schema_version: {timeline.get('schema_version')}")
    print(f"predictions: {timeline.get('n_predictions')}")
    print(f"status_counts: {json.dumps(timeline.get('status_counts'), sort_keys=True)}")
    print(f"price_status_counts: {json.dumps(timeline.get('price_status_counts'), sort_keys=True)}")
    if not args.dry_run:
        print(f"wrote: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
