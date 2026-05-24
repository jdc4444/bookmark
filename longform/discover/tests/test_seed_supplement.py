from __future__ import annotations

import sys
import urllib.error
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import seed_supplement
from seed_supplement import YahooRateLimitError, _append_crumb, recent_candles_fetched_at, seed_symbol
from supplement_cache import upsert_candles


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return self.body


def test_append_crumb_preserves_query_and_replaces_existing_crumb():
    url = "https://query1.finance.yahoo.com/v8/finance/chart/SIVE.ST?range=5y&crumb=old&interval=1d"

    authed = _append_crumb(url, "new-token")

    assert authed == (
        "https://query1.finance.yahoo.com/v8/finance/chart/SIVE.ST"
        "?range=5y&interval=1d&crumb=new-token"
    )


def test_recent_candles_fetched_at_uses_one_hour_window(tmp_path):
    db_path = tmp_path / "supplement.sqlite3"
    upsert_candles("TEST", "5Y", [], 1_000, db_path)

    assert recent_candles_fetched_at("TEST", db_path, now=4_599) == 1_000
    assert recent_candles_fetched_at("TEST", db_path, now=4_600) is None


def test_seed_symbol_retries_quote_429_without_refetching_candles(monkeypatch, tmp_path):
    db_path = tmp_path / "supplement.sqlite3"
    calls = {"candles": 0, "quote": 0}
    sleeps: list[int] = []
    candles = [
        {
            "time": 1_700_000_000,
            "open": 9.5,
            "high": 10.5,
            "low": 9.0,
            "close": 10.0,
            "volume": 1234,
        }
    ]

    def fake_fetch_candles(symbol, client):
        calls["candles"] += 1
        return candles

    def fake_fetch_quote(symbol, client):
        calls["quote"] += 1
        if calls["quote"] == 1:
            raise YahooRateLimitError(429, "busy")
        return {
            "market_cap": 1_200_000_000,
            "shares_outstanding": 100_000_000,
            "currency": "USD",
            "exchange": "NasdaqGS",
            "payload": {},
        }

    monkeypatch.setattr(seed_supplement, "fetch_candles", fake_fetch_candles)
    monkeypatch.setattr(seed_supplement, "fetch_quote", fake_fetch_quote)
    monkeypatch.setattr(seed_supplement.time, "sleep", lambda seconds: sleeps.append(seconds))

    ok, summary = seed_symbol("TEST", db_path, object(), retry=2)

    assert ok
    assert calls == {"candles": 1, "quote": 2}
    assert sleeps == [60]
    assert "errors=" not in summary


def test_yahoo_client_rebootstraps_once_after_403(monkeypatch):
    class FakeOpener:
        def __init__(self, name):
            self.name = name
            self.urls: list[str] = []

        def open(self, url, timeout):
            self.urls.append(url)
            if self.name == "first":
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, BytesIO(b"forbidden"))
            return _Response(b'{"ok": true}')

    openers: list[FakeOpener] = []

    def fake_make_opener():
        opener = FakeOpener("first" if not openers else "second")
        openers.append(opener)
        return opener

    def fake_get_crumb(opener):
        return f"{opener.name}-crumb"

    monkeypatch.setattr(seed_supplement, "make_opener", fake_make_opener)
    monkeypatch.setattr(seed_supplement, "get_crumb", fake_get_crumb)

    client = seed_supplement.YahooClient()

    assert client.fetch_json("https://query1.finance.yahoo.com/test?range=5y") == {"ok": True}
    assert "crumb=first-crumb" in openers[0].urls[0]
    assert "crumb=second-crumb" in openers[1].urls[0]
