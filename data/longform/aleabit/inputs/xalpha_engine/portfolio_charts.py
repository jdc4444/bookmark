"""Chart data for account portfolios.

Produces:
  - Per-ticker price series with tweet markers (first buy highlighted, subsequent
    calls + any sells flagged).
  - Aggregate equity curve: $100 start, walk forward, each new trade gets equal
    (or tier-weighted) allocation. Compare against SPY / QQQ.

Reuses portfolio.build_trades_for_account for the trade list.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from bisect import bisect_right

import pandas as pd
from dateutil import parser as dtparser

from fetch_ohlcv import fetch_klines, symbol_for
from portfolio import (
    build_trades_for_account, TIER_WEIGHT, CONVICTION_WEIGHT, _get_bench,
)

ROOT = Path(__file__).parent
DATA = ROOT / "data"


def _as_utc(d):
    if isinstance(d, str):
        d = dtparser.parse(d)
    if d.tzinfo is None:
        return pd.Timestamp(d, tz="UTC")
    return pd.Timestamp(d).tz_convert("UTC")


def _series_from_df(df: pd.DataFrame, start: datetime, end: datetime) -> list[dict]:
    if df is None or df.empty:
        return []
    s = _as_utc(start); e = _as_utc(end)
    sub = df[(df["open_time"] >= s) & (df["open_time"] <= e)]
    return [
        {"date": t.strftime("%Y-%m-%d"), "close": round(float(c), 4)}
        for t, c in zip(sub["open_time"], sub["close"])
    ]


_HORIZON_HALFLIFE_DAYS = {
    "intraday":   7,
    "swing":     30,
    "multi_week": 90,
    "unknown":  180,
    None:       180,
    "":         180,
    "long_term": 540,
}


def _weight_for(trade: dict, curve_date: str | None = None) -> float:
    """Composite weight for a trade entry in the equity curve.

    Multiplies four signal-quality factors:
      - tier × conviction (base): S=3, A=2, B=1, high=3, med=2, low=1
      - position_share: 1/n_tickers_in_tweet — dilutes list-spam (Case 1+3)
      - mention_share: account-level concentration on this ticker — kills
        spurious credit for a passing 1-of-100 mention (Case 3)
      - recency_decay: exp(-(days_since_last_mention)/half_life), where
        half_life depends on the author-stated `horizon`:
          intraday=7d, swing=30d, multi_week=90d, unknown=180d, long_term=540d
        So a "long_term $X" position with no mention for 6 months barely
        decays (~80% retained), while a "swing $X" position decays to
        essentially zero after a month of silence. Catches Case 2 with
        respect for the author's stated holding period.

    `curve_date` is the YYYY-MM-DD of the equity-curve point; only the
    recency_decay term uses it. None disables decay (legacy behaviour).
    """
    tier = trade.get("tier")
    conv = trade.get("conviction") or "med"
    base = TIER_WEIGHT[tier] if tier in TIER_WEIGHT else CONVICTION_WEIGHT.get(conv, 1.0)
    share = trade.get("position_share")
    if share is None:
        share = 1.0
    mshare = trade.get("mention_share")
    if mshare is None or mshare <= 0:
        mshare = 1.0
    decay = 1.0
    if curve_date and trade.get("last_mention_date"):
        try:
            last = trade["last_mention_date"][:10]
            cdate = curve_date[:10]
            d_delta = (datetime.strptime(cdate, "%Y-%m-%d")
                       - datetime.strptime(last, "%Y-%m-%d")).days
            if d_delta > 0:
                hl = _HORIZON_HALFLIFE_DAYS.get(
                    (trade.get("horizon") or "unknown").lower(),
                    180,
                )
                import math as _m
                decay = _m.exp(-d_delta / hl)
        except Exception:
            pass
    return base * share * mshare * decay


def _build_pit_indexes(handle: str, signals: list[dict] | None = None) -> dict:
    """Build chronological indexes used by the point-in-time weight builder.

    Returns:
      tier_history[(ticker, direction)]: sorted [(date_iso, tier), ...]
        Lets us look up "highest tier achieved by date X" in O(log N).
      mention_history[ticker]: sorted [date_iso, ...]
        Each entry is one tweet on this account that named the ticker.
      total_mention_history: sorted [date_iso, ...]
        Each entry is one tweet on this account that named ANY ticker.

    All indexes derive from calls_llm.json + signals_llm.json + tweets.json.
    Read-only — no caching, build fresh per request (~ms-scale even for
    large accounts).
    """
    acct_dir = DATA / handle
    calls_path = acct_dir / "calls_llm.json"
    tweets_path = acct_dir / "tweets.json"
    signals_path = acct_dir / "signals_llm.json"
    calls = json.loads(calls_path.read_text()) if calls_path.exists() else []
    tweets_idx = {}
    if tweets_path.exists():
        for tw in json.loads(tweets_path.read_text()):
            tweets_idx[tw["id"]] = tw.get("created_at")
    signals = signals if signals is not None else (
        json.loads(signals_path.read_text()) if signals_path.exists() else []
    )

    # Tier history: every (ticker, direction) gets the chronological sequence
    # of tier-crossings. signals_llm already records each crossing as its own
    # row with first_signal_date, so we just group + sort.
    tier_history: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for s in signals:
        if not s.get("tier"):
            continue
        key = (s["ticker"], s.get("direction"))
        tier_history.setdefault(key, []).append(
            (s["first_signal_date"], s["tier"])
        )
    for k in tier_history:
        tier_history[k].sort()

    # Mention history: per ticker, the sorted list of tweet timestamps that
    # mentioned it. Account-total = same, deduped on tweet_id (so a tweet
    # naming five tickers counts once toward total but five times per-ticker).
    mention_history: dict[str, list[str]] = {}
    total_seen_tweets: set[str] = set()
    total_mention_history: list[str] = []
    for c in calls:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("tweet_id") or "")
        ts = tweets_idx.get(cid) or c.get("created_at")
        if not ts:
            continue
        names: set[str] = set()
        for ent in (c.get("tickers") or []):
            if isinstance(ent, dict):
                tk = (ent.get("ticker") or "").strip().lstrip("$").upper()
                if tk:
                    names.add(tk)
        pt = c.get("primary_ticker")
        if isinstance(pt, str) and pt.strip():
            names.add(pt.strip().lstrip("$").upper())
        if not names:
            continue
        for tk in names:
            mention_history.setdefault(tk, []).append(ts)
        if cid and cid not in total_seen_tweets:
            total_seen_tweets.add(cid)
            total_mention_history.append(ts)
    for tk in mention_history:
        mention_history[tk].sort()
    total_mention_history.sort()

    # Median inter-mention gap per ticker — drives the cadence-aware decay
    # bound. If the trader's typical re-mention gap is N days, the basket
    # weight stays at full strength until 2×N silent days have passed; only
    # then does exponential decay start. Falls back to None for tickers with
    # <3 mentions (not enough data to characterize cadence — the decay code
    # uses the horizon's τ as a conservative default).
    median_gap_per_ticker: dict[str, float] = {}
    for tk, dates in mention_history.items():
        if len(dates) < 3:
            continue
        # Convert to ordinal days; gaps in days
        try:
            parsed = [datetime.fromisoformat(d.replace("Z", "+00:00")) for d in dates]
        except Exception:
            continue
        parsed.sort()
        gaps = [(parsed[i + 1] - parsed[i]).total_seconds() / 86400.0
                for i in range(len(parsed) - 1)]
        gaps.sort()
        median_gap_per_ticker[tk] = gaps[len(gaps) // 2]

    return {
        "tier_history": tier_history,
        "mention_history": mention_history,
        "total_mention_history": total_mention_history,
        "median_gap": median_gap_per_ticker,
    }


_TIER_PRIORITY = {"S": 0, "A": 1, "B": 2}


def _tier_at(ticker: str, direction: str, as_of: str,
             tier_history: dict) -> str | None:
    """Highest tier reached for (ticker, direction) by `as_of`."""
    seq = tier_history.get((ticker, direction)) or []
    best: str | None = None
    best_pri = 999
    for d, t in seq:
        if d > as_of:
            break
        pri = _TIER_PRIORITY.get(t, 9)
        if pri < best_pri:
            best, best_pri = t, pri
    return best


def _count_le(sorted_list: list[str], as_of: str) -> int:
    """How many entries in a sorted list are ≤ as_of. O(log N)."""
    import bisect
    return bisect.bisect_right(sorted_list, as_of + "￿")


def _last_le(sorted_list: list[str], as_of: str) -> str | None:
    """Rightmost entry ≤ as_of, or None."""
    import bisect
    i = bisect.bisect_right(sorted_list, as_of + "￿")
    return sorted_list[i - 1] if i > 0 else None


def _weight_for_pit(trade: dict, curve_date: str, idx: dict) -> float:
    """Point-in-time weight (v2 formula). All factors evaluated as of
    `curve_date` rather than baked-in at build time.

    Formula:
      base   = 0.5 × tier_weight  +  0.5 × conviction_weight
                 (untiered → tier=0; the 0.5×conv carries alone)
      shares = 0.3 × position_share  +  0.7 × mention_share
      decay  = 1.0 if days_since_last_mention <= max(τ, 2×median_gap)
               else exp(-(days_over_bound) / τ)
      weight = base × shares × decay

    A separate basket-level pass (in _twr_curve_pit) drops positions that
    would weigh <1% of the basket and renormalizes survivors to 100%."""
    ticker = trade["ticker"]
    direction = trade.get("direction", "long")
    conv = (trade.get("conviction") or "med").lower()

    # Base: 50/50 blend of tier (PIT) and conviction. Untiered → tier_weight=0,
    # so a high-conv first-time mention gets 0.5 × 3 = 1.5 (penalized vs a
    # tiered B+high which gets (1+3)/2 = 2.0). Tiering "earns" full weight.
    tier = _tier_at(ticker, direction, curve_date, idx["tier_history"])
    tier_w = TIER_WEIGHT.get(tier, 0.0) if tier else 0.0
    conv_w = CONVICTION_WEIGHT.get(conv, 1.0)
    base = 0.5 * tier_w + 0.5 * conv_w

    # Shares: 30/70 blend favoring mention. Mention dominates because the
    # account-level concentration ("does this trader actually care?") is
    # the stronger signal than per-tweet purity. Position share still does
    # work to suppress 50-stock list-spam tweets.
    pshare = trade.get("position_share")
    if pshare is None:
        pshare = 1.0
    nm = _count_le(idx["mention_history"].get(ticker, []), curve_date)
    tm = _count_le(idx["total_mention_history"], curve_date)
    mshare = (nm / tm) if tm > 0 else 0.0
    shares = 0.3 * pshare + 0.7 * mshare

    # Cadence-aware decay. We only start eroding the position once the
    # trader has been silent longer than 2× their typical inter-mention
    # gap on this ticker (or the horizon's τ, whichever is larger). Inside
    # the bound: decay=1.0. Past the bound: exponential w/ horizon τ.
    horizon = (trade.get("horizon") or "unknown").lower()
    hl = _HORIZON_HALFLIFE_DAYS.get(horizon, 180)
    median_gap = idx.get("median_gap", {}).get(ticker)
    bound = max(hl, 2.0 * median_gap) if median_gap is not None else float(hl)
    last = _last_le(idx["mention_history"].get(ticker, []), curve_date)
    decay = 1.0
    if last:
        try:
            d_delta = (datetime.strptime(curve_date[:10], "%Y-%m-%d")
                       - datetime.strptime(last[:10], "%Y-%m-%d")).days
            if d_delta > bound:
                import math as _m
                decay = _m.exp(-(d_delta - bound) / hl)
        except Exception:
            pass

    return base * shares * decay


def _basket_filter_renorm(weights: dict, threshold: float = 0.01) -> dict:
    """Single-pass basket cleanup: normalize → drop <threshold → renormalize.

    1. Compute each weight as a fraction of the total.
    2. Drop positions whose share is below `threshold` (default 1%).
    3. Renormalize the survivors so they sum to 1.0.

    Survivors after the renormalization may end up below the threshold
    (because removing the tail shifted percentages); we accept that single
    pass to avoid cascading drops."""
    total = sum(weights.values())
    if total <= 0:
        return weights
    pct = {k: v / total for k, v in weights.items()}
    survivors = {k: v for k, v in pct.items() if v >= threshold}
    surv_total = sum(survivors.values())
    if surv_total <= 0:
        # Edge case: no position made the cut. Keep the largest one so the
        # curve doesn't flatline to None.
        if not pct:
            return {}
        k = max(pct, key=pct.get)
        return {k: 1.0}
    return {k: v / surv_total for k, v in survivors.items()}


def _date10(v) -> str:
    return str(v or "")[:10]


def _fmt_key_num(v) -> str:
    if v is None:
        return ""
    try:
        s = f"{float(v):.4f}".rstrip("0").rstrip(".")
        return "0" if s == "-0" else s
    except Exception:
        return str(v)


def _marker_key(
    ticker: str | None,
    call_date: str | None,
    entry_date: str | None,
    direction: str | None,
    entry_price,
    conviction: str | None,
) -> str:
    return "|".join([
        (ticker or "").strip().lstrip("$").upper(),
        _date10(call_date),
        _date10(entry_date),
        (direction or "").lower(),
        _fmt_key_num(entry_price),
        (conviction or "").lower(),
    ])


def _cached_curve_dates(chart: dict) -> list[str]:
    agg = chart.get("aggregate") or {}
    candidates = [
        ((agg.get("curves") or {}).get("twr") or []),
        agg.get("tier_weight") or [],
        agg.get("spy") or [],
        agg.get("qqq") or [],
    ]
    for rows in candidates:
        dates = [str(r.get("date")) for r in rows if isinstance(r, dict) and r.get("date")]
        if dates:
            return sorted(set(dates))
    return []


def _cached_price_maps(chart: dict) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for row in chart.get("tickers") or []:
        ticker = (row.get("ticker") or "").strip().lstrip("$").upper()
        if not ticker:
            continue
        m: dict[str, float] = {}
        for p in row.get("prices") or []:
            d = p.get("date")
            if not d:
                continue
            try:
                m[str(d)] = float(p.get("close"))
            except Exception:
                continue
        if m:
            out[ticker] = m
    return out


def _load_portfolio_result_for_overlay(handle: str) -> dict:
    acct_dir = DATA / handle
    portfolio_path = acct_dir / "portfolio.json"
    if portfolio_path.exists():
        try:
            return json.loads(portfolio_path.read_text())
        except Exception:
            pass
    return build_trades_for_account(handle)


def _build_cached_price_lookup(price_maps: dict[str, dict[str, float]]):
    price_dates = {tk: sorted(m) for tk, m in price_maps.items()}

    def lookup(ticker: str, date: str) -> float | None:
        tk = (ticker or "").strip().lstrip("$").upper()
        dates = price_dates.get(tk) or []
        if not dates:
            return None
        i = bisect_right(dates, date) - 1
        if i < 0:
            return None
        return price_maps[tk].get(dates[i])

    return lookup


def _ret_mult_from_cached_prices(
    trade: dict,
    date: str,
    final_date: str,
    price_lookup,
) -> float | None:
    entry_date = trade.get("entry_date")
    if not entry_date or entry_date > date:
        return None
    entry_px = trade.get("entry_price")
    if not entry_px:
        return None

    ex_date = trade.get("exit_date")
    ex_px = trade.get("exit_price")
    if ex_date and ex_px is not None and ex_date < final_date and ex_date <= date:
        px = ex_px
    else:
        px = price_lookup(trade.get("ticker", ""), date)
        if px is None:
            return None

    sign = 1 if trade.get("direction") == "long" else -1
    rm = 1 + sign * ((float(px) - float(entry_px)) / float(entry_px))
    return max(0.0, rm) if sign == -1 else rm


def _twr_curve_from_cached_prices(
    trades_sorted: list[dict],
    sorted_dates: list[str],
    price_maps: dict[str, dict[str, float]],
    weight_fn,
    basket_filter=None,
) -> list[dict]:
    if not sorted_dates:
        return []
    price_lookup = _build_cached_price_lookup(price_maps)
    final_date = sorted_dates[-1]
    out = []
    value = 100.0
    prev_rm: dict[str, float] = {}

    for date in sorted_dates:
        today_rm: dict[str, float] = {}
        today_w: dict[str, float] = {}
        for i, trade in enumerate(trades_sorted):
            rm = _ret_mult_from_cached_prices(trade, date, final_date, price_lookup)
            if rm is None:
                continue
            key = str(i)
            today_rm[key] = rm
            today_w[key] = weight_fn(trade, date)

        if basket_filter is not None:
            today_w = basket_filter(today_w)
            today_rm = {k: today_rm[k] for k in today_w if k in today_rm}

        if prev_rm:
            shared = set(today_rm) & set(prev_rm)
            pv = 0.0
            tw = 0.0
            for k in shared:
                if prev_rm[k] <= 0:
                    continue
                r = (today_rm[k] / prev_rm[k]) - 1
                w = today_w[k]
                pv += w * r
                tw += w
            if tw > 0:
                value *= 1 + pv / tw
        out.append({"date": date, "value": round(value, 4)})
        prev_rm = today_rm

    return out


def _pit_marker_overlay(trade: dict, as_of: str, idx: dict) -> dict:
    ticker = (trade.get("ticker") or "").strip().lstrip("$").upper()
    direction = trade.get("direction", "long")
    nm = _count_le(idx["mention_history"].get(ticker, []), as_of)
    tm = _count_le(idx["total_mention_history"], as_of)
    median_gap = idx.get("median_gap", {}).get(ticker)
    return {
        "key": _marker_key(
            ticker,
            trade.get("call_date"),
            trade.get("entry_date"),
            direction,
            trade.get("entry_price"),
            trade.get("conviction"),
        ),
        "ticker": ticker,
        "date": _date10(trade.get("call_date")),
        "entry_date": _date10(trade.get("entry_date")),
        "direction": direction,
        "entry_price": trade.get("entry_price"),
        "conviction": trade.get("conviction"),
        "tier": _tier_at(ticker, direction, as_of, idx["tier_history"]),
        "mention_share": (nm / tm) if tm > 0 else 0.0,
        "last_mention_date": _last_le(idx["mention_history"].get(ticker, []), as_of),
        "median_gap_days": round(median_gap, 2) if median_gap is not None else None,
        "raw_weight": round(_weight_for_pit(trade, as_of, idx), 8),
    }


def build_reweight_overlay_for_account(
    handle: str,
    chart: dict | None = None,
    portfolio_result: dict | None = None,
) -> dict:
    """Fast PIT reweight for the account page.

    Uses the already-materialized portfolio_charts.json price series and
    portfolio.json trades. This avoids the slow full chart rebuild/yfinance path
    while still recomputing the aggregate TWR curve with PIT weights.
    """
    acct_dir = DATA / handle
    if chart is None:
        chart_path = acct_dir / "portfolio_charts.json"
        if not chart_path.exists():
            raise FileNotFoundError(f"missing chart cache for @{handle}")
        chart = json.loads(chart_path.read_text())
    portfolio_result = portfolio_result or _load_portfolio_result_for_overlay(handle)

    chart_marker_keys: set[str] = set()
    marker_trades: list[dict] = []
    for ticker_row in chart.get("tickers") or []:
        ticker = (ticker_row.get("ticker") or "").strip().lstrip("$").upper()
        for marker in ticker_row.get("markers") or []:
            key = _marker_key(
                ticker,
                marker.get("date"),
                marker.get("entry_date"),
                marker.get("direction"),
                marker.get("entry_price"),
                marker.get("conviction"),
            )
            chart_marker_keys.add(key)
            marker_trades.append({
                "status": "ok",
                "ticker": ticker,
                "direction": marker.get("direction"),
                "conviction": marker.get("conviction"),
                "call_date": marker.get("date"),
                "entry_date": marker.get("entry_date"),
                "entry_price": marker.get("entry_price"),
                "exit_date": None,
                "exit_price": None,
                "position_share": marker.get("position_share"),
                "horizon": marker.get("horizon"),
                "_marker_key": key,
            })

    trades = [
        t for t in (portfolio_result.get("trades") or [])
        if t.get("status") == "ok" and t.get("entry_date") and t.get("entry_price")
    ]
    if chart_marker_keys:
        keyed_trades = []
        seen_trade_keys = set()
        for trade in trades:
            key = _marker_key(
                trade.get("ticker"),
                trade.get("call_date"),
                trade.get("entry_date"),
                trade.get("direction"),
                trade.get("entry_price"),
                trade.get("conviction"),
            )
            if key not in chart_marker_keys:
                continue
            trade["_marker_key"] = key
            keyed_trades.append(trade)
            seen_trade_keys.add(key)
        for marker_trade in marker_trades:
            if marker_trade["_marker_key"] not in seen_trade_keys:
                keyed_trades.append(marker_trade)
        trades = keyed_trades
    if not trades:
        return {
            "ok": True,
            "handle": handle,
            "as_of": None,
            "aggregate": {"curves": {"twr": []}},
            "marker_overlays": [],
            "trades": 0,
        }

    sorted_dates = _cached_curve_dates(chart)
    if not sorted_dates:
        return {
            "ok": False,
            "handle": handle,
            "as_of": None,
            "aggregate": {"curves": {"twr": []}},
            "marker_overlays": [],
            "trades": len(trades),
            "error": "chart cache has no aggregate dates",
        }

    trades_sorted = sorted(trades, key=lambda t: t["entry_date"])
    price_maps = _cached_price_maps(chart)
    pit_idx = _build_pit_indexes(handle)
    twr_curve = _twr_curve_from_cached_prices(
        trades_sorted,
        sorted_dates,
        price_maps,
        lambda trade, date: _weight_for_pit(trade, date, pit_idx),
        basket_filter=_basket_filter_renorm,
    )
    as_of = sorted_dates[-1]
    return {
        "ok": True,
        "handle": handle,
        "as_of": as_of,
        "aggregate": {"curves": {"twr": twr_curve}},
        "marker_overlays": [
            _pit_marker_overlay(t, as_of, pit_idx)
            for t in trades_sorted
        ],
        "trades": len(trades_sorted),
    }


def apply_reweight_overlay_to_chart(chart: dict, overlay: dict) -> dict:
    import copy

    out = copy.deepcopy(chart)
    agg = out.setdefault("aggregate", {})
    curves = agg.setdefault("curves", {})
    twr = ((overlay.get("aggregate") or {}).get("curves") or {}).get("twr")
    if twr is not None:
        curves["twr"] = twr

    by_key = {
        o.get("key"): o
        for o in overlay.get("marker_overlays") or []
        if o.get("key")
    }
    for ticker_row in out.get("tickers") or []:
        ticker = ticker_row.get("ticker")
        for marker in ticker_row.get("markers") or []:
            key = _marker_key(
                ticker,
                marker.get("date"),
                marker.get("entry_date"),
                marker.get("direction"),
                marker.get("entry_price"),
                marker.get("conviction"),
            )
            patch = by_key.get(key)
            if not patch:
                continue
            for name in ("tier", "mention_share", "last_mention_date", "median_gap_days"):
                marker[name] = patch.get(name)
    out["reweight"] = {
        "as_of": overlay.get("as_of"),
        "trades": overlay.get("trades"),
        "mode": "pit_fast_overlay_v1",
    }
    return out


def build_charts_for_account(
    handle: str,
    end_date: datetime | None = None,
    portfolio_result: dict | None = None,
    point_in_time: bool = False,
) -> dict:
    end_date = end_date or datetime.now(timezone.utc)
    result = (
        portfolio_result
        if portfolio_result is not None
        else build_trades_for_account(handle, end_date)
    )
    trades = [t for t in result.get("trades", []) if t.get("status") == "ok"]
    if not trades:
        return {"handle": handle, "aggregate": None, "tickers": []}

    # Load tweet text so markers can show the actual tweet on hover.
    tweets_path = DATA / handle / "tweets.json"
    tweet_text_by_id: dict[str, str] = {}
    if tweets_path.exists():
        for t in json.loads(tweets_path.read_text()):
            tweet_text_by_id[t["id"]] = (t.get("text") or "")

    # Dates
    dates_parsed = [dtparser.parse(t["entry_date"]) for t in trades]
    start = min(dates_parsed)
    span_start = start - timedelta(days=5)
    span_end = end_date + timedelta(days=1)

    # Benchmarks
    spy = _get_bench("SPY", span_start, span_end)
    qqq = _get_bench("QQQ", span_start, span_end)

    # Per-ticker: collect all trade records + load OHLCV
    per_ticker: dict[str, dict] = defaultdict(
        lambda: {"ticker": "", "markers": [], "prices": [], "first_call_date": None}
    )
    ticker_dfs: dict[str, pd.DataFrame | None] = {}
    for t in trades:
        tk = t["ticker"]
        per_ticker[tk]["ticker"] = tk
        if tk not in ticker_dfs:
            sym = symbol_for(tk)
            try:
                ticker_dfs[tk] = fetch_klines(
                    sym, span_start.replace(tzinfo=None), span_end.replace(tzinfo=None)
                ) if sym else None
            except Exception:
                ticker_dfs[tk] = None
        per_ticker[tk]["markers"].append({
            "date": t["call_date"][:10],
            "entry_date": t["entry_date"],
            "entry_price": t["entry_price"],
            "direction": t["direction"],
            "conviction": t["conviction"],
            "tier": t.get("tier"),
            "style": t.get("style"),
            "reasoning": t.get("reasoning"),
            "stock_return": t.get("stock_return"),
            "url": t.get("url"),
            "text": tweet_text_by_id.get(t["tweet_id"], "")[:500],
            # Weight inputs the frontend uses to compute a per-position % of
            # the basket on hover. Mirrors what `_weight_for()` consumes.
            "position_share": t.get("position_share"),
            "mention_share": t.get("mention_share"),
            "last_mention_date": t.get("last_mention_date"),
            "horizon": t.get("horizon"),
        })
    # Sort markers + compute prices + mark first_call
    for tk, pt in per_ticker.items():
        pt["markers"].sort(key=lambda m: m["date"])
        if pt["markers"]:
            pt["first_call_date"] = pt["markers"][0]["date"]
            pt["markers"][0]["is_first"] = True
        df = ticker_dfs.get(tk)
        pt["prices"] = _series_from_df(df, span_start, span_end) if df is not None else []

    # Aggregate equity curve: walk-forward, each new trade adds a "position" of
    # size weight / total_alloc_so_far (so cumulative trade count doesn't dilute)
    # Simpler: every trade gets fixed $1 of capital (equal) or $weight (tier).
    # Portfolio value = sum(position_value) over all positions. Index to 100.
    trades_sorted = sorted(trades, key=lambda t: t["entry_date"])
    all_dates: set[str] = set()
    for df in ticker_dfs.values():
        if df is not None:
            for d in df["open_time"]:
                all_dates.add(d.strftime("%Y-%m-%d"))
    if spy is not None:
        for d in spy["open_time"]:
            all_dates.add(d.strftime("%Y-%m-%d"))
    sorted_dates = sorted(d for d in all_dates if d >= start.strftime("%Y-%m-%d"))

    # Restrict the equity-curve x-axis to trading days only (dates the SPY
    # benchmark actually traded). Without this, the engine emits portfolio
    # values for weekends/holidays — the per-trade prices roll forward via
    # _price_at_or_before() so values are filled — but SPY/QQQ have no data
    # on those dates, which makes the alpha curve look like a zigzag with
    # missing chunks. Aligning everything to the SPY calendar gives all four
    # series the same timeline.
    if spy is not None and len(spy) > 0:
        spy_trading_days = {d.strftime("%Y-%m-%d") for d in spy["open_time"]}
        sorted_dates = [d for d in sorted_dates if d in spy_trading_days]

    # Price lookup helper: for each ticker, map date → close. Drop NaN closes
    # (foreign listings return NaN on US-only trading days — calendar mismatch).
    import math as _math
    def _mk_map(df):
        m = {}
        if df is None: return m
        for t, c in zip(df["open_time"], df["close"]):
            try: v = float(c)
            except Exception: continue
            if not _math.isfinite(v): continue
            m[t.strftime("%Y-%m-%d")] = v
        return m

    price_map: dict[str, dict[str, float]] = {tk: _mk_map(df) for tk, df in ticker_dfs.items()}
    spy_map = _mk_map(spy)
    qqq_map = _mk_map(qqq)

    # Build a fast date→index lookup once (binary-search-friendly)
    sorted_dates_arr = sorted_dates

    def _date_index(date: str) -> int:
        # Index of `date` in sorted_dates_arr. Falls back to insertion point.
        # We don't need bisect — sorted_dates is small (<=1000 entries).
        for i, d in enumerate(sorted_dates_arr):
            if d >= date:
                return i
        return len(sorted_dates_arr)

    def _price_at_or_before(pmap: dict, date: str) -> float | None:
        """Last known close on or before `date`."""
        if date in pmap:
            return pmap[date]
        for i in range(len(sorted_dates_arr) - 1, -1, -1):
            if sorted_dates_arr[i] > date:
                continue
            v = pmap.get(sorted_dates_arr[i])
            if v is not None:
                return v
        return None

    def equity_curve(weight_fn, exit_after_days: int | None = None,
                     margin_call_shorts: bool = True,
                     date_aware_weight: bool = False):
        """Walk-forward mark-to-market equity curve.

        Args:
          weight_fn:       trade → weight (equal=1.0 or tier-weighted).
          exit_after_days: force-close each position this many trading days
                           after entry (holding-period variant). None = rely
                           only on explicit exit events from the classifier.
          margin_call_shorts: cap short ret_mult at 0 (simulates 100% loss
                           margin-call behaviour — otherwise a short of a
                           3x stock contributes -200% to the curve).

        Position-aware: if a Trade has an `exit_date` before today (the
        classifier saw an explicit "sold" / "exit" event), we freeze that
        position's return at its exit_price from then on. Open positions
        continue to mark against the current date's close.

        Returns a list of {date, value} where value is indexed to 100 at
        the date the first position enters."""
        series = []
        for date in sorted_dates_arr:
            pv = 0.0
            tw = 0.0
            for t in trades_sorted:
                entry_date = t["entry_date"]
                if entry_date > date:
                    continue

                # Explicit exit event from the classifier. We stored the
                # exit_date on the Trade — once the walk crosses it, freeze
                # the position at its exit_price (no more mark-to-market).
                t_exit_date = t.get("exit_date")
                t_exit_price = t.get("exit_price")
                explicit_exit = (
                    t_exit_date and t_exit_price is not None
                    and t_exit_date < sorted_dates_arr[-1]  # not a still-open placeholder
                    and t_exit_date <= date
                )
                if explicit_exit:
                    entry_px = t["entry_price"]
                    if not entry_px: continue
                    sign = 1 if t["direction"] == "long" else -1
                    ret_mult = 1 + sign * ((t_exit_price - entry_px) / entry_px)
                    if margin_call_shorts and sign == -1:
                        ret_mult = max(0.0, ret_mult)
                    w = weight_fn(t, date) if date_aware_weight else weight_fn(t)
                    pv += w * ret_mult
                    tw += w
                    continue

                if exit_after_days is not None:
                    i_entry = _date_index(entry_date)
                    i_now = _date_index(date)
                    if i_now - i_entry > exit_after_days:
                        # Forced-close variant (time-bucketed holding periods).
                        i_exit = min(i_entry + exit_after_days, len(sorted_dates_arr) - 1)
                        forced_exit_date = sorted_dates_arr[i_exit]
                        pmap = price_map.get(t["ticker"], {})
                        forced_exit_price = _price_at_or_before(pmap, forced_exit_date)
                        if forced_exit_price is None: continue
                        entry_px = t["entry_price"]
                        if not entry_px: continue
                        sign = 1 if t["direction"] == "long" else -1
                        ret_mult = 1 + sign * ((forced_exit_price - entry_px) / entry_px)
                        if margin_call_shorts and sign == -1:
                            ret_mult = max(0.0, ret_mult)
                        w = weight_fn(t, date) if date_aware_weight else weight_fn(t)
                        pv += w * ret_mult
                        tw += w
                        continue
                # Open position — mark to current date
                pmap = price_map.get(t["ticker"], {})
                price = _price_at_or_before(pmap, date)
                if price is None:
                    continue
                entry_px = t["entry_price"]
                if not entry_px: continue
                sign = 1 if t["direction"] == "long" else -1
                ret_mult = 1 + sign * ((price - entry_px) / entry_px)
                if margin_call_shorts and sign == -1:
                    ret_mult = max(0.0, ret_mult)
                w = weight_fn(t, date) if date_aware_weight else weight_fn(t)
                pv += w * ret_mult
                tw += w
            value = (pv / tw) * 100 if tw > 0 else None
            series.append({"date": date, "value": round(value, 4) if value is not None else None})
        return series

    # Legacy equity curves — kept so older clients / cached snapshots still
    # render. The new curves below are the canonical ones (TWR / MWR / hold)
    # and the frontend toggles between them.
    equal_curve = equity_curve(lambda t: 1.0)
    tier_curve  = equity_curve(_weight_for, date_aware_weight=True)

    # ── New curves (toggleable from frontend) ──────────────────────────────
    # A — TWR (time-weighted return). Compounds the basket's day-over-day
    #     return, weighted by stated conviction. New trades enter with 0%
    #     contribution on day 1 (no spike), then participate from day 2.
    #     Always 100% invested in the live basket.
    # B — MWR-style cash-budget portfolio. $100 starting cash, 5% of starting
    #     capital deployed per new signal. Positions return to cash on exit.
    #     If cash runs out, new trades are skipped. Curve = cash + Σ(open
    #     positions × today's price). Shown in dollars.
    # C — Buy-and-hold, equal-weight across all calls. Each trade gets $1 at
    #     entry, frozen at exit_price after explicit exit, otherwise marked
    #     to today's price. Curve = (Σ position values) / N_entered × 100,
    #     so a flat 100 means the average call has gone nowhere.

    def _live_at(t: dict, date: str) -> tuple[bool, str | None]:
        """(is_live_at_close_of_date, frozen_price). Frozen price returned for
        trades whose explicit exit has occurred — those positions stop marking
        to market and contribute their exit_price thereafter."""
        if t["entry_date"] > date:
            return False, None
        ex_date = t.get("exit_date")
        ex_px = t.get("exit_price")
        if (ex_date and ex_px is not None
                and ex_date < sorted_dates_arr[-1]
                and ex_date <= date):
            return True, "EXIT"
        return True, None

    def _ret_mult(t: dict, date: str) -> float | None:
        """Position's return multiplier (1.0 = breakeven, 1.2 = +20%)."""
        live, marker = _live_at(t, date)
        if not live:
            return None
        entry_px = t["entry_price"]
        if not entry_px:
            return None
        if marker == "EXIT":
            px = t["exit_price"]
        else:
            pmap = price_map.get(t["ticker"], {})
            px = _price_at_or_before(pmap, date)
            if px is None:
                return None
        sign = 1 if t["direction"] == "long" else -1
        rm = 1 + sign * ((px - entry_px) / entry_px)
        return max(0.0, rm) if sign == -1 else rm

    def _twr_curve(weight_fn, date_aware: bool, basket_filter=None) -> list[dict]:
        """Time-weighted return: V_t = V_{t-1} × (1 + r_t), where r_t is the
        weighted-average day-over-day return across positions live on BOTH
        days. Positions entering today contribute nothing to today's return.

        `basket_filter`, if provided, runs on the today_w dict each day to
        post-process basket weights (e.g. drop <1% positions and renormalize
        survivors). PIT v2 mode passes _basket_filter_renorm here."""
        out = []
        value = 100.0
        prev_date: str | None = None
        prev_rm: dict[str, float] = {}
        for date in sorted_dates_arr:
            # Today's return multipliers for every live position
            today_rm: dict[str, float] = {}
            today_w: dict[str, float] = {}
            for i, t in enumerate(trades_sorted):
                rm = _ret_mult(t, date)
                if rm is None:
                    continue
                key = f"{i}"
                today_rm[key] = rm
                today_w[key] = weight_fn(t, date) if date_aware else weight_fn(t)

            # Optional basket-level post-process: drop tail + renormalize.
            # When applied, today_w sums to 1.0 across the survivors; we
            # also prune today_rm so the shared-set logic below stays
            # consistent (a position that didn't survive shouldn't count).
            if basket_filter is not None:
                today_w = basket_filter(today_w)
                today_rm = {k: today_rm[k] for k in today_w if k in today_rm}

            # Compute the day's basket return from positions live on BOTH days
            if prev_date is not None and prev_rm:
                shared = set(today_rm) & set(prev_rm)
                pv, tw = 0.0, 0.0
                for k in shared:
                    if prev_rm[k] <= 0:
                        continue  # avoid div-by-zero on capped shorts
                    r = (today_rm[k] / prev_rm[k]) - 1
                    w = today_w[k]
                    pv += w * r
                    tw += w
                if tw > 0:
                    value = value * (1 + pv / tw)
            out.append({"date": date, "value": round(value, 4)})
            prev_date = date
            prev_rm = today_rm
        return out

    def _mwr_cash_budget_curve(starting_cash: float = 100.0,
                               per_trade_pct: float = 0.05) -> list[dict]:
        """Money-weighted: deploy a fixed % of starting capital into each new
        trade. Cash flows back on explicit exit. Skip trades if cash empty."""
        per_trade = starting_cash * per_trade_pct
        cash = starting_cash
        # position state: trade_idx → {"shares": ..., "entry_px": ..., "sign": ..., "open": True}
        positions: dict[int, dict] = {}
        # Trades sorted by entry_date already
        out = []
        next_to_open = 0
        for date in sorted_dates_arr:
            # Open any trades that entered on/before today
            while (next_to_open < len(trades_sorted)
                   and trades_sorted[next_to_open]["entry_date"] <= date):
                t = trades_sorted[next_to_open]
                idx = next_to_open
                next_to_open += 1
                entry_px = t["entry_price"]
                if not entry_px or cash < 0.01:
                    continue
                alloc = min(per_trade, cash)
                shares = alloc / entry_px
                cash -= alloc
                positions[idx] = {
                    "shares": shares,
                    "entry_px": entry_px,
                    "sign": 1 if t["direction"] == "long" else -1,
                    "alloc": alloc,
                    "open": True,
                }

            # Process explicit exits dated on or before today
            for idx, p in positions.items():
                if not p["open"]:
                    continue
                t = trades_sorted[idx]
                ex_date, ex_px = t.get("exit_date"), t.get("exit_price")
                if (ex_date and ex_px is not None
                        and ex_date < sorted_dates_arr[-1]
                        and ex_date <= date):
                    # Realize P/L back to cash
                    proceeds = p["alloc"] * (1 + p["sign"] * ((ex_px - p["entry_px"]) / p["entry_px"]))
                    proceeds = max(0.0, proceeds)  # margin-cap shorts
                    cash += proceeds
                    p["open"] = False

            # Mark open positions to today's close
            mv = 0.0
            for idx, p in positions.items():
                if not p["open"]:
                    continue
                t = trades_sorted[idx]
                pmap = price_map.get(t["ticker"], {})
                px = _price_at_or_before(pmap, date)
                if px is None:
                    # If we can't price it, mark at last allocation value
                    mv += p["alloc"]
                    continue
                pos_val = p["alloc"] * (1 + p["sign"] * ((px - p["entry_px"]) / p["entry_px"]))
                mv += max(0.0, pos_val)
            out.append({"date": date, "value": round(cash + mv, 4)})
        return out

    def _buy_hold_curve() -> list[dict]:
        """Equal-weight buy-and-hold: each trade gets $1 at entry, held until
        explicit exit (or forever). Curve = avg ret_mult × 100 across all
        trades that have entered. Spike-prone when N is small early on."""
        out = []
        for date in sorted_dates_arr:
            vals = []
            for t in trades_sorted:
                rm = _ret_mult(t, date)
                if rm is not None:
                    vals.append(rm)
            if not vals:
                out.append({"date": date, "value": None})
                continue
            value = (sum(vals) / len(vals)) * 100
            out.append({"date": date, "value": round(value, 4)})
        return out

    if point_in_time:
        # Build PIT indexes once per request and use a closure that wraps
        # _weight_for_pit. Same signature as _weight_for so the existing
        # _twr_curve walker is reused unchanged.
        pit_idx = _build_pit_indexes(handle)
        def _weight_for_pit_closure(trade, curve_date=None):
            if curve_date is None:
                # Mimic _weight_for legacy path (no decay) — only used by
                # equal-weight buy-hold sanity curve, which doesn't care.
                return _weight_for_pit(trade, sorted_dates_arr[-1] if sorted_dates_arr else "", pit_idx)
            return _weight_for_pit(trade, curve_date, pit_idx)
        twr_curve = _twr_curve(_weight_for_pit_closure, date_aware=True,
                               basket_filter=_basket_filter_renorm)

        # Also rewrite the per-marker fields the FRONTEND consumes when it
        # recomputes basket weights for the Active Positions panel. Stamp
        # each marker with PIT-corrected values evaluated at TODAY (the
        # right reference for a "current basket" view). When the user
        # scrubs back in time on the panel, weights still use these
        # today-values — accepting that limitation here in exchange for
        # not having to ship the full tier_history per marker. The equity
        # curve itself, computed above, IS fully time-aware day by day.
        as_of = sorted_dates_arr[-1] if sorted_dates_arr else end_date.strftime("%Y-%m-%d")
        for tk, pt in per_ticker.items():
            for m in pt["markers"]:
                tier_pit = _tier_at(tk, m.get("direction", "long"), as_of, pit_idx["tier_history"])
                if tier_pit is not None:
                    m["tier"] = tier_pit
                # Recompute mention_share with PIT counts as of today
                nm = _count_le(pit_idx["mention_history"].get(tk, []), as_of)
                tm = _count_le(pit_idx["total_mention_history"], as_of)
                if tm > 0 and nm > 0:
                    m["mention_share"] = nm / tm
                # Last mention as of today
                last = _last_le(pit_idx["mention_history"].get(tk, []), as_of)
                if last:
                    m["last_mention_date"] = last
                # Median inter-mention gap for cadence-aware decay (client
                # uses this in markerWeight v2). None for sparse-history
                # tickers — client falls back to horizon τ.
                mg = pit_idx.get("median_gap", {}).get(tk)
                if mg is not None:
                    m["median_gap_days"] = round(mg, 2)
    else:
        twr_curve = _twr_curve(_weight_for, date_aware=True)
    mwr_curve = _mwr_cash_budget_curve()
    hold_curve = _buy_hold_curve()

    # SPY / QQQ curve: indexed to 100 at start
    def bench_curve(bmap):
        if not bmap:
            return []
        start_val = None
        out = []
        for date in sorted_dates:
            v = bmap.get(date)
            if v is None: continue
            if start_val is None:
                start_val = v
            out.append({"date": date, "value": round(v / start_val * 100, 4)})
        return out

    spy_curve = bench_curve(spy_map)
    qqq_curve = bench_curve(qqq_map)

    # Per-ticker list sorted by highest-return first
    ticker_list = sorted(
        per_ticker.values(),
        key=lambda x: max((m.get("stock_return") or 0) for m in x["markers"]),
        reverse=True,
    )
    # Trim to ticker with at least some price data (skip if no series)
    ticker_list = [t for t in ticker_list if t["prices"]]

    # Raw SPY / QQQ close prices over the same span. The aggregate `spy` /
    # `qqq` entries above are equity curves (indexed to 100); the per-ticker
    # charts want real SPY prices to overlay as a white reference line
    # scaled to each ticker's visible range.
    def _raw_series(bmap):
        return [{"date": d, "close": round(v, 4)} for d, v in sorted(bmap.items())]

    return {
        "handle": handle,
        "aggregate": {
            # Legacy keys (still consumed by older code paths)
            "equal_weight": equal_curve,
            "tier_weight": tier_curve,
            "spy": spy_curve,
            "qqq": qqq_curve,
            # New toggleable curves the frontend chooses between
            "curves": {
                "twr": twr_curve,
                "mwr": mwr_curve,
                "buy_hold": hold_curve,
            },
        },
        "spy_prices": _raw_series(spy_map),
        "qqq_prices": _raw_series(qqq_map),
        "tickers": ticker_list,
    }


if __name__ == "__main__":
    import sys
    h = sys.argv[1] if len(sys.argv) > 1 else "aleabitoreddit"
    out = build_charts_for_account(h)
    print(f"tickers: {len(out.get('tickers', []))}")
    if out.get("aggregate"):
        print(f"equity curve points: {len(out['aggregate']['equal_weight'])}")
        if out["aggregate"]["equal_weight"]:
            last = out["aggregate"]["equal_weight"][-1]
            print(f"final equal-weight value: {last}")
            last = out["aggregate"]["tier_weight"][-1]
            print(f"final tier-weight value: {last}")
