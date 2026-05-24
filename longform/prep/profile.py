"""Build profile.json — every account fact that doesn't need web research.

Account-agnostic: takes --handle + --report-id, reads frozen xalpha data
under data/longform/<report_id>/inputs/xalpha/<handle>/, writes to
data/longform/<report_id>/prep/profile.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import load_meta, report_inputs, report_prep, validate_report_id  # noqa: E402


def load(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def build_profile(handle: str, report_id: str) -> dict:
    in_dir = report_inputs(report_id) / "xalpha" / handle
    profile = load(in_dir / "profile.json", {})
    tweets = load(in_dir / "tweets.json", [])
    calls = load(in_dir / "calls_llm.json", [])
    network = load(in_dir / "network.json", {})
    following = load(in_dir / "following.json", {})
    dossiers = load(in_dir / "dossiers.json", [])
    portfolio = load(in_dir / "portfolio.json", {})

    by_month = Counter(t["created_at"][:7] for t in tweets if t.get("created_at"))
    media_count = sum(1 for t in tweets if (t.get("images") or []))
    text_lens = [len(t.get("text") or "") for t in tweets]
    avg_len = sum(text_lens) / len(text_lens) if text_lens else 0

    row_types = Counter(c.get("row_type") for c in calls)
    convictions: Counter = Counter()
    for c in calls:
        if not c.get("is_trade_call"):
            continue
        for ent in (c.get("tickers") or []):
            if isinstance(ent, dict):
                convictions[ent.get("conviction", "unknown")] += 1

    first_mentions: dict[str, str] = {}
    by_id = {str(t.get("id")): t for t in tweets}
    for c in calls:
        if c.get("is_retrospective"):
            continue
        tw = by_id.get(str(c.get("tweet_id")))
        if not tw:
            continue
        ts = (tw.get("created_at") or "")[:10]
        if not ts:
            continue
        for ent in (c.get("tickers") or []):
            t = ent.get("ticker") if isinstance(ent, dict) else ent
            if not t:
                continue
            if t not in first_mentions or ts < first_mentions[t]:
                first_mentions[t] = ts

    replied_to = network.get("replied_to") or {}
    mentioned = network.get("mentioned") or {}
    follows: list[str] = []
    for entry in (following.get("follows") or []):
        if isinstance(entry, dict):
            follows.append(entry.get("handle") or entry.get("username") or "")
        else:
            follows.append(str(entry))
    follows = [f for f in follows if f]

    top_dossiers = sorted(dossiers, key=lambda d: -d.get("score", 0))[:30]
    summary = portfolio.get("summary") or {}

    return {
        "handle": profile.get("handle", handle),
        "display_name": profile.get("display_name") or handle,
        "bio": profile.get("bio", ""),
        "profile_scraped_at": profile.get("profile_scraped_at"),
        "tweet_volume": {
            "total": len(tweets),
            "by_month": dict(sorted(by_month.items())),
            "first_tweet_date": (tweets[-1].get("created_at", "")[:10] if tweets else None),
            "last_tweet_date": (tweets[0].get("created_at", "")[:10] if tweets else None),
        },
        "style": {
            "avg_text_len": round(avg_len, 1),
            "with_image_pct": round(media_count / len(tweets) * 100, 1) if tweets else 0,
            "row_type_distribution": dict(row_types.most_common()),
            "conviction_on_trade_calls": dict(convictions.most_common()),
        },
        "network": {
            "n_following": len(follows),
            "follows": sorted(follows),
            "top_replied_to": [
                {"handle": h, "n": n}
                for h, n in sorted(replied_to.items(), key=lambda kv: -kv[1])[:25]
            ],
            "top_mentioned": [
                {"handle": h, "n": n}
                for h, n in sorted(mentioned.items(), key=lambda kv: -kv[1])[:25]
            ],
            "reposted_from": network.get("reposted_from") or {},
        },
        "portfolio_summary": {
            "n_trades": summary.get("n_trades"),
            "n_unique_tickers": summary.get("n_unique_tickers"),
            "selectivity": summary.get("selectivity"),
            "win_rate": summary.get("win_rate"),
            "equal_weight_return": summary.get("equal_weight_return"),
            "signal_weighted_return": summary.get("signal_weighted_return"),
            "best_trade": summary.get("best_trade"),
            "best_return": summary.get("best_return"),
            "worst_trade": summary.get("worst_trade"),
            "worst_return": summary.get("worst_return"),
        },
        "top_dossiers": [
            {
                "ticker": d.get("ticker"),
                "tier": d.get("tier"),
                "score": d.get("score"),
                "mentions": d.get("total_mentions"),
                "first_mention": d.get("first_mention"),
                "last_mention": d.get("last_mention"),
                "alpha": (d.get("return") or {}).get("pct"),
                "spy_window_alpha": d.get("alpha"),
            }
            for d in top_dossiers
        ],
        "first_mention_by_ticker": dict(sorted(first_mentions.items(), key=lambda kv: kv[1])),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--handle", help="X handle; defaults to meta.json")
    p.add_argument("--report-id", required=True)
    args = p.parse_args(argv)
    if getattr(args, 'report_id', None):
        validate_report_id(args.report_id)
    meta = load_meta(args.report_id)
    handle = (args.handle or meta.get("handle") or "").strip().lstrip("@")
    if not handle:
        p.error("could not determine handle; pass --handle or run freeze first")

    out = build_profile(handle, args.report_id)
    out_path = report_prep(args.report_id) / "profile.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"→ {out_path}")
    print(f"  display_name: {out['display_name']}")
    print(f"  tweet count: {out['tweet_volume']['total']}")
    print(f"  date range: {out['tweet_volume']['first_tweet_date']} → "
          f"{out['tweet_volume']['last_tweet_date']}")
    print(f"  follows: {out['network']['n_following']}")
    if out['network']['top_replied_to']:
        top = out['network']['top_replied_to'][0]
        print(f"  top dialogue partner: {top['handle']} ({top['n']} replies)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
