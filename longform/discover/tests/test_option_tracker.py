from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from option_tracker import (  # noqa: E402
    black_scholes_price,
    has_doubled,
    implied_volatility,
    realized_vol,
)


def test_black_scholes_iv_inversion_round_trips_call():
    price = black_scholes_price(spot=128.0, strike=160.0, years=1.94, rate=0.045, vol=0.33, right="C")
    iv = implied_volatility(price=price, spot=128.0, strike=160.0, years=1.94, rate=0.045, right="C")
    assert iv is not None
    assert abs(iv - 0.33) < 1e-5


def test_realized_vol_uses_log_returns_and_annualizes():
    closes = [100.0, 101.0, 99.0, 102.0]
    candles = [{"date": f"2026-01-0{i + 1}", "close": close} for i, close in enumerate(closes)]
    out = realized_vol(candles, 3)
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean = sum(returns) / len(returns)
    sample_var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    expected = math.sqrt(sample_var) * math.sqrt(252.0)
    assert len(out) == 1
    assert out[0]["date"] == "2026-01-04"
    assert abs(float(out[0]["rv"]) - expected) < 1e-12


def test_doubled_check_requires_current_at_least_two_times_entry():
    assert has_doubled(4.0, 8.0) is True
    assert has_doubled(4.0, 7.99) is False
    assert has_doubled(None, 8.0) is False
