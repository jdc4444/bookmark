"""Backfill `media` for bookmarks that don't have it, by visiting each tweet's
status page in Safari and re-extracting.

Bookmarks rolled off the bookmarks timeline don't get a fresh image scrape during
safari-import — this fills the gap one tweet at a time. Deleted/unavailable
tweets are detected quickly (short timeout) and marked `media_unavailable: true`
so we don't keep retrying them.

Rate-limit aware:
- Throttles to a configurable per-tweet floor.
- Takes a short break every N tweets and a longer break on consecutive failures.
- Detects X.com's "Something went wrong"/"Try again"/"Retry" rate-limit pages
  and pauses for `--cooldown-seconds` before resuming.
- Verifies a non-front Safari window has an x.com tab before starting.
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
)


RATE_LIMIT_TITLE_PATTERNS = [
    re.compile(r"something went wrong", re.IGNORECASE),
    re.compile(r"rate limit", re.IGNORECASE),
    re.compile(r"try again", re.IGNORECASE),
    re.compile(r"retry", re.IGNORECASE),
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive", default="data/x-bookmarks.json")
    p.add_argument("--load-timeout", type=float, default=10.0,
                   help="Max seconds to wait for a status page to load.")
    p.add_argument("--per-tweet-pause", type=float, default=1.0,
                   help="Pause after URL change before first extraction.")
    p.add_argument("--throttle", type=float, default=4.0,
                   help="Floor on total seconds per tweet (sleep extra if processing was faster).")
    p.add_argument("--break-every", type=int, default=30,
                   help="Take a break after every N tweets.")
    p.add_argument("--break-seconds", type=float, default=45.0,
                   help="Length of regular break.")
    p.add_argument("--cooldown-seconds", type=float, default=300.0,
                   help="Pause after a rate-limit detection (default 5 min).")
    p.add_argument("--max-cooldowns", type=int, default=5,
                   help="Stop after this many consecutive cooldowns.")
    p.add_argument("--save-every", type=int, default=20,
                   help="Save partial progress every N tweets.")
    p.add_argument("--limit", type=int, default=0,
                   help="Stop after N bookmarks. 0 = no limit.")
    p.add_argument("--ids", default="",
                   help="Comma-separated list of bookmark IDs to target (overrides default 'no media' filter).")
    p.add_argument("--retry-unavailable", action="store_true",
                   help="Retry tweets previously marked media_unavailable.")
    p.add_argument("--skip-precheck", action="store_true",
                   help="Skip Safari topology check (use only if you know it's set up).")
    return p.parse_args()


def ensure_x_in_non_front() -> str | None:
    """Make sure a non-front Safari window has an x.com tab.

    If x.com is in the front window, swap windows so a non-x window is in front.
    Returns None on success, or an error message.
    """
    script = '''
tell application "Safari"
  -- check if any non-front window already has x.com
  set hasIt to false
  repeat with w in windows
    if (index of w) is not 1 then
      repeat with t in tabs of w
        try
          if (URL of t as text) contains "x.com" then
            set hasIt to true
            exit repeat
          end if
        end try
      end repeat
      if hasIt then exit repeat
    end if
  end repeat
  if hasIt then return "ok"

  -- check if FRONT has x.com; if so, find a non-x window and bring it forward
  set frontHasX to false
  if (count of windows) > 0 then
    repeat with t in tabs of window 1
      try
        if (URL of t as text) contains "x.com" then
          set frontHasX to true
          exit repeat
        end if
      end try
    end repeat
  end if
  if frontHasX then
    repeat with w in windows
      if (index of w) is not 1 then
        set hasX to false
        repeat with t in tabs of w
          try
            if (URL of t as text) contains "x.com" then
              set hasX to true
              exit repeat
            end if
          end try
        end repeat
        if not hasX then
          set index of w to 1
          return "swapped"
        end if
      end if
    end repeat
  end if

  return "no_x_tab"
end tell
'''
    try:
        result = subprocess.check_output(
            ["osascript", "-e", script], text=True, timeout=10
        ).strip()
    except subprocess.SubprocessError as exc:
        return f"could not query Safari: {exc}"
    if result in ("ok", "swapped"):
        return None
    return ("No x.com tab in any non-front Safari window. Open Safari, make "
            "sure two windows exist, and load any x.com URL in the non-front one.")


def precheck_safari() -> str | None:
    """Backwards-compat alias kept for clarity. Auto-fixes when possible."""
    return ensure_x_in_non_front()


def extract_status(tweet_id: str, *, load_timeout: float, per_tweet_pause: float,
                   media_settle: float = 1.5) -> dict | None:
    """Return the status page payload for tweet_id, or None on timeout/failure.

    Polls until the page is loaded with at least one article. Once loaded, if
    the target article has no media yet (lazy-load not finished), wait
    `media_settle` seconds and re-extract.
    """
    js = SAFARI_STATUS_CONTEXT_JS.replace("__TARGET_ID__", json.dumps(str(tweet_id)))
    needle = f"/status/{tweet_id}"
    deadline = time.time() + load_timeout
    time.sleep(per_tweet_pause)

    def try_extract() -> dict | None:
        try:
            raw = safari_do_javascript(js, timeout=6, target_url_contains=needle)
            return json.loads(raw)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                json.JSONDecodeError):
            return None

    page = None
    while time.time() < deadline:
        candidate = try_extract()
        if candidate and needle in (candidate.get("href") or "") \
                and candidate.get("article_count", 0) > 0:
            page = candidate
            break
        time.sleep(0.5)
    if page is None:
        return None

    target = next((a for a in (page.get("articles") or [])
                   if str(a.get("id") or "") == str(tweet_id)), None)
    if target and not target.get("media"):
        time.sleep(media_settle)
        candidate = try_extract()
        if candidate and candidate.get("article_count", 0) > 0:
            page = candidate
    return page


def detect_rate_limit() -> bool:
    """Probe the front-most non-front Safari x.com tab for rate-limit indicators."""
    js = """
