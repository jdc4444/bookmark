"""Re-run the 'best interpretable' parameter sweep formula (final = 553.01)
against frozen xalpha portfolio_charts.json. Produces a sidebar metric that
gives the report something concrete to point at when discussing
'self-reported 630%' — not as audit, just as flavor.

Formula (extracted from the codex run on 2026-05-08):
  tier_mix=1, tier_power=1, conv_power=1, untiered_floor=0,
  pshare_mix=0.3, pshare_power=1, mention_power=1, mention_prior=0,
  recency_mode='zero', hl_scale=1, recency_strength=1,
  threshold=0.02, top_n=null, max_w=null, fallback='flat'

In English: tier_weight × (0.3*position_share + 0.7*mention_share),
            decayed via exp(-days_since_last_mention / horizon_half_life)
            from the moment the last mention happens, drop weights below 2%,
            no concentration cap.

Reads directly from data/inputs/xalpha_engine/* (frozen) so nothing here
shifts when xalpha churns.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import (  # noqa: E402
    XALPHA_ROOT, load_meta, report_inputs, report_prep, validate_report_id
)

# These get bound in main() once we know the report_id. The xalpha engine
# imports also happen inside main() — see _import_engine() — so the FROZEN
# copy of portfolio_charts.py / portfolio.py is preferred over the live
# version. portfolio_charts itself imports fetch_ohlcv from the xalpha
# tree, so we add the live xalpha to sys.path as a fallback only (the
# frozen portfolio_charts will use the live fetch_ohlcv shim, but never
# actually call fetch_klines because all prices are cached in
# portfolio_charts.json).
REPORT_ID = ""
INPUTS: Path = Path()
PREP: Path = Path()
HANDLE = ""

# Bound in _import_engine().
_marker_key = None
_cached_curve_dates = None
_cached_price_maps = None
_build_cached_price_lookup = None
_ret_mult_from_cached_prices = None
_build_pit_indexes = None
_tier_at = None
_count_le = None
_last_le = None
_weight_for = None
_weight_for_pit = None
_HORIZON_HALFLIFE_DAYS = None
TIER_WEIGHT = None
CONVICTION_WEIGHT = None
pc = None  # the imported portfolio_charts module


def _import_engine() -> None:
    """Import the xalpha engine, preferring the frozen copy at
    `<report_id>/inputs/xalpha_engine/` over the live xalpha tree.
    portfolio_charts.py itself imports `fetch_ohlcv`, which only lives in
    the live xalpha repo — so we add live xalpha *after* the frozen copy,
    and rely on python's import precedence to pick up frozen first."""
    global _marker_key, _cached_curve_dates, _cached_price_maps
    global _build_cached_price_lookup, _ret_mult_from_cached_prices
    global _build_pit_indexes, _tier_at, _count_le, _last_le
    global _weight_for, _weight_for_pit, _HORIZON_HALFLIFE_DAYS
    global TIER_WEIGHT, CONVICTION_WEIGHT, pc
    frozen_engine = INPUTS / "xalpha_engine"
    if frozen_engine.exists():
        sys.path.insert(0, str(frozen_engine))
    sys.path.append(str(XALPHA_ROOT))  # fallback for fetch_ohlcv
    import portfolio_charts as _pc  # type: ignore
    from portfolio import (  # type: ignore
        TIER_WEIGHT as _TW, CONVICTION_WEIGHT as _CW,
    )
    pc = _pc
    _marker_key = _pc._marker_key
    _cached_curve_dates = _pc._cached_curve_dates
    _cached_price_maps = _pc._cached_price_maps
    _build_cached_price_lookup = _pc._build_cached_price_lookup
    _ret_mult_from_cached_prices = _pc._ret_mult_from_cached_prices
    _build_pit_indexes = _pc._build_pit_indexes
    _tier_at = _pc._tier_at
    _count_le = _pc._count_le
    _last_le = _pc._last_le
    _weight_for = _pc._weight_for
    _weight_for_pit = _pc._weight_for_pit
    _HORIZON_HALFLIFE_DAYS = _pc._HORIZON_HALFLIFE_DAYS
    TIER_WEIGHT = _TW
    CONVICTION_WEIGHT = _CW

# The "best interpretable" sweep result — 553.01 (final), -20.1% max DD, 90.5%
# annualized vol, 14.7 avg open positions, 0 fallback days.
BEST_INTERPRETABLE = {
    "tier_mix": 1, "tier_power": 1, "conv_power": 1, "untiered_floor": 0,
    "pshare_mix": 0.3, "pshare_power": 1, "mention_power": 1, "mention_prior": 0,
    "recency_mode": "zero", "hl_scale": 1, "recency_strength": 1,
    "threshold": 0.02, "top_n": None, "max_w": None, "fallback": "flat",
}

