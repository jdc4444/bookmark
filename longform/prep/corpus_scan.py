"""Scan the entire xalpha corpus for mentions of a target handle.

This is the ONE prep stage that reads LIVE xalpha data rather than the
frozen snapshot — by design. The point of corpus_scan is to find which
*other* accounts in the wider xalpha universe talk about our subject, and
freezing all 85k of them per-report would balloon snapshots from ~50MB to
~50GB. The output (corpus_mentions.json) becomes the snapshot artifact.

Outputs data/longform/<report_id>/prep/corpus_mentions.json with:
- top_mentioners: handles ranked by mention count
- signals: heuristic flags (high_mention_rate, low_volume_heavy_mention)
- samples_by_handle: sample tweets from each top mentioner

Pure-data, no LLM. ~30s against ~85,000 handles.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import (  # noqa: E402
    XALPHA_ROOT, derive_report_id, load_meta,
    report_inputs, report_prep, validate_report_id
)

XALPHA_DATA = XALPHA_ROOT / "data"


def build_match_re(handle: str, display_name: str | None) -> re.Pattern:
    """A single combined regex matching @handle, x.com/handle, twitter.com/handle,
    and the display name when it's distinctive (≥4 chars, not all common-word).
    """
    parts = [rf"@{re.escape(handle)}"]
    parts.append(rf"x\.com/{re.escape(handle)}")
    parts.append(rf"twitter\.com/{re.escape(handle)}")
    if display_name and len(display_name) >= 4 and display_name.lower() not in COMMON_NAMES:
        # Match the display name only when adjacent to capitals/punctuation —
        # bare words like "Alex" otherwise produce huge false-positive sets.
        parts.append(
            r"(?:^|[\s\"\'(@])" + re.escape(display_name) + r"(?:[\s.,;:!\?\)]|$)"
        )
    return re.compile("|".join(parts), re.IGNORECASE)


COMMON_NAMES = {
    "alex", "anna", "ben", "chris", "dan", "david", "eric", "jake",
    "james", "john", "mark", "matt", "max", "mike", "nick", "ryan",
    "sam", "tom",
}


def scan_one_handle(handle_dir: Path, target_handle: str,
                    match_re: re.Pattern) -> dict | None:
    tweets_path = handle_dir / "tweets.json"
    if not tweets_path.exists() or tweets_path.stat().st_size < 100:
        return None
    try:
        tweets = json.loads(tweets_path.read_text())
    except Exception:
        return None
    if not tweets:
        return None

    n_mentions = 0
    samples: list[dict] = []
    months: Counter = Counter()

    for t in tweets:
        text = (t.get("text") or "")
        if match_re.search(text):
            n_mentions += 1
            ts = (t.get("created_at") or "")[:10]
            months[ts[:7]] += 1
            if len(samples) < 5:
                samples.append({
                    "id": str(t.get("id", "")),
                    "date": ts,
                    "text": text[:280],
                    "url": t.get("url") or "",
                })

    if n_mentions == 0:
        return None
    return {
        "handle": handle_dir.name,
        "n_tweets_total": len(tweets),
        "n_mentions": n_mentions,
        "mention_rate": round(n_mentions / len(tweets), 4) if tweets else 0,
        "months": dict(months),
        "samples": samples,
    }


def scan_corpus(target_handle: str, display_name: str | None) -> dict:
    print(f"Scanning {XALPHA_DATA} for mentions of @{target_handle}"
          + (f" / '{display_name}'" if display_name else "") + "...")
    match_re = build_match_re(target_handle, display_name)
    results: list[dict] = []
    n_scanned = 0
    for handle_dir in sorted(XALPHA_DATA.iterdir()):
        if not handle_dir.is_dir():
            continue
        if handle_dir.name == target_handle:
            continue
        n_scanned += 1
        out = scan_one_handle(handle_dir, target_handle, match_re)
        if out:
            results.append(out)
        if n_scanned % 1000 == 0:
            print(f"  scanned {n_scanned} handles, found {len(results)} mentioners")

    results.sort(key=lambda r: -r["n_mentions"])
    return {
        "handle": target_handle,
        "scanned_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "n_handles_scanned": n_scanned,
        "n_handles_mentioning": len(results),
        "total_mentions": sum(r["n_mentions"] for r in results),
        "mentioners": results,
    }


def derive_signals(scan: dict) -> dict:
    """Weak heuristics for booster-style accounts. Flagged for review, not asserted."""
    mentioners = scan["mentioners"]
    high_rate = sorted(
        [m for m in mentioners if m["mention_rate"] >= 0.05 and m["n_tweets_total"] >= 20],
        key=lambda m: -m["mention_rate"],
    )
    low_vol = sorted(
        [m for m in mentioners
         if m["n_tweets_total"] < 200 and m["n_mentions"] >= 5
         and m["mention_rate"] >= 0.03],
        key=lambda m: -m["mention_rate"],
    )
    return {
        "high_mention_rate": [
            {"handle": m["handle"], "mention_rate": m["mention_rate"],
             "n_mentions": m["n_mentions"], "n_tweets_total": m["n_tweets_total"]}
            for m in high_rate[:30]
        ],
        "low_volume_heavy_mention": [
            {"handle": m["handle"], "mention_rate": m["mention_rate"],
             "n_mentions": m["n_mentions"], "n_tweets_total": m["n_tweets_total"]}
            for m in low_vol[:30]
        ],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--handle", help="target X handle (defaults to meta.json)")
    p.add_argument("--report-id", required=True)
    args = p.parse_args(argv)
    if getattr(args, 'report_id', None):
        validate_report_id(args.report_id)
    meta = load_meta(args.report_id)
    handle = (args.handle or meta.get("handle") or "").strip().lstrip("@")
    if not handle:
        p.error("could not determine handle; pass --handle or run freeze first")
    display_name = meta.get("display_name") or None
    out_dir = report_prep(args.report_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    scan = scan_corpus(handle, display_name)
    signals = derive_signals(scan)
    out = {
        "handle": handle,
        "scanned_at": scan["scanned_at"],
        "n_handles_scanned": scan["n_handles_scanned"],
        "n_handles_mentioning": scan["n_handles_mentioning"],
        "total_mentions": scan["total_mentions"],
        "top_mentioners": [
            {"handle": m["handle"], "n_mentions": m["n_mentions"],
             "n_tweets_total": m["n_tweets_total"], "mention_rate": m["mention_rate"]}
            for m in scan["mentioners"][:50]
        ],
        "signals": signals,
        "samples_by_handle": {m["handle"]: m["samples"] for m in scan["mentioners"][:50]},
    }
    out_path = out_dir / "corpus_mentions.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"\n→ {out_path}")
    print(f"  {out['n_handles_mentioning']} handles mention @{handle}, "
          f"{out['total_mentions']} total mentions across "
          f"{out['n_handles_scanned']} scanned handles.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