(function(){
  const title = document.title || '';
  const body = (document.body && document.body.innerText || '').slice(0, 4000);
  return JSON.stringify({title: title, snippet: body});
})();
"""
    try:
        raw = safari_do_javascript(js, timeout=6, target_url_contains="x.com")
        info = json.loads(raw)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            json.JSONDecodeError):
        return False
    haystack = (info.get("title", "") + "\n" + info.get("snippet", "")).strip()
    if not haystack:
        return False
    for pattern in RATE_LIMIT_TITLE_PATTERNS:
        if pattern.search(haystack):
            return True
    return False


def find_target_article(page: dict, tweet_id: str) -> dict | None:
    articles = page.get("articles") or []
    for article in articles:
        if str(article.get("id") or "") == str(tweet_id):
            return article
    return articles[0] if articles else None


def main() -> int:
    args = parse_args()
    archive_path = Path(args.archive)
    payload = json.loads(archive_path.read_text())
    bookmarks = payload.get("bookmarks") or []

    if not args.skip_precheck:
        msg = precheck_safari()
        if msg:
            print(f"Pre-check failed: {msg}", file=sys.stderr)
            return 2

    explicit_ids = {s.strip() for s in (args.ids or "").split(",") if s.strip()}
    targets = []
    for bookmark in bookmarks:
        bid = str(bookmark.get("id") or "")
        if not bid:
            continue
        if explicit_ids:
            if bid in explicit_ids:
                targets.append(bookmark)
            continue
        if bookmark.get("media"):
            continue
        if bookmark.get("media_unavailable") and not args.retry_unavailable:
            continue
        targets.append(bookmark)

    print(f"Backfilling media for {len(targets)} bookmarks "
          f"(out of {len(bookmarks)} total).", flush=True)
    if args.limit:
        targets = targets[: args.limit]
        print(f"Limited to {len(targets)}.", flush=True)

    by_id = {str(b.get("id")): b for b in bookmarks if b.get("id")}

    filled = 0
    unavailable = 0
    consecutive_failures = 0
    cooldowns = 0
    started = time.time()

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
            consecutive_failures += 1
            if consecutive_failures >= 5:
                msg = precheck_safari()
                if msg:
                    print(f"  Safari topology lost: {msg}  Stopping.", flush=True)
                    save()
                    return 3
            continue

        page = extract_status(tweet_id, load_timeout=args.load_timeout,
                              per_tweet_pause=args.per_tweet_pause)
        if page is None:
            # Could be deleted/private OR rate-limited. Probe.
            if detect_rate_limit():
                cooldowns += 1
                print(f"  [{index}/{len(targets)}] RATE LIMIT detected. "
                      f"Cooldown #{cooldowns} for {args.cooldown_seconds:.0f}s...",
                      flush=True)
                save()
                if cooldowns >= args.max_cooldowns:
                    print(f"  Reached max cooldowns ({args.max_cooldowns}). Stopping.",
                          flush=True)
                    return 4
                time.sleep(args.cooldown_seconds)
                # Retry this tweet after cooldown
                try:
                    safari_set_url(url, target_url_contains="x.com")
                except subprocess.CalledProcessError:
                    pass
                page = extract_status(tweet_id, load_timeout=args.load_timeout,
                                      per_tweet_pause=args.per_tweet_pause)

        if page is None:
            bookmark["media_unavailable"] = True
            bookmark["media_checked_at"] = (
                dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
            )
            unavailable += 1
            consecutive_failures += 1
        else:
            article = find_target_article(page, tweet_id)
            media = (article or {}).get("media") or []
            if media:
                bookmark["media"] = media
                bookmark.pop("media_unavailable", None)
                filled += 1
            else:
                bookmark["media"] = []
                bookmark["media_unavailable"] = True
                unavailable += 1
            bookmark["media_checked_at"] = (
                dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
            )
            consecutive_failures = 0

        if index % 25 == 0 or index == len(targets):
            elapsed = time.time() - started
            rate = index / max(0.1, elapsed)
            remaining = (len(targets) - index) / max(0.01, rate)
            print(f"  [{index}/{len(targets)}] filled={filled} "
                  f"unavailable={unavailable} cooldowns={cooldowns}  "
                  f"elapsed={elapsed:.0f}s eta={remaining:.0f}s", flush=True)

        if args.save_every and index % args.save_every == 0:
            save()

        # Throttle: ensure we spend at least `throttle` seconds per tweet
        spent = time.time() - tweet_started
        if spent < args.throttle:
            time.sleep(args.throttle - spent)

        # Periodic break to avoid sustained pressure on x.com
        if args.break_every and index % args.break_every == 0 and index < len(targets):
            print(f"  -- break {args.break_seconds:.0f}s --", flush=True)
            time.sleep(args.break_seconds)
            # Re-check Safari topology after long pause; auto-fix if user moved windows
            msg = ensure_x_in_non_front()
            if msg:
                print(f"  Safari setup lost during break: {msg}", flush=True)
                save()
                return 5

    save()

    total_with_media = sum(1 for b in bookmarks if b.get("media"))
    print(f"\nDone. filled={filled} unavailable={unavailable} "
          f"cooldowns={cooldowns}  total_with_media={total_with_media}/{len(bookmarks)}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
