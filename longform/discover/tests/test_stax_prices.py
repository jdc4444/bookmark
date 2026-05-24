from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stax_prices import price_snapshot
from supplement_cache import upsert_candles, upsert_quote
from timeline_progress import enrich_prediction


def _empty_stax_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE candles_cache (
                symbol TEXT NOT NULL,
                range TEXT NOT NULL,
                fetched_at INTEGER NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (symbol, range)
            )
            """
        )


def _candles(entry_close: float = 10.0, current_close: float = 12.0):
    return [
        {
            "time": 1_704_153_600,
            "open": entry_close,
            "high": entry_close,
            "low": entry_close,
            "close": entry_close,
            "volume": 100,
        },
        {
            "time": 1_714_521_600,
            "open": current_close,
            "high": current_close,
            "low": current_close,
            "close": current_close,
            "volume": 200,
        },
    ]


def test_price_snapshot_falls_through_to_supplement_cache(tmp_path):
    stax_db = tmp_path / "stax.sqlite3"
    supplement_db = tmp_path / "supplement.sqlite3"
    _empty_stax_db(stax_db)
    upsert_candles("MISS", "5Y", _candles(), 1_714_600_000, supplement_db)

    snapshot = price_snapshot(
        "MISS",
        "2024-01-02",
        db_path=stax_db,
        supplement_db=supplement_db,
    )

    assert snapshot["price_status"] == "ok"
    assert snapshot["price_range"] == "5Y"
    assert snapshot["entry_price"] == 10
    assert snapshot["current_price"] == 12


def test_market_cap_prediction_uses_quote_and_current_share_proxy(tmp_path):
    stax_db = tmp_path / "stax.sqlite3"
    supplement_db = tmp_path / "supplement.sqlite3"
    _empty_stax_db(stax_db)
    upsert_candles("TEST", "5Y", _candles(), 1_714_600_000, supplement_db)
    upsert_quote(
        "TEST",
        market_cap=1_200_000_000,
        shares=100_000_000,
        currency="USD",
        exchange="NasdaqGS",
        payload={},
        db_path=supplement_db,
        fetched_at=1_714_600_001,
    )
    source = {
        "tweet_id": "1",
        "tickers": ["TEST"],
        "date": "2024-01-02",
        "prediction": "TEST can hit a $1.5B market cap.",
        "target_value": "$1.5B market cap",
        "target_metric": "price",
        "target_date": "2025-01-02",
    }

    row = enrich_prediction(source, "TEST", {}, stax_db, supplement_db, date(2024, 5, 9))

    assert row["price_status"] == "ok"
    assert row["status"] == "in_progress"
    assert row["entry_market_cap"] == 1_000_000_000
    assert row["current_market_cap"] == 1_200_000_000
    assert row["target_market_cap"] == 1_500_000_000
    assert row["progress_pct_current"] == 40.0


def test_market_cap_floor_hits_and_caps_progress(tmp_path):
    stax_db = tmp_path / "stax.sqlite3"
    supplement_db = tmp_path / "supplement.sqlite3"
    _empty_stax_db(stax_db)
    upsert_candles("TEST", "5Y", _candles(), 1_714_600_000, supplement_db)
    upsert_quote(
        "TEST",
        market_cap=1_200_000_000,
        shares=100_000_000,
        currency="USD",
        exchange="NasdaqGS",
        payload={},
        db_path=supplement_db,
        fetched_at=1_714_600_001,
    )
    source = {
        "tweet_id": "floor",
        "tickers": ["TEST"],
        "date": "2024-01-02",
        "prediction": "TEST is undervalued at a $800M valuation.",
        "target_value": "$800M valuation floor",
        "target_metric": "price",
        "target_date": "2025-01-02",
    }

    row = enrich_prediction(source, "TEST", {}, stax_db, supplement_db, date(2024, 5, 9))

    assert row["target"]["basis"] == "market_cap_floor"
    assert row["status"] == "hit"
    assert row["target_market_cap"] == 800_000_000
    assert row["progress_pct_current"] == 100.0


def test_market_cap_range_hits_high_end_with_high_denominator(tmp_path):
    stax_db = tmp_path / "stax.sqlite3"
    supplement_db = tmp_path / "supplement.sqlite3"
    _empty_stax_db(stax_db)
    upsert_candles("TEST", "5Y", _candles(), 1_714_600_000, supplement_db)
    upsert_quote(
        "TEST",
        market_cap=1_200_000_000,
        shares=100_000_000,
        currency="USD",
        exchange="NasdaqGS",
        payload={},
        db_path=supplement_db,
        fetched_at=1_714_600_001,
    )
    source = {
        "tweet_id": "range",
        "tickers": ["TEST"],
        "date": "2024-01-02",
        "prediction": "TEST market cap could reach $1.1B-$1.2B in a year.",
        "target_value": "$1.1B-$1.2B market cap",
        "target_metric": "price",
        "target_date": "2025-01-02",
    }

    row = enrich_prediction(source, "TEST", {}, stax_db, supplement_db, date(2024, 5, 9))

    assert row["status"] == "hit"
    assert row["target_market_cap_low"] == 1_100_000_000
    assert row["target_market_cap_high"] == 1_200_000_000
    assert row["target_market_cap"] == 1_200_000_000
    assert row["progress_pct_current"] == 100.0


def test_market_cap_up_target_below_entry_never_goes_negative(tmp_path):
    stax_db = tmp_path / "stax.sqlite3"
    supplement_db = tmp_path / "supplement.sqlite3"
    _empty_stax_db(stax_db)
    upsert_candles("TEST", "5Y", _candles(current_close=7), 1_714_600_000, supplement_db)
    upsert_quote(
        "TEST",
        market_cap=700_000_000,
        shares=100_000_000,
        currency="USD",
        exchange="NasdaqGS",
        payload={},
        db_path=supplement_db,
        fetched_at=1_714_600_001,
    )
    source = {
        "tweet_id": "below-entry",
        "tickers": ["TEST"],
        "date": "2024-01-02",
        "prediction": "TEST can hit a $800M market cap.",
        "target_value": "$800M market cap",
        "target_metric": "price",
        "target_date": "2025-01-02",
    }

    row = enrich_prediction(source, "TEST", {}, stax_db, supplement_db, date(2024, 5, 9))

    assert row["status"] == "in_progress"
    assert row["entry_market_cap"] == 1_000_000_000
    assert row["target_market_cap"] == 800_000_000
    assert row["progress_pct_current"] == 0.0


def test_revenue_multiple_prediction_is_qualitative(tmp_path):
    stax_db = tmp_path / "stax.sqlite3"
    supplement_db = tmp_path / "supplement.sqlite3"
    _empty_stax_db(stax_db)
    upsert_candles("TEST", "5Y", _candles(), 1_714_600_000, supplement_db)
    source = {
        "tweet_id": "revenue",
        "tickers": ["TEST"],
        "date": "2024-01-02",
        "prediction": "TEST revenue will ramp 10x by 2026.",
        "target_value": "revenue ramp 10x",
        "target_metric": "price",
        "target_date": "2026-12-31",
    }

    row = enrich_prediction(source, "TEST", {}, stax_db, supplement_db, date(2024, 5, 9))

    assert row["target"]["basis"] == "revenue"
    assert row["status"] == "qualitative"
    assert row["progress_pct_current"] is None


def test_market_cap_currency_mismatch_blocks_progress(tmp_path):
    stax_db = tmp_path / "stax.sqlite3"
    supplement_db = tmp_path / "supplement.sqlite3"
    _empty_stax_db(stax_db)
    upsert_candles("SIVE.ST", "5Y", _candles(), 1_714_600_000, supplement_db)
    upsert_quote(
        "SIVE.ST",
        market_cap=1_200_000_000,
        shares=100_000_000,
        currency="SEK",
        exchange="Stockholm",
        payload={},
        db_path=supplement_db,
        fetched_at=1_714_600_001,
    )
    source = {
        "tweet_id": "2",
        "tickers": ["SIVE"],
        "date": "2024-01-02",
        "prediction": "SIVE can hit a $10B valuation.",
        "target_value": "$10B valuation",
        "target_metric": "other",
        "target_date": "2025-01-02",
    }
    aliases = {"SIVE": {"symbol": "SIVE.ST", "currency": "SEK"}}

    row = enrich_prediction(source, "SIVE", aliases, stax_db, supplement_db, date(2024, 5, 9))

    assert row["price_status"] == "currency_mismatch"
    assert row["status"] == "currency_mismatch"
    assert row["quote_currency"] == "SEK"
    assert row["progress_pct_current"] is None
