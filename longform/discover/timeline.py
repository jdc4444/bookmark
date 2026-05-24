"""Extract dated stock predictions from the subject's tweets.

Reads tweets.json from the frozen account dir, filters to the past N days
(default 180 — the user asked for 6 months), pre-screens to tweets that
contain timing language ("by Q4", "next year", "within 12 months", "expect
… by", etc.) and ticker mentions, then sends batches to codex (gpt-5.5)
for structured extraction.

Output: data/longform/<report_id>/prep/timeline.json with entries:
  {tweet_id, date, url, tickers[], prediction, target_date, target_metric,
   target_value, conviction, status}

The codex CLI is used per CLAUDE.md (paid LLM ⇒ user CLI, never SDK).

Usage:
  python3 longform/discover/timeline.py --report-id aleabit
  python3 longform/discover/timeline.py --report-id aleabit --window-days 365
  python3 longform/discover/timeline.py --report-id aleabit --plan-only
  python3 longform/discover/timeline.py --report-id aleabit --force
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import (  # noqa: E402
    load_meta, report_inputs, report_prep, validate_report_id,
)

CODEX = "/Applications/Codex.app/Contents/Resources/codex"
CODEX_MODEL = "gpt-5.5"

# Tweets that contain at least one of these are candidates for extraction.
# Regex is intentionally generous; codex filters out non-predictions.
TIMING_PATTERNS = re.compile(
    r"\b("
    r"by\s+(?:Q[1-4]|FY|end\s+of|next|early|mid|late|H[12])"
    r"|within\s+(?:\d+|a|the\s+next)\s+(?:months?|weeks?|quarters?|years?)"
    r"|next\s+(?:quarter|month|year|earnings)"
    r"|in\s+(?:\d+|a)\s+(?:months?|weeks?|quarters?|years?)"
    r"|(?:should|will|expect|targeting|target)\s+(?:hit|reach|trade|be|see|do|print)"
    r"|(?:price\s+target|PT)\s+(?:of\s+)?\$?\d"
    r"|to\s+(?:hit|reach|trade\s+at)\s+\$?\d"
    r"|\d+%\s+(?:in|by|within)\s+\d"
    r"|\bFY?20[2-3]\d\b"
    r"|Q[1-4]\s*['′ ]?20[2-3]\d"
    r")",
    re.IGNORECASE,
)

# We pre-extract the unique tickers actually held in the account so codex
# only sees relevant ticker mentions. Built from the dossier ticker list.

EXTRACTION_PROMPT = """You are extracting dated stock predictions from a small batch of tweets by an X-finance account. Each tweet has an id, date, and text. For every tweet that contains a SPECIFIC, DATED, FUTURE prediction about a public company's stock — return one entry. Skip tweets that are pure commentary, retrospectives ("X was up…"), generic bullishness without a date, or open positions without an outcome target.

A "specific dated prediction" requires:
  • a ticker (or unambiguous company name)
  • a directional outcome (price target / revenue figure / earnings beat / event)
  • a TIME WINDOW the prediction is supposed to resolve in (Q1 26, by FY27, within 12 months, "next earnings", end of 2027, etc.)

Output ONLY a JSON array, no prose, no code fences. Schema for each entry:

[
  {{
    "tweet_id": "<id from input>",
    "date": "<YYYY-MM-DD from input>",
    "tickers": ["<TICKER>", ...],
    "prediction": "<one-sentence summary of the predicted outcome>",
    "target_date": "<YYYY-MM-DD or YYYY-Qn or YYYY (best estimate)>",
    "target_metric": "<price | revenue | earnings | catalyst_event | other>",
    "target_value": "<the actual target text — '$1B FY26 revenue', 'SEK 100', 'beat by 15%', '1.6T module ramp'>",
    "conviction": "<high | med | low (from tweet language)>",
    "stance": "<long | short | neutral>"
  }}
]

If the batch contains zero qualifying tweets, return [].

INPUT BATCH (JSON array of tweets):
{batch}

