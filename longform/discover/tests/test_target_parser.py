from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from target_parser import parse_target, resolve_price_target


def test_arrow_target_is_ticker_specific_for_aaoi():
    row = {
        "prediction": "AAOI should be valued around $162 per share and SIVE around $38.5 per share.",
        "target_value": "$AAOI $93 -> $162 (~$13B MC); $SIVE 7.7 -> 38.5 ($1.1B MC)",
        "target_metric": "revenue",
        "target_date": "2027-12-31",
    }
    target = parse_target(row, ticker="AAOI")
    assert target["basis"] == "price"
    assert target["entry_price_hint"] == 93
    assert target["target_price"] == 162
    assert target["target_currency"] == "USD"


def test_arrow_target_is_ticker_specific_for_sive_native_price():
    row = {
        "prediction": "AAOI should be valued around $162 per share and SIVE around $38.5 per share.",
        "target_value": "$AAOI $93 -> $162 (~$13B MC); $SIVE 7.7 -> 38.5 ($1.1B MC)",
        "target_metric": "revenue",
    }
    target = parse_target(row, ticker="SIVE")
    assert target["basis"] == "price"
    assert target["entry_price_hint"] == 7.7
    assert target["target_price"] == 38.5
    assert "target_currency" not in target


def test_downside_range_sets_threshold_to_less_severe_price():
    target = parse_target("HIMS, DUOL, and BMNR are likely to drop 50-70%+ in Q1 2026.")
    resolved = resolve_price_target(target, 100)
    assert resolved["basis"] == "percent_return"
    assert resolved["direction"] == "down"
    assert round(resolved["target_price_low"], 6) == 30
    assert round(resolved["target_price_high"], 6) == 50
    assert resolved["threshold_price"] == 50


def test_pure_outperform_stays_comparative():
    target = parse_target("AAOI is expected to outperform over the next year.")
    assert target["basis"] == "comparative"
    assert "return_pct" not in target
    assert "threshold_price" not in target


def test_benchmark_relative_requires_numeric_threshold():
    target = parse_target("AAOI should outperform SPY by 30% over the next year.")
    assert target["basis"] == "benchmark_relative"
    assert target["benchmark"] == "SPY"
    assert target["benchmark_relative_pct"] == 30


def test_market_cap_target_is_not_treated_as_share_price():
    target = parse_target("SIVE can hit a $10B+ market cap next year.")
    assert target["basis"] == "market_cap"
    assert target["market_cap"] == 10_000_000_000
    assert target["market_cap_currency"] == "USD"
    assert "target_price" not in target


def test_market_cap_floor_language_sets_floor_basis():
    target = parse_target("AAOI will look undervalued at a $6B valuation in one year.")
    assert target["basis"] == "market_cap_floor"
    assert target["market_cap"] == 6_000_000_000
    assert target["market_cap_currency"] == "USD"


def test_market_cap_ranges_populate_low_and_high():
    cases = [
        "SOI may become an $8B-$12B company within one year.",
        "SOI may become an $8B–$12B company within one year.",
        "SOI may become between $8B and $12B company within one year.",
        "SOI may become an $8 to $12B company within one year.",
        "MTSI market cap could reach $17-20B in a year and a half.",
    ]
    for text in cases:
        target = parse_target(text)
        assert target["basis"] == "market_cap"
        assert "market_cap" not in target
        assert target["market_cap_low"] < target["market_cap_high"]


def test_revenue_multiple_claim_does_not_become_price_multiple():
    target = parse_target("AAOI revenue will ramp 10x from optical transceivers in 2027.")
    assert target["basis"] == "revenue"
    assert "multiple" not in target
    assert "target_price" not in target


def test_share_price_floor_sets_valuation_floor():
    target = parse_target("AAOI should be worth $25+ within one year.")
    resolved = resolve_price_target(target, 10)
    assert target["basis"] == "valuation_floor"
    assert target["target_price"] == 25
    assert resolved["threshold_price"] == 25


def test_percent_upside_resolves_to_entry_based_price():
    target = parse_target("SOI could rise 200% over the next year.")
    resolved = resolve_price_target(target, 20)
    assert resolved["basis"] == "percent_return"
    assert resolved["return_pct"] == 200
    assert resolved["target_price"] == 60
    assert resolved["threshold_price"] == 60


def test_multiple_range_resolves_to_low_threshold():
    target = parse_target("SOI can rise 2-3x within the next year.")
    resolved = resolve_price_target(target, 12)
    assert resolved["basis"] == "multiple"
    assert resolved["multiple_low"] == 2
    assert resolved["multiple_high"] == 3
    assert resolved["target_price_low"] == 24
    assert resolved["target_price_high"] == 36
    assert resolved["threshold_price"] == 24


def test_derivative_prediction_does_not_become_underlying_price_progress():
    target = parse_target("EWY 2028 OTM LEAP calls may double again.")
    assert target["basis"] == "derivative"
    assert target["multiple"] == 2
    assert "threshold_price" not in target


def test_indirect_etf_pull_higher_stays_qualitative():
    row = {
        "prediction": "Samsung and SK Hynix are likely to double again within two years, which would pull EWY higher.",
        "target_value": "double again in the next two years",
        "target_metric": "price",
        "target_date": "2028-02-12",
    }
    target = parse_target(row, ticker="EWY")
    assert target["basis"] == "other"
    assert target["direction"] == "up"
    assert "multiple" not in target
    assert "threshold_price" not in target


def test_option_tweet_with_underlying_rally_uses_underlying_threshold():
    target = parse_target("XLU 2-year OTM calls should return 9-10x if XLU rallies 39% over the next year.")
    resolved = resolve_price_target(target, 50)
    assert target["basis"] == "percent_return"
    assert target["return_pct"] == 39
    assert resolved["threshold_price"] == 69.5