# Also report the cleaner top-5 / 20%-cap result for context.
BEST_CLEAN_TOP5 = {
    "tier_mix": 0.1, "tier_power": 0.5, "conv_power": 2, "untiered_floor": 0.5,
    "pshare_mix": 1, "pshare_power": 2, "mention_power": 1.5, "mention_prior": 2,
    "recency_mode": "zero", "hl_scale": 0.25, "recency_strength": 3,
    "threshold": 0.005, "top_n": 5, "max_w": 0.2, "fallback": "max",
}


def build_trade_set(handle: str) -> tuple[list[dict], list[str]]:
    chart_path = INPUTS / "xalpha" / handle / "portfolio_charts.json"
    pf_path = INPUTS / "xalpha" / handle / "portfolio.json"
    chart = json.loads(chart_path.read_text())
    pf = json.loads(pf_path.read_text())

    keys: set[str] = set()
    marker_trades: list[dict] = []
    for row in chart.get("tickers") or []:
        ticker = (row.get("ticker") or "").strip().lstrip("$").upper()
        for m in row.get("markers") or []:
            key = _marker_key(
                ticker, m.get("date"), m.get("entry_date"),
                m.get("direction"), m.get("entry_price"), m.get("conviction"),
            )
            keys.add(key)
            marker_trades.append({
                "status": "ok", "ticker": ticker,
                "direction": m.get("direction"), "conviction": m.get("conviction"),
                "call_date": m.get("date"), "entry_date": m.get("entry_date"),
                "entry_price": m.get("entry_price"),
                "exit_date": None, "exit_price": None,
                "position_share": m.get("position_share"),
                "horizon": m.get("horizon"),
                "tier": m.get("tier"),
                "_marker_key": key,
            })

    trades: list[dict] = []
    seen: set[str] = set()
    for t in pf.get("trades") or []:
        if t.get("status") != "ok" or not t.get("entry_date") or not t.get("entry_price"):
            continue
        key = _marker_key(
            t.get("ticker"), t.get("call_date"), t.get("entry_date"),
            t.get("direction"), t.get("entry_price"), t.get("conviction"),
        )
        if key in keys:
            t = dict(t); t["_marker_key"] = key
            trades.append(t); seen.add(key)
    for m in marker_trades:
        if m["_marker_key"] not in seen:
            trades.append(m)
    trades.sort(key=lambda t: t.get("entry_date") or "")
    dates = _cached_curve_dates(chart)
    return trades, dates, chart