Return ONLY the JSON array — no preamble, no comments.
"""


def codex_call(prompt: str, *, timeout: float = 600.0) -> tuple[bool, str]:
    """Call codex exec in non-interactive mode. Per CLAUDE.md the codex CLI
    is the right entry point for paid OpenAI inference (uses the user's
    Codex Pro subscription, not metered API credits)."""
    if not Path(CODEX).exists():
        return False, f"codex binary not found at {CODEX}"
    cmd = [
        CODEX, "exec",
        "--model", CODEX_MODEL,
        "--skip-git-repo-check",
        "--sandbox", "read-only",
        "-",
    ]
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "timeout"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return False, err[:500] or f"exit_{proc.returncode}"
    return True, proc.stdout


def parse_json_array(raw: str) -> list[dict] | None:
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```\s*$", "", s)
    # codex prepends some chatter — find the first '[' and last ']'.
    start = s.find("[")
    end = s.rfind("]")
    if start < 0 or end <= start:
        return None
    chunk = s[start:end+1]
    try:
        out = json.loads(chunk)
        return out if isinstance(out, list) else None
    except Exception:
        return None


def candidate_tweets(tweets: list[dict], window_start: str,
                     known_tickers: set[str]) -> list[dict]:
    """Pre-filter: timing language present, ticker mention present, in window."""
    ticker_re = re.compile(
        r"\$?(" + "|".join(re.escape(t) for t in sorted(known_tickers, key=len, reverse=True)) + r")\b"
    ) if known_tickers else None
    out: list[dict] = []
    for t in tweets:
        date = (t.get("created_at") or "")[:10]
        if not date or date < window_start:
            continue
        text = t.get("text") or ""
        if not text:
            continue
        if not TIMING_PATTERNS.search(text):
            continue
        if ticker_re and not ticker_re.search(text):
            continue
        out.append({
            "id": str(t.get("id", "")),
            "date": date,
            "text": text[:1000],  # cap to keep batches manageable
        })
    return out


def chunk(lst: list, n: int) -> list[list]:
    return [lst[i:i+n] for i in range(0, len(lst), n)]


def validate_predictions(items: list) -> list[dict]:
    """Return the subset of LLM output that actually meets schema."""
    valid: list[dict] = []
    for x in items:
        if not isinstance(x, dict):
            continue
        if not x.get("tweet_id") or not x.get("date"):
            continue
        if not isinstance(x.get("tickers"), list) or not x["tickers"]:
            continue
        if not isinstance(x.get("prediction"), str) or len(x["prediction"]) < 8:
            continue
        valid.append({
            "tweet_id": str(x["tweet_id"]),
            "date": str(x["date"])[:10],
            "tickers": [str(t).strip().lstrip("$").upper() for t in x["tickers"] if t],
            "prediction": x["prediction"].strip(),
            "target_date": (x.get("target_date") or "").strip() or None,
            "target_metric": (x.get("target_metric") or "").strip() or None,
            "target_value": (x.get("target_value") or "").strip() or None,
            "conviction": (x.get("conviction") or "").strip() or None,
            "stance": (x.get("stance") or "").strip() or None,
        })
    return valid


def attach_urls(predictions: list[dict], tweets_by_id: dict[str, dict]) -> list[dict]:
    for p in predictions:
        tw = tweets_by_id.get(p["tweet_id"])
        if tw:
            p["url"] = tw.get("url") or ""
        else:
            p["url"] = ""
    return predictions


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--report-id", required=True)
    p.add_argument("--window-days", type=int, default=180,
                   help="Look back this many days from snapshot_date (default: 180)")
    p.add_argument("--batch-size", type=int, default=15,
                   help="Tweets per codex call (default: 15)")
    p.add_argument("--max-batches", type=int, default=None,
                   help="Cap total batches; useful for first-pass / debugging")
    p.add_argument("--plan-only", action="store_true",
                   help="Pre-filter and print what would be sent; no codex calls")
    p.add_argument("--force", action="store_true",
                   help="Overwrite timeline.json even if it exists")
    args = p.parse_args(argv)
    validate_report_id(args.report_id)

    out_path = report_prep(args.report_id) / "timeline.json"
    if out_path.exists() and not args.force and not args.plan_only:
        print(f"  timeline.json exists at {out_path} — skipping. --force to regenerate.")
        return 0

    meta = load_meta(args.report_id)
    handle = meta.get("handle")
    if not handle:
        p.error("meta.json missing handle. Run prep/freeze.py first.")

    # Read frozen tweets + dossiers.
    in_dir = report_inputs(args.report_id) / "xalpha" / handle
    tweets = json.loads((in_dir / "tweets.json").read_text())
    dossiers = json.loads((in_dir / "dossiers.json").read_text())
    known_tickers = {d["ticker"] for d in dossiers if d.get("ticker")}
    print(f"  loaded {len(tweets)} tweets and {len(known_tickers)} dossier tickers")

    # Window
    snap = meta.get("snapshot_date") or datetime.now(timezone.utc).date().isoformat()
    snap_dt = datetime.strptime(snap, "%Y-%m-%d")
    start_dt = snap_dt - timedelta(days=args.window_days)
    window_start = start_dt.strftime("%Y-%m-%d")
    print(f"  window: {window_start} → {snap} ({args.window_days} days)")

    # Pre-filter
    candidates = candidate_tweets(tweets, window_start, known_tickers)
    print(f"  pre-filtered {len(candidates)} candidate tweets with timing language")
    if not candidates:
        timeline = {
            "report_id": args.report_id, "handle": handle,
            "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "window": {"start": window_start, "end": snap},
            "n_candidates": 0,
            "predictions": [],
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(timeline, indent=2))
        print(f"  → {out_path} (0 predictions)")
        return 0

    if args.plan_only:
        print(f"  --plan-only: would send {len(candidates)} tweets in "
              f"{(len(candidates)+args.batch_size-1)//args.batch_size} batches "
              f"of {args.batch_size}.")
        for c in candidates[:5]:
            print(f"    [{c['date']}] {c['text'][:120]}")
        if len(candidates) > 5:
            print(f"    ... and {len(candidates)-5} more")
        return 0

    # Batched codex extraction
    tweets_by_id = {str(t.get("id")): t for t in tweets}
    batches = chunk(candidates, args.batch_size)
    if args.max_batches:
        batches = batches[: args.max_batches]
    print(f"  sending {len(batches)} batches to {CODEX_MODEL} via codex CLI...")

    all_preds: list[dict] = []
    for i, batch in enumerate(batches, 1):
        prompt = EXTRACTION_PROMPT.format(batch=json.dumps(batch, indent=2))
        ok, raw = codex_call(prompt)
        if not ok:
            print(f"    batch {i}: FAILED ({raw[:200]})", file=sys.stderr)
            continue
        items = parse_json_array(raw)
        if items is None:
            print(f"    batch {i}: could not parse JSON array; first 300 chars:\n{raw[:300]}",
                  file=sys.stderr)
            continue
        valid = validate_predictions(items)
        all_preds.extend(valid)
        print(f"    batch {i}/{len(batches)}: {len(valid)}/{len(items)} valid predictions")

    all_preds = attach_urls(all_preds, tweets_by_id)
    # Dedupe by (tweet_id, prediction)
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for pr in all_preds:
        key = (pr["tweet_id"], pr["prediction"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(pr)
    deduped.sort(key=lambda x: x["date"])

    # Per-ticker rollup for the frontend
    by_ticker: dict[str, list[dict]] = {}
    for pr in deduped:
        for t in pr["tickers"]:
            by_ticker.setdefault(t, []).append(pr)

    timeline = {
        "report_id": args.report_id, "handle": handle,
        "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "window": {"start": window_start, "end": snap},
        "n_candidates": len(candidates),
        "n_batches": len(batches),
        "predictions": deduped,
        "by_ticker": {t: prs for t, prs in by_ticker.items()},
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(timeline, indent=2, ensure_ascii=False))
    print(f"\n  → {out_path}")
    print(f"  {len(deduped)} unique predictions across {len(by_ticker)} tickers")
    if deduped[:3]:
        print("  sample:")
        for pr in deduped[:3]:
            print(f"    [{pr['date']}] {','.join(pr['tickers'])} → "
                  f"{pr['prediction'][:80]} (target: {pr.get('target_date') or '?'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
