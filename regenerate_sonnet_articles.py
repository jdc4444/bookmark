#!/usr/bin/env python3
"""Batch regenerate Sonnet articles for bookmarks from the past 12 months.

Filter rules:
  * Skip bookmarks older than 12 months.
  * Include bookmarks with no cached article.
  * Include bookmarks whose cached article was produced by another backend
    (deepseek, ollama, etc.) — we want everything on Sonnet.
  * Include bookmarks whose cached Sonnet article is older than the
    bookmark's most recent enrichment (new thread/reply/quote context
    captured since the article was written).
  * Otherwise skip.

Runs `article_generator.py --prefer claude` per bookmark with bounded
parallelism. The CLI uses the user's Claude Max plan, so no metered cost.
"""

import concurrent.futures
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / "data" / "x-bookmarks.json"
ARTICLES_DIR = ROOT / "data" / "articles"
LOG = ROOT / "data" / f"regenerate_sonnet_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
CONCURRENCY = 4
PER_TWEET_TIMEOUT = 600  # seconds
LOOKBACK_DAYS = 365


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def article_meta(bm_id):
    p = ARTICLES_DIR / f"article_{bm_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


CLAUDE_BACKENDS = {"claude-sonnet-subagent", "claude"}


def classify(bm):
    """Returns (reason, should_regenerate)."""
    art = article_meta(bm["id"])
    if not art:
        return ("missing", True)
    backend = art.get("backend") or art.get("model") or "unknown"
    if backend not in CLAUDE_BACKENDS:
        return (f"backend={backend}", True)
    art_ts = parse_iso(art.get("generated_at"))
    enrich_ts = parse_iso(bm.get("enriched_at"))
    if art_ts and enrich_ts and enrich_ts > art_ts:
        return ("stale", True)
    return ("fresh", False)


def log(msg):
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n"
    with LOG.open("a") as f:
        f.write(line)


def gen_one(bm_id):
    proc = subprocess.run(
        [sys.executable, str(ROOT / "article_generator.py"),
         "--bookmark-id", str(bm_id), "--prefer", "claude"],
        capture_output=True, text=True, timeout=PER_TWEET_TIMEOUT,
        cwd=str(ROOT),
    )
    return bm_id, proc.returncode, (proc.stderr or "")[-400:]


def main():
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    bms = json.loads(ARCHIVE.read_text())["bookmarks"]
    todo = []
    skipped_old = skipped_fresh = 0
    for bm in bms:
        ts = parse_iso(bm.get("created_at"))
        if not ts or ts < cutoff:
            skipped_old += 1
            continue
        reason, do_it = classify(bm)
        if do_it:
            todo.append((bm["id"], reason))
        else:
            skipped_fresh += 1

    log(f"start: todo={len(todo)} skipped_old={skipped_old} skipped_fresh={skipped_fresh} concurrency={CONCURRENCY}")
    by_reason = {}
    for _, r in todo:
        by_reason[r] = by_reason.get(r, 0) + 1
    log(f"breakdown: {by_reason}")

    ok = fail = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        future_map = {ex.submit(gen_one, bm_id): (bm_id, reason) for bm_id, reason in todo}
        for fut in concurrent.futures.as_completed(future_map):
            bm_id, reason = future_map[fut]
            try:
                _, rc, err = fut.result()
                if rc == 0:
                    ok += 1
                    log(f"ok {bm_id} ({reason})")
                else:
                    fail += 1
                    log(f"fail {bm_id} ({reason}) rc={rc} err={err.strip()[:200]}")
            except Exception as e:
                fail += 1
                log(f"exc {bm_id} ({reason}) {type(e).__name__}: {e}")

    log(f"done: ok={ok} fail={fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
