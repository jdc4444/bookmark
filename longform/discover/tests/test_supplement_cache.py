from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from supplement_cache import read_candles, read_quote, upsert_candles, upsert_quote


def test_round_trips_candles_and_quote(tmp_path):
    db_path = tmp_path / "stax_supplement.sqlite3"
    candles = [
        {"time": 1_700_000_000, "open": 9.5, "high": 10.5, "low": 9.0, "close": 10.0, "volume": 1234},
        {"time": 1_700_086_400, "open": 10.0, "high": 11.5, "low": 9.8, "close": 11.0, "volume": 2345},
    ]

    upsert_candles("TEST", "5Y", candles, 1_700_100_000, db_path)
    upsert_quote(
        "TEST",
        market_cap=1_200_000_000,
        shares=100_000_000,
        currency="USD",
        exchange="NasdaqGS",
        payload={"price": {"currency": "USD"}},
        db_path=db_path,
        fetched_at=1_700_100_001,
    )

    assert read_candles("TEST", "5Y", db_path) == candles
    quote = read_quote("TEST", db_path)
    assert quote is not None
    assert quote["market_cap"] == 1_200_000_000
    assert quote["shares_outstanding"] == 100_000_000
    assert quote["currency"] == "USD"
    assert quote["exchange"] == "NasdaqGS"
    assert quote["payload"] == {"price": {"currency": "USD"}}