def reconstruct(params: dict) -> dict:
    trades, dates, chart = build_trade_set(HANDLE)
    D = len(dates); T = len(trades)
    # _build_pit_indexes reads from the module's DATA constant — point it at
    # the frozen account dir so we read frozen mention/tier history rather
    # than the live xalpha tree.
    pc.DATA = INPUTS / "xalpha"
    idx = pc._build_pit_indexes(HANDLE)
    lookup = _build_cached_price_lookup(_cached_price_maps(chart))

    final_date = dates[-1]
    R = np.full((D, T), np.nan)
    RM = np.full((D, T), np.nan)
    for di, d in enumerate(dates):
        for ti, t in enumerate(trades):
            rm = _ret_mult_from_cached_prices(t, d, final_date, lookup)
            if rm is not None and math.isfinite(rm):
                RM[di, ti] = rm
    for di in range(1, D):
        ok = np.isfinite(RM[di-1]) & np.isfinite(RM[di]) & (RM[di-1] > 0)
        R[di, ok] = RM[di, ok] / RM[di-1, ok] - 1.0

    tier = np.zeros((D, T))
    conv = np.zeros(T)
    ps = np.ones(T)
    ms = np.zeros((D, T))
    nm_mat = np.zeros((D, T))
    tm_vec = np.zeros(D)
    silence = np.zeros((D, T))
    hl_vec = np.ones(T)
    bound_current = np.zeros((D, T))
    active_mention = np.zeros((D, T), dtype=bool)

    for ti, t in enumerate(trades):
        tk = (t.get("ticker") or "").strip().lstrip("$").upper()
        direction = t.get("direction", "long")
        conv[ti] = CONVICTION_WEIGHT.get((t.get("conviction") or "med").lower(), 1.0)
        ps[ti] = 1.0 if t.get("position_share") is None else float(t.get("position_share"))
        hl = _HORIZON_HALFLIFE_DAYS.get((t.get("horizon") or "unknown").lower(), 180)
        hl_vec[ti] = hl
        median = idx.get("median_gap", {}).get(tk)
        for di, d in enumerate(dates):
            tw = _tier_at(tk, direction, d, idx["tier_history"])
            tier[di, ti] = TIER_WEIGHT.get(tw, 0.0) if tw else 0.0
            tm = _count_le(idx["total_mention_history"], d)
            nm = _count_le(idx["mention_history"].get(tk, []), d)
            tm_vec[di] = tm
            nm_mat[di, ti] = nm
            ms[di, ti] = (nm / tm) if tm else 0.0
            last = _last_le(idx["mention_history"].get(tk, []), d)
            if last:
                active_mention[di, ti] = True
                try:
                    dd = (datetime.strptime(d[:10], "%Y-%m-%d")
                          - datetime.strptime(last[:10], "%Y-%m-%d")).days
                except Exception:
                    dd = 0
                silence[di, ti] = max(0, dd)
                bound_current[di, ti] = (max(hl, 2.0 * median)
                                         if median is not None else float(hl))

    p = params
    tw_arr = np.where(tier > 0, np.power(tier, p["tier_power"]), p["untiered_floor"])
    cw_arr = np.power(conv, p["conv_power"])
    base = p["tier_mix"] * tw_arr + (1 - p["tier_mix"]) * cw_arr[None, :]
    pp = np.power(ps, p["pshare_power"])
    if p["mention_prior"]:
        m = (nm_mat + p["mention_prior"]) / (tm_vec[:, None] + p["mention_prior"] * T)
    else:
        m = ms
    mm = np.where(m > 0, np.power(m, p["mention_power"]), 0)
    shares = p["pshare_mix"] * pp[None, :] + (1 - p["pshare_mix"]) * mm

    if p["recency_strength"] == 0 or p["recency_mode"] == "none":
        decay = np.ones((D, T))
    else:
        if p["recency_mode"] == "zero":
            bound = np.zeros((D, T))
        elif p["recency_mode"] == "half_horizon":
            bound = 0.5 * hl_vec[None, :]
        elif p["recency_mode"] == "horizon":
            bound = hl_vec[None, :]
        elif p["recency_mode"] == "current":
            bound = bound_current
        else:
            bound = bound_current
        excess = np.maximum(0, silence - bound)
        decay = np.exp(-excess / (hl_vec[None, :] * p["hl_scale"])) ** p["recency_strength"]
        decay[~active_mention] = 1.0

    W = base * shares * decay

    val = 100.0
    vals = [val]
    ns: list[int] = []
    fb = 0
    threshold, top_n, max_w, fallback = p["threshold"], p["top_n"], p["max_w"], p["fallback"]
    for di in range(1, D):
        ok = np.isfinite(R[di]) & np.isfinite(W[di]) & (W[di] > 0)
        if not ok.any():
            vals.append(val); continue
        w = np.where(ok, W[di], 0.0)
        s = w.sum()
        if s <= 0:
            vals.append(val); continue
        w = w / s
        if threshold > 0:
            w = np.where(w >= threshold, w, 0.0)
        if top_n is not None and (w > 0).sum() > top_n:
            inds = np.argpartition(w, -top_n)[-top_n:]
            mask = np.zeros(T, dtype=bool); mask[inds] = True
            w = np.where(mask, w, 0.0)
        if max_w is not None and max_w > 0:
            w = np.minimum(w, max_w)
        s = w.sum()
        if s <= 0:
            fb += 1
            if fallback == "max":
                j = int(np.argmax(np.where(ok, W[di], -1)))
                w = np.zeros(T); w[j] = 1.0
            else:
                vals.append(val); ns.append(0); continue
        else:
            w = w / s
        ns.append(int((w > 0).sum()))
        val *= 1.0 + float(np.nansum(w * R[di]))
        vals.append(val)

    arr = np.array(vals)
    peak = np.maximum.accumulate(arr)
    dd_pct = float(np.min(arr / peak - 1.0))
    daily = arr[1:] / arr[:-1] - 1
    vol = float(np.std(daily) * math.sqrt(252))
    return {
        "final": round(float(arr[-1]), 2),
        "max_dd_pct": round(dd_pct * 100, 1),
        "ann_vol_pct": round(vol * 100, 1),
        "avg_n": round(float(np.mean(ns) if ns else 0), 1),
        "fallback_days": fb,
        "params": params,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--handle", help="X handle; defaults to meta.json")
    p.add_argument("--report-id", required=True)
    args = p.parse_args(argv)
    if getattr(args, 'report_id', None):
        validate_report_id(args.report_id)
    global REPORT_ID, INPUTS, PREP, HANDLE
    meta = load_meta(args.report_id)
    handle = (args.handle or meta.get("handle") or "").strip().lstrip("@")
    if not handle:
        p.error("could not determine handle; pass --handle or run freeze first")

    REPORT_ID = args.report_id
    INPUTS = report_inputs(REPORT_ID)
    PREP = report_prep(REPORT_ID)
    PREP.mkdir(parents=True, exist_ok=True)
    HANDLE = handle
    _import_engine()

    print(f"Reconstructing weighted-portfolio sidebar for @{handle} from frozen inputs...\n")
    interp = reconstruct(BEST_INTERPRETABLE)
    print(f"  best_interpretable: {interp}")
    clean = reconstruct(BEST_CLEAN_TOP5)
    print(f"  best_clean_top5:    {clean}")

    out = {
        "handle": handle,
        "computed_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "best_interpretable": interp,
        "best_clean_top5": clean,
        "note": (
            "Sidebar metric — not the headline. These two formulas come from "
            "an in-sample sweep tuned on aleabit's series; treat them as a "
            "rough sense of how a sensible weighting reconstructs the "
            "track record, not as audited returns."
        ),
    }
    out_path = PREP / "reconstruction.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\n→ {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
