"""Per-account paper portfolio backtest.

For each actionable call, compute entry/exit prices + returns vs SPY and QQQ.
Aggregate into:
  - Equal-weight portfolio return / alpha
  - Tier-weighted portfolio return / alpha (S=3, A=2, B=1)
  - Per-trade rows for the UI table

Uses fetch_ohlcv.py (yfinance, cached).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from dateutil import parser as dtparser

from fetch_ohlcv import fetch_klines, symbol_for

ROOT = Path(__file__).parent
DATA = ROOT / "data"

TIER_WEIGHT = {"S": 3.0, "A": 2.0, "B": 1.0}
CONVICTION_WEIGHT = {"high": 3.0, "med": 2.0, "low": 1.0}

# How to handle classifier calls with direction but no ticker ("Long gold
# miners", "bearish on equities"). "drop" = ignore them (lossless w.r.t.
# portfolio math). "resolve_spy" = map long→SPY long, short→SPY short —
# treats macro calls as implicit SPY bets. Settable via XALPHA_NULL_TICKER env var.
NULL_TICKER_MODE = os.environ.get("XALPHA_NULL_TICKER_MODE", "drop")

# Widest window we'd ever backtest — covers ~2 years
_BENCH_CACHE: dict[str, pd.DataFrame] = {}


def _get_bench(ticker: str, start: datetime, end: datetime) -> pd.DataFrame | None:
    key = f"{ticker}:{start.date()}:{end.date()}"
    if key in _BENCH_CACHE:
        return _BENCH_CACHE[key]
    try:
        df = fetch_klines(ticker, start.replace(tzinfo=None) - timedelta(days=3),
                          end.replace(tzinfo=None) + timedelta(days=3))
    except Exception:
        return None
    _BENCH_CACHE[key] = df
    return df


def _finite(v):
    """None if v is NaN/inf, else the float. Foreign listings sometimes
    return rows with NaN close on US holidays (exchange calendars mismatch)."""
    if v is None: return None
    try:
        v = float(v)
    except Exception:
        return None
    import math as _math
    return v if _math.isfinite(v) else None


def _price_after(df: pd.DataFrame, when: datetime, col: str = "open") -> Optional[float]:
    """First non-NaN price in df after `when`."""
    if df is None or df.empty:
        return None
    ts = pd.Timestamp(when).tz_convert("UTC") if pd.Timestamp(when).tzinfo \
         else pd.Timestamp(when, tz="UTC")
    sub = df[df["open_time"] > ts]
    for _, row in sub.iterrows():
        v = _finite(row[col])
        if v is not None:
            return v
    return None


def _price_at_or_before(df: pd.DataFrame, when: datetime, col: str = "close") -> Optional[float]:
    if df is None or df.empty:
        return None
    ts = pd.Timestamp(when).tz_convert("UTC") if pd.Timestamp(when).tzinfo \
         else pd.Timestamp(when, tz="UTC")
    sub = df[df["open_time"] <= ts].iloc[::-1]  # walk backward for last valid
    for _, row in sub.iterrows():
        v = _finite(row[col])
        if v is not None:
            return v
    return None


@dataclass
class Trade:
    tweet_id: str
    ticker: str
    direction: str         # long or short
    conviction: str        # high/med/low
    tier: Optional[str]    # S/A/B if known
    style: Optional[str]
    reasoning: Optional[str]
    call_date: str         # ISO
    entry_date: Optional[str]
    entry_price: Optional[float]
    exit_date: Optional[str]
    exit_price: Optional[float]
    stock_return: Optional[float]  # signed for the author's position
    spy_return: Optional[float]    # same window
    qqq_return: Optional[float]
    alpha_spy: Optional[float]
    alpha_qqq: Optional[float]
    status: str            # "ok" | "no_data" | "bad_ticker"
    url: Optional[str]
    # Per-tweet density: 1.0 if tweet only named this ticker; 1/N for N-ticker
    # tweets. Dilutes list-spam in the equity curve.
    position_share: float = 1.0
    # Account-level concentration: fraction of all this account's stock-mention
    # tweets that mention THIS ticker. A passing 1-of-100 mention gets 0.01;
    # a focused account that mentions only 5 tickers gives each ~0.20. Used
    # by portfolio_charts to weight the open position. Catches Case 3
    # ("$X mentioned once in a 10-ticker tweet, then 100 other tickers" —
    # mention_share ≈ 0.005, killing spurious credit for the rare hit).
    mention_share: float = 1.0
    # Date of the LAST time this ticker was mentioned by this account (any
    # direction). The equity curve applies an exp-decay past this date, so
    # positions silently held fade out naturally without needing an explicit
    # "exit" event. Decay rate is HORIZON-AWARE (see `horizon` below).
    last_mention_date: Optional[str] = None
    # Author-stated holding horizon — modulates the recency-decay half-life
    # so a position labeled long_term doesn't fade out after 6 months of
    # silence (the author said they'd hold!). Schema enum:
    #   intraday    → 7d half-life (gone in a week of silence)
    #   swing       → 30d
    #   multi_week  → 90d
    #   unknown/null→ 180d (default)
    #   long_term   → 540d (~1.5y, barely decays in a year)
    horizon: Optional[str] = None


def build_trades_for_account(handle: str, end_date: datetime | None = None) -> dict:
    acct_dir = DATA / handle
    tweets_path = acct_dir / "tweets.json"
    calls_path = acct_dir / "calls_llm.json"
    signals_path = acct_dir / "signals_llm.json"
    if not tweets_path.exists() or not calls_path.exists():
        return {"handle": handle, "trades": [], "summary": None, "error": "missing data"}

    tweets = json.loads(tweets_path.read_text())
    tweet_by_id = {t["id"]: t for t in tweets}
    calls = json.loads(calls_path.read_text())

    # Manual overrides for misclassified tweets. The LLM occasionally tags
    # something as the wrong direction (e.g., a tweet ending with "Short"
    # gets read as a short call when context makes it bullish). Maintainers
    # can drop a `calls_llm_overrides.json` next to calls_llm.json to patch
    # specific tweet_ids without re-running the classifier.
    #
    # Override schema:
    #   - tweet_id (required)
    #   - tickers: [...]   — full replacement of the LLM's tickers list
    #   - drop_tickers: ["NBIS"]  — surgical: remove just these tickers from
    #     the original list, keep the rest. Used by the Flag button's "Drop
    #     ticker" preset for multi-ticker tweets where only some are wrong.
    #   - row_type / is_trade_call / actionable_for_portfolio — direct overrides
    #   - note — free-form, ignored by engine (paper trail)
    overrides_path = acct_dir / "calls_llm_overrides.json"
    if overrides_path.exists():
        overrides = json.loads(overrides_path.read_text())
        ov_by_id = {str(o["tweet_id"]): o for o in overrides if o.get("tweet_id")}
        for i, c in enumerate(calls):
            cid = str(c.get("tweet_id") or "")
            if cid not in ov_by_id:
                continue
            ovr = ov_by_id[cid]
            new_call = {**c, **ovr}
            # Surgical drop: filter the original tickers, don't replace.
            if "drop_tickers" in ovr:
                drop_set = {str(t).upper() for t in (ovr.get("drop_tickers") or [])}
                orig_tickers = c.get("tickers") or []
                kept = [t for t in orig_tickers
                        if (t.get("ticker", "") or "").upper() not in drop_set]
                new_call["tickers"] = kept
                # If we dropped them all, flip the call out of trade-call
                # status so it stops contributing positions.
                if not kept:
                    new_call["is_trade_call"] = False
                    new_call.setdefault("row_type", "thesis_or_opinion")
            calls[i] = new_call

    signals = json.loads(signals_path.read_text()) if signals_path.exists() else []
    tier_lookup = {(s["ticker"], s.get("direction")): s["tier"]
                   for s in signals if s.get("tier")}

    end_date = end_date or datetime.now(timezone.utc)

    # Pre-pass: per-ticker mention stats across ALL classifier-extracted calls
    # (any direction, including non-trade-call rows since "still long $X" or
    # "watching $X" still indicates the account is following the name).
    # mention_count counts unique tweets that named the ticker.
    # last_mention_date tracks the most recent mention (used for recency decay).
    # n_unique_stock_tweets = denominator for mention_share.
    from collections import defaultdict as _dd
    mention_count: dict[str, int] = _dd(int)
    last_mention_iso: dict[str, str] = {}
    seen_tweet_ticker: set = set()
    n_unique_stock_tweets = 0  # tweets mentioning ANY ticker (any direction)
    stock_tweet_ids: set = set()
    for c in calls:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("tweet_id") or "")
        tw = tweet_by_id.get(cid)
        ts = (tw or {}).get("created_at") if tw else None
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
        if cid:
            stock_tweet_ids.add(cid)
        for tk in names:
            key = (cid, tk)
            if key in seen_tweet_ticker:
                continue
            seen_tweet_ticker.add(key)
            mention_count[tk] += 1
            if ts and (tk not in last_mention_iso or ts > last_mention_iso[tk]):
                last_mention_iso[tk] = ts
    n_unique_stock_tweets = len(stock_tweet_ids)
    total_mentions = sum(mention_count.values()) or 1

    # Each event is (call, ticker, direction, conviction, action, position_share).
    # Action ∈ {enter, add, trim, exit, hold} drives position lifecycle below.
    # position_share = 1/N where N is the count of valid (ticker, direction)
    # pairs in the same source tweet — dilutes list-spam tweets in the
    # equity curve so a 5-ticker promo doesn't claim 5x portfolio capital.
    # Legacy calls without `action` default to "enter" (safe — pre-schema
    # calls only ever represented new positions).
    events: list[tuple[dict, str, str, str | None, str, float]] = []
    for c in calls:
        if not c.get("is_trade_call") or c.get("is_retrospective"):
            continue
        tickers = c.get("tickers")
        if tickers:
            valid = [
                ent for ent in tickers
                if ent.get("ticker") and ent.get("direction")
            ]
            n_valid = len(valid) or 1
            share = 1.0 / n_valid
            for ent in valid:
                events.append((
                    c, ent["ticker"], ent["direction"],
                    ent.get("conviction"), ent.get("action") or "enter",
                    share,
                ))
        else:
            tk = c.get("primary_ticker")
            direction = c.get("direction")
            if tk and direction:
                events.append((c, tk, direction, c.get("conviction"), "enter", 1.0))
            elif direction and NULL_TICKER_MODE == "resolve_spy":
                # Macro directional call with no explicit symbol — map to SPY.
                # Preserves the author's directional intent at the index level.
                events.append((c, "SPY", direction, c.get("conviction") or "med", "enter", 1.0))
    if not events:
        return {"handle": handle, "trades": [], "summary": None,
                "error": "no actionable calls"}

    # Determine date span — `events` is list[(call, ticker, direction, conv, action, share)]
    dates = []
    for c, _tk, _dir, _conv, _act, _share in events:
        t = tweet_by_id.get(c["tweet_id"], {})
        if t.get("created_at"):
            try:
                dates.append(dtparser.parse(t["created_at"]))
            except Exception:
                pass
    if not dates:
        return {"handle": handle, "trades": [], "summary": None, "error": "no dates"}
    start = min(dates)

    spy = _get_bench("SPY", start, end_date)
    qqq = _get_bench("QQQ", start, end_date)

    # Per-ticker OHLCV cache for this account
    ticker_cache: dict[str, pd.DataFrame | None] = {}

    def get_ticker_df(ticker: str) -> pd.DataFrame | None:
        if ticker in ticker_cache:
            return ticker_cache[ticker]
        sym = symbol_for(ticker)
        if sym is None:
            ticker_cache[ticker] = None
            return None
        try:
            df = fetch_klines(sym, start.replace(tzinfo=None) - timedelta(days=3),
                              end_date.replace(tzinfo=None) + timedelta(days=3))
            ticker_cache[ticker] = df
            return df
        except Exception:
            ticker_cache[ticker] = None
            return None

    # ── Position-aware pairing ────────────────────────────────────────────
    # Walk events chronologically per (ticker, direction). `enter` opens a
    # position, `exit` closes it (realizing P&L at that date's price).
    # `add`/`trim`/`hold` don't change open/closed state — they're conviction
    # reinforcement without a size field to work with. Re-entry after exit
    # starts a fresh position (emitted as a separate Trade row).
    def _call_at(entry):
        c = entry[0]
        return tweet_by_id.get(c["tweet_id"], {}).get("created_at", "")
    sorted_events = sorted(events, key=_call_at)

    # Same-day dedup within a (ticker, direction, action-class, day) bucket.
    # Action-class collapses add/enter and trim/exit into two classes to avoid
    # spurious double-events when a tweet repeats.
    def _action_class(a: str) -> str:
        return {"enter": "open", "add": "open",
                "trim": "close", "exit": "close"}.get(a, "hold")
    seen_day: set = set()
    compact_events: list = []
    for c, ticker, direction, conv_in, action, share in sorted_events:
        t = tweet_by_id.get(c["tweet_id"], {})
        if not t.get("created_at"): continue
        day = t["created_at"][:10]
        tk_up = ticker.upper().lstrip("$")
        key = (tk_up, direction, _action_class(action), day)
        if key in seen_day: continue
        seen_day.add(key)
        compact_events.append((c, tk_up, direction, conv_in, action, t, share))

    # Per-ticker,direction state: list of completed positions + optional open one.
    # Each position = {call_in, call_out (None if open), ...}
    positions_by_key: dict[tuple[str, str], list[dict]] = {}
    open_pos: dict[tuple[str, str], dict] = {}

    for c, ticker, direction, conv_in, action, tw, share in compact_events:
        key = (ticker, direction)
        call_date = tw["created_at"]
        if action in ("enter", "add"):
            if key not in open_pos:
                open_pos[key] = {
                    "call_in": c, "tweet_in": tw,
                    "conviction": conv_in, "call_out": None, "tweet_out": None,
                    "reinforcements": 0, "exits_seen": 0,
                    "position_share": share,
                }
            else:
                open_pos[key]["reinforcements"] += 1
                if conv_in == "high":
                    open_pos[key]["conviction"] = "high"  # escalate conviction
                # Use the LARGER share if this is a focused re-entry
                # (e.g., "Long $X" alone after a multi-ticker tweet means
                # author conviction has crystallized on this name).
                if share > open_pos[key].get("position_share", 1.0):
                    open_pos[key]["position_share"] = share
        elif action in ("exit", "trim"):
            if key in open_pos:
                # First exit/trim closes the position. A subsequent trim is ignored.
                if action == "exit" or open_pos[key].get("first_exit") is None:
                    open_pos[key]["call_out"] = c
                    open_pos[key]["tweet_out"] = tw
                    open_pos[key]["first_exit"] = action
                    positions_by_key.setdefault(key, []).append(open_pos.pop(key))
        # hold → no state change

    # Flush any still-open positions (no exit tweet seen — position still held)
    for key, pos in open_pos.items():
        positions_by_key.setdefault(key, []).append(pos)

    trades: list[Trade] = []
    for (ticker, direction), poslist in positions_by_key.items():
        for pos in poslist:
            c = pos["call_in"]; tw_in = pos["tweet_in"]
            tw_out = pos["tweet_out"]
            conv = pos.get("conviction") or "med"
            tier = tier_lookup.get((ticker, direction))
            call_dt = dtparser.parse(tw_in["created_at"])
            df = get_ticker_df(ticker)
            sign = 1 if direction == "long" else -1

            share = pos.get("position_share", 1.0)
            tk_share = mention_count.get(ticker, 1) / total_mentions
            tk_last = last_mention_iso.get(ticker)
            if df is None or df.empty:
                trades.append(Trade(
                    tweet_id=c["tweet_id"], ticker=ticker, direction=direction,
                    conviction=conv, tier=tier, style=c.get("style"),
                    reasoning=c.get("reasoning"),
                    call_date=call_dt.isoformat(),
                    entry_date=None, entry_price=None,
                    exit_date=None, exit_price=None,
                    stock_return=None, spy_return=None, qqq_return=None,
                    alpha_spy=None, alpha_qqq=None,
                    status="bad_ticker", url=tw_in.get("url"),
                    position_share=share,
                    mention_share=round(tk_share, 4),
                    last_mention_date=tk_last,
                    horizon=c.get("horizon"),
                ))
                continue

            entry = _price_after(df, call_dt, "open")
            # If the position has an explicit exit event, use that date's close.
            # Otherwise mark-to-market against end_date (position still open).
            if tw_out:
                exit_dt = dtparser.parse(tw_out["created_at"])
                exit_price = _price_at_or_before(df, exit_dt, "close")
                exit_date_str = str(exit_dt)[:10]
                position_closed = True
            else:
                exit_dt = end_date
                exit_price = _price_at_or_before(df, end_date, "close")
                exit_date_str = str(end_date)[:10]
                position_closed = False

            if entry is None or exit_price is None or not entry:
                trades.append(Trade(
                    tweet_id=c["tweet_id"], ticker=ticker, direction=direction,
                    conviction=conv, tier=tier, style=c.get("style"),
                    reasoning=c.get("reasoning"),
                    call_date=call_dt.isoformat(),
                    entry_date=None, entry_price=None,
                    exit_date=None, exit_price=None,
                    stock_return=None, spy_return=None, qqq_return=None,
                    alpha_spy=None, alpha_qqq=None,
                    status="no_data", url=tw_in.get("url"),
                    position_share=share,
                    mention_share=round(tk_share, 4),
                    last_mention_date=tk_last,
                    horizon=c.get("horizon"),
                ))
                continue

            raw_return = (exit_price - entry) / entry
            stock_return = sign * raw_return

            entry_time = df[df["open_time"] > pd.Timestamp(call_dt).tz_convert("UTC")].head(1)["open_time"]
            benchmark_start = entry_time.iloc[0].to_pydatetime() if not entry_time.empty else call_dt

            spy_start = _price_after(spy, benchmark_start, "open") if spy is not None else None
            spy_end = _price_at_or_before(spy, exit_dt, "close") if spy is not None else None
            qqq_start = _price_after(qqq, benchmark_start, "open") if qqq is not None else None
            qqq_end = _price_at_or_before(qqq, exit_dt, "close") if qqq is not None else None

            spy_return = ((spy_end - spy_start) / spy_start) if (spy_start and spy_end) else None
            qqq_return = ((qqq_end - qqq_start) / qqq_start) if (qqq_start and qqq_end) else None
            alpha_spy = (stock_return - (sign * spy_return)) if spy_return is not None else None
            alpha_qqq = (stock_return - (sign * qqq_return)) if qqq_return is not None else None

            trades.append(Trade(
                tweet_id=c["tweet_id"], ticker=ticker, direction=direction,
                conviction=conv, tier=tier, style=c.get("style"),
                reasoning=c.get("reasoning"),
                call_date=call_dt.isoformat(),
                entry_date=str(benchmark_start)[:10], entry_price=round(entry, 2),
                exit_date=exit_date_str, exit_price=round(exit_price, 2),
                stock_return=round(stock_return, 4),
                spy_return=round(spy_return, 4) if spy_return is not None else None,
                qqq_return=round(qqq_return, 4) if qqq_return is not None else None,
                alpha_spy=round(alpha_spy, 4) if alpha_spy is not None else None,
                alpha_qqq=round(alpha_qqq, 4) if alpha_qqq is not None else None,
                status="ok", url=tw_in.get("url"),
                position_share=share,
                mention_share=round(tk_share, 4),
                last_mention_date=tk_last,
            ))

    ok = [t for t in trades if t.status == "ok"]
    summary = {}
    if ok:
        def avg(vals):
            vals = [v for v in vals if v is not None]
            return round(sum(vals) / len(vals), 4) if vals else None

        def weighted_avg(rows, weight_fn):
            num = 0.0
            den = 0.0
            for r in rows:
                w = weight_fn(r)
                if r.stock_return is None or not w:
                    continue
                num += r.stock_return * w
                den += w
            return round(num / den, 4) if den else None

        # Composite weight: tier × conviction × position_share. The
        # position_share dilutes list-spam tweets (a 5-ticker promo gives
        # each ticker 0.2× weight) so a daily-list-style account can't claim
        # 5x portfolio capital.
        def _signal_weight(r):
            tw = TIER_WEIGHT.get(r.tier, CONVICTION_WEIGHT.get(r.conviction, 1.0))
            return tw * (r.position_share or 1.0)

        # "Effective" trade count = sum of position_shares. SpartanTrading
        # 1294 raw → 334 effective (each 5-ticker promo = 1 unit of capital).
        effective_n_trades = round(sum(r.position_share or 1.0 for r in ok), 1)
        # Selectivity: fraction of all account tweets that mention any stock.
        # Lower = more selective (Case 4: 1000 tweets, 2 mentions → 0.002).
        # Used by server.py:_account_stats to gentle-shrink quiet selective
        # accounts (their few calls carry more signal than spammy daily lists).
        n_total_tweets = max(1, len(tweets))
        selectivity = round(n_unique_stock_tweets / n_total_tweets, 4)

        summary = {
            "n_trades": len(ok),
            "n_skipped": len(trades) - len(ok),
            "effective_n_trades": effective_n_trades,
            "n_total_tweets": n_total_tweets,
            "n_stock_tweets": n_unique_stock_tweets,
            "n_unique_tickers": len(mention_count),
            "selectivity": selectivity,
            "win_rate": round(sum(1 for r in ok if (r.stock_return or 0) > 0) / len(ok), 4),
            # Equal weight (legacy — kept for transparency)
            "equal_weight_return": avg([r.stock_return for r in ok]),
            "equal_weight_alpha_spy": avg([r.alpha_spy for r in ok]),
            "equal_weight_alpha_qqq": avg([r.alpha_qqq for r in ok]),
            # Tier-weighted (use tier when present, else conviction) — legacy
            "tier_weighted_return": weighted_avg(
                ok, lambda r: TIER_WEIGHT.get(r.tier, CONVICTION_WEIGHT.get(r.conviction, 1.0))
            ),
            "tier_weighted_alpha_spy": (lambda rows: weighted_avg(
                [type(r)(**{**asdict(r), "stock_return": r.alpha_spy}) for r in rows if r.alpha_spy is not None],
                lambda r: TIER_WEIGHT.get(r.tier, CONVICTION_WEIGHT.get(r.conviction, 1.0))
            ))(ok),
            "tier_weighted_alpha_qqq": (lambda rows: weighted_avg(
                [type(r)(**{**asdict(r), "stock_return": r.alpha_qqq}) for r in rows if r.alpha_qqq is not None],
                lambda r: TIER_WEIGHT.get(r.tier, CONVICTION_WEIGHT.get(r.conviction, 1.0))
            ))(ok),
            # Signal-weighted (tier × conviction × position_share) — the new primary
            "signal_weighted_return": weighted_avg(ok, _signal_weight),
            "signal_weighted_alpha_spy": (lambda rows: weighted_avg(
                [type(r)(**{**asdict(r), "stock_return": r.alpha_spy}) for r in rows if r.alpha_spy is not None],
                _signal_weight,
            ))(ok),
            "signal_weighted_alpha_qqq": (lambda rows: weighted_avg(
                [type(r)(**{**asdict(r), "stock_return": r.alpha_qqq}) for r in rows if r.alpha_qqq is not None],
                _signal_weight,
            ))(ok),
            "best_trade": max(ok, key=lambda r: r.stock_return or -999).ticker,
            "best_return": round(max((r.stock_return for r in ok), default=0), 4),
            "worst_trade": min(ok, key=lambda r: r.stock_return or 999).ticker,
            "worst_return": round(min((r.stock_return for r in ok), default=0), 4),
        }

    return {
        "handle": handle,
        "summary": summary,
        "trades": [asdict(t) for t in trades],
    }


if __name__ == "__main__":
    import sys
    h = sys.argv[1] if len(sys.argv) > 1 else "aleabitoreddit"
    out = build_trades_for_account(h)
    print(json.dumps(out.get("summary", {}), indent=2))
    print(f"\n{len(out['trades'])} trade rows (showing first 5):")
    for t in out["trades"][:5]:
        print(f"  {t}")
