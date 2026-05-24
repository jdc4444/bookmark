"""Backfill full text for bookmarks whose text was truncated when first scraped.

Twitter cuts long tweets at ~280 chars in the bookmarks timeline (with a
"Show more" link). Bookmarks that have rolled off the timeline can no longer
be re-scraped from there — we have to visit each tweet's status page, which
shows the full text by default.

Detection heuristic: text length >= 270 OR explicit `text_truncated: true` flag.
We also skip bookmarks already marked `text_full: true` from a previous run.

Rate-limit aware: same throttle/break/cooldown patterns as backfill_media.
"""

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from archive_io import write_archive_locked
from x_bookmark_reporter import (
    SAFARI_STATUS_CONTEXT_JS,
    safari_do_javascript,
    safari_set_url,
    clean_text,
)

from backfill_media import (
    ensure_x_in_non_front,
    extract_status,
    find_target_article,
)


def likely_truncated(bookmark: dict) -> bool:
    if bookmark.get("text_full"):
        return False
    if bookmark.get("text_truncated"):
        return True
    # Strongest signal: the scrape captured the literal "Show more" link the
    # truncated tweet renders in the timeline. Catches short-but-cut tweets the
    # length heuristic misses.
    raw = ((bookmark.get("raw") or {}).get("raw_text") or "")
    if isinstance(raw, str) and "Show more" in raw:
        return True
    text = (bookmark.get("text") or "").strip()
    if len(text) < 260:
        return False
    # Heuristic: a complete tweet usually ends with a sentence-final character.
    return text[-1] not in '.!?"\')]…'


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive", default="data/x-bookmarks.json")
    p.add_argument("--load-timeout", type=float, default=10.0)
    p.add_argument("--per-tweet-pause", type=float, default=2.0,
                   help="Wait before extracting (lets Show-more click settle).")
    p.add_argument("--throttle", type=float, default=4.0)
    p.add_argument("--break-every", type=int, default=30)
    p.add_argument("--break-seconds", type=float, default=45.0)
    p.add_argument("--cooldown-seconds", type=float, default=300.0)
    p.add_argument("--max-cooldowns", type=int, default=3)
    p.add_argument("--save-every", type=int, default=20)
    p.add_argument("--limit", type=int, default=0)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    archive_path = Path(args.archive)
    payload = json.loads(archive_path.read_text())
    bookmarks = payload.get("bookmarks") or []

    msg = ensure_x_in_non_front()
    if msg:
        print(f"Pre-check failed: {msg}", file=sys.stderr)
        return 2

    targets = [b for b in bookmarks if b.get("id") and likely_truncated(b)]
    print(f"Truncated candidates: {len(targets)} of {len(bookmarks)} total.",
          flush=True)
    if args.limit:
        targets = targets[: args.limit]
        print(f"Limited to {len(targets)}.", flush=True)
    if not targets:
        return 0

    by_id = {str(b.get("id")): b for b in bookmarks if b.get("id")}
    started = time.time()
    updated = 0
    no_change = 0
    failed = 0
    cooldowns = 0

    def save():
        payload["bookmarks"] = list(by_id.values())
        write_archive_locked(archive_path, payload)

    for index, bookmark in enumerate(targets, start=1):
        tweet_started = time.time()
        tweet_id = str(bookmark["id"])
        url = bookmark.get("url") or f"https://x.com/i/web/status/{tweet_id}"
        try:
            safari_set_url(url, target_url_contains="x.com")
        except subprocess.CalledProcessError as exc:
            print(f"  [{index}/{len(targets)}] {tweet_id}  set_url failed: {exc}",
                  flush=True)
            failed += 1
            continue

        page = extract_status(tweet_id, load_timeout=args.load_timeout,
                              per_tweet_pause=args.per_tweet_pause,
                              media_settle=2.0)
        if page is None:
            failed += 1
            bookmark["text_check_failed"] = True
        else:
            article = find_target_article(page, tweet_id)
            new_text = clean_text((article or {}).get("text") or "")
            old_text = (bookmark.get("text") or "").strip()
            if new_text and len(new_text) > len(old_text) + 10:
                bookmark["text"] = new_text
                bookmark["text_full"] = True
                bookmark.pop("text_truncated", None)
                updated += 1
            else:
                # Mark as checked so we don't reprocess each run
                bookmark["text_full"] = True
                no_change += 1
        bookmark["text_checked_at"] = (
            dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        )

        if index % 25 == 0 or index == len(targets):
            elapsed = time.time() - started
            rate = index / max(0.1, elapsed)
            eta = (len(targets) - index) / max(0.01, rate)
            print(f"  [{index}/{len(targets)}] updated={updated} "
                  f"no_change={no_change} failed={failed}  "
                  f"elapsed={elapsed:.0f}s eta={eta:.0f}s", flush=True)

        if args.save_every and index % args.save_every == 0:
            save()

        spent = time.time() - tweet_started
        if spent < args.throttle:
            time.sleep(args.throttle - spent)

        if args.break_every and index % args.break_every == 0 and index < len(targets):
            print(f"  -- break {args.break_seconds:.0f}s --", flush=True)
            time.sleep(args.break_seconds)
            msg = ensure_x_in_non_front()
            if msg:
                print(f"  Safari setup lost: {msg}", flush=True)
                save()
                return 5

    save()
    print(f"\nDone. updated={updated} no_change={no_change} failed={failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
