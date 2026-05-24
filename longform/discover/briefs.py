"""Generate the chapter briefs from discovered themes + account profile.

Reads:
  data/longform/<report_id>/meta.json
  data/longform/<report_id>/discover/themes.json
  data/longform/<report_id>/prep/profile.json
  data/longform/<report_id>/prep/corpus_mentions.json
  data/longform/<report_id>/prep/themes.json (joined cluster data)

Asks Sonnet to write the CHAPTER_SPEC dict that research/runner.py will
feed to its sub-agent. Always emits these scaffolding chapters where the
data supports them:
  profile           — always
  worldview         — only if there's a recurring framing term in the posts
  what_didnt_work   — only if the portfolio has ≥3 losing trades

Plus one chapter per theme from themes.json.

Writes (only if missing or --force):
  data/longform/<report_id>/discover/briefs.json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import (  # noqa: E402
    load_meta, report_discover, report_inputs, report_prep, validate_report_id
)

CLAUDE_MODEL = "claude-sonnet-4-6"


PROMPT = """You're writing the chapter briefs for a longform Economist-style report on @{handle} (display name "{display}"). Each brief is a directive to a separate research sub-agent that will then go pull 12-18 primary sources and write the chapter.

CRITICAL: briefs are RESEARCH DIRECTIVES, not chapter outlines. Tell the sub-agent what cluster of source material to read and what kind of chapter to write — but don't pre-decide what the chapter argues. Conclusions must emerge from the source material, not from us.

Don't include phrases like:
- "where the bull case has gone ahead of fundamentals" (a conclusion)
- "this cluster is now an inactive book" (a framing decision)
- "honest about misses" (an editorial directive)
- "End with..." (a structural prescription)
- "Note the user's hypothesis explicitly" (meta-narrative)

DO include:
- which tickers / which posts / which kb_sources to read
- which technical literature would be appropriate (filings, conference papers, sell-side)
- target word count
- what kind of voice (Economist, measured, smart non-specialist)

DATA — meta
{meta_block}

DATA — profile.json highlights
{profile_block}

DATA — corpus mentions
{corpus_block}

DATA — themes.json (the discovered thematic clusters with cluster sizes)
{themes_block}

OUTPUT — return ONLY a JSON object, no prose, no code fences.

The chapters must be in READING ORDER. Conventional order:
  1. profile (always)
  2. worldview (optional — only if the source supports it; see rule below)
  3-N. one chapter per theme from themes.json, in roughly the order that
       most-active first reads best
  last. what_didnt_work (only if portfolio has clear losers)

Schema:

{{
  "report_id": "{report_id}",
  "handle": "{handle}",
  "display_name": "{display}",
  "discovery_method": "llm",
  "chapters": [
    {{
      "slug": "<kebab-case>",
      "title": "<6-12 word title>",
      "n_words": <target word count, integer>,
      "theme_slug": <"theme-slug" if this chapter maps to a discovered theme, else null>,
      "brief": "<the directive, 100-200 words. Read like a research brief: what to look at, what to read, what kind of chapter, what voice. NOT a thesis statement.>"
    }}
  ]
}}

WORD-COUNT BUDGET (typical, adjust for the data):
- profile: 600-800w
- worldview: 600-800w (skip entirely if no recurring framing term)
- the deepest theme chapter: 1200-1500w
- other theme chapters: 700-1000w
- what_didnt_work: 400-600w

WORLDVIEW INCLUSION RULE
Include a worldview chapter ONLY if the dossiers + the bio suggest a coherent recurring framing — a term-of-art the account uses repeatedly to describe their selection method (examples: "functional monopolies", "supercycle", "capital flow funnel", "golden pocket"). If the account is just picking individual stocks without a unifying frame, omit worldview. Better to skip than to manufacture one.

WHAT_DIDNT_WORK INCLUSION RULE
Include only if portfolio_summary shows worst_return < -0.20 AND there are at least 3 losing trades visible in the dossier data. Otherwise skip.

THEME CHAPTERS
One chapter per theme from themes.json. Set theme_slug to match the theme's slug exactly. The cluster data (per-ticker representative posts, dossier summary) will be injected automatically into the research prompt — don't repeat it in the brief, just point at it.

VOICE / TONE for every brief
- Economist register: measured, allergic to hype, smart non-specialist reader
- they/them/their pronouns when referring to the account holder
- 12-18 primary sources per chapter (filings, conference papers, sell-side, IR pages)
- Avoid SEO blogs, CNBC summaries, aggregators
"""


def claude_call(prompt: str, *, timeout: float = 600.0) -> tuple[bool, str]:
    cmd = [
        "claude", "-p", prompt,
        "--model", CLAUDE_MODEL,
        "--permission-mode", "bypassPermissions",
        "--allowed-tools", "",
        "--effort", "medium",
        "--output-format", "text",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return False, err[:500] or f"exit_{proc.returncode}"
    return True, proc.stdout


def parse_json(raw: str) -> dict | None:
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```\s*$", "", s)
    try:
        return json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def validate_briefs(parsed: dict) -> tuple[bool, str]:
    if not isinstance(parsed, dict):
        return False, f"expected dict, got {type(parsed).__name__}"
    chapters = parsed.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        return False, "missing or empty 'chapters' list"
    if len(chapters) > 12:
        return False, f"too many chapters ({len(chapters)}); expected ≤10"
    seen: set[str] = set()
    for i, ch in enumerate(chapters):
        if not isinstance(ch, dict):
            return False, f"chapters[{i}] is not a dict"
        slug = ch.get("slug")
        if not isinstance(slug, str) or not re.match(r"^[a-z][a-z0-9_-]{0,39}$", slug):
            return False, f"chapters[{i}].slug invalid: {slug!r}"
        if slug in seen:
            return False, f"duplicate chapter slug: {slug!r}"
        seen.add(slug)
        if not isinstance(ch.get("title"), str) or len(ch["title"]) < 3:
            return False, f"chapters[{i}].title missing or too short"
        n_words = ch.get("n_words")
        if not isinstance(n_words, int) or not (200 <= n_words <= 3000):
            return False, f"chapters[{i}].n_words out of range: {n_words!r}"
        brief = ch.get("brief")
        if not isinstance(brief, str) or len(brief) < 60:
            return False, f"chapters[{i}].brief missing or too short"
        ts = ch.get("theme_slug")
        if ts is not None and not isinstance(ts, str):
            return False, f"chapters[{i}].theme_slug must be string or null"
    return True, ""


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)


def build_prompt(report_id: str) -> str:
    meta = load_meta(report_id)
    handle = meta.get("handle", "")
    display = meta.get("display_name") or handle
    if not handle:
        raise RuntimeError(
            f"meta.json missing handle for report_id={report_id}. "
            "Run prep/freeze.py first."
        )

    profile = json.loads((report_prep(report_id) / "profile.json").read_text())
    corpus_path = report_prep(report_id) / "corpus_mentions.json"
    corpus = json.loads(corpus_path.read_text()) if corpus_path.exists() else {}

    discover_themes_path = report_discover(report_id) / "themes.json"
    if not discover_themes_path.exists():
        raise FileNotFoundError(
            f"missing {discover_themes_path}. Run discover/themes.py first."
        )
    discover_themes = json.loads(discover_themes_path.read_text())

    # Joined cluster data (mention counts, dossier summary, etc.) — required
    # for sizing chapter lengths. Must exist, since briefs.py runs AFTER
    # prep/themes.py per the build.py sequencing. Direct invocation without
    # that step is a config error.
    joined_path = report_prep(report_id) / "themes.json"
    if not joined_path.exists():
        raise FileNotFoundError(
            f"missing {joined_path}. Run prep/themes.py before discover/briefs.py "
            f"(or use build.py, which sequences this for you)."
        )
    joined = json.loads(joined_path.read_text())
    discovered_slugs = {t["slug"] for t in discover_themes.get("themes", [])}
    joined_slugs = set((joined.get("chapters") or {}).keys())
    stale = discovered_slugs - joined_slugs
    if stale:
        raise RuntimeError(
            f"prep/themes.json is stale: missing themes {sorted(stale)} "
            f"that exist in discover/themes.json. Re-run prep/themes.py."
        )

    meta_block = json.dumps({
        "handle": handle,
        "display_name": display,
        "bio": meta.get("bio"),
        "snapshot_date": meta.get("snapshot_date"),
    }, indent=2)

    profile_block = json.dumps({
        "tweet_count": profile.get("tweet_volume", {}).get("total"),
        "date_range": [profile.get("tweet_volume", {}).get("first_tweet_date"),
                       profile.get("tweet_volume", {}).get("last_tweet_date")],
        "monthly_volume": profile.get("tweet_volume", {}).get("by_month"),
        "portfolio_summary": profile.get("portfolio_summary"),
        "top_dossiers_first8": profile.get("top_dossiers", [])[:8],
        "top_replied_to": profile.get("network", {}).get("top_replied_to", [])[:6],
    }, indent=2)

    corpus_block = json.dumps({
        "n_handles_mentioning": corpus.get("n_handles_mentioning", 0),
        "total_mentions": corpus.get("total_mentions", 0),
        "n_handles_scanned": corpus.get("n_handles_scanned", 0),
        "top_mentioners": (corpus.get("top_mentioners") or [])[:8],
        "signals": corpus.get("signals", {}),
    }, indent=2)

    # Themes with cluster sizes
    theme_rows = []
    for t in discover_themes.get("themes", []):
        slug = t.get("slug")
        joined_ch = (joined.get("chapters") or {}).get(slug, {})
        theme_rows.append({
            "slug": slug,
            "title": t.get("title"),
            "tickers": t.get("tickers"),
            "rationale": t.get("rationale"),
            "n_calls_total": joined_ch.get("n_calls_total"),
            "n_calls_ytd": joined_ch.get("n_calls_ytd"),
            "n_calls_last_4w": joined_ch.get("n_calls_last_4w"),
        })
    themes_block = json.dumps({"themes": theme_rows}, indent=2)

    return PROMPT.format(
        handle=handle, display=display, report_id=report_id,
        meta_block=meta_block, profile_block=profile_block,
        corpus_block=corpus_block, themes_block=themes_block,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report-id", required=True)
    p.add_argument("--force", action="store_true",
                   help="Overwrite briefs.json even if it exists")
    p.add_argument("--plan-only", action="store_true",
                   help="Print prompt without calling Sonnet")
    args = p.parse_args(argv)
    if getattr(args, 'report_id', None):
        validate_report_id(args.report_id)
    out_dir = report_discover(args.report_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "briefs.json"
    if out_path.exists() and not args.force and not args.plan_only:
        print(f"  briefs.json exists at {out_path} — skipping. --force to overwrite.")
        return 0

    prompt = build_prompt(args.report_id)
    if args.plan_only:
        print(prompt)
        return 0

    print(f"[discover/briefs] calling Sonnet 4.6 (medium effort, no tools)…")
    ok, raw = claude_call(prompt)
    if not ok:
        print(f"  FAILED: {raw[:300]}", file=sys.stderr)
        return 1
    parsed = parse_json(raw)
    if parsed is None:
        print(f"  could not parse JSON. First 400 chars:\n{raw[:400]}", file=sys.stderr)
        return 1

    ok_v, reason = validate_briefs(parsed)
    if not ok_v:
        print(f"  briefs validation failed: {reason}\n  raw output:\n{json.dumps(parsed, indent=2)[:800]}",
              file=sys.stderr)
        return 1

    # Cross-check theme coverage: every discovered theme must be referenced
    # by exactly one chapter, and every theme_slug must exist in themes.
    discover_themes = json.loads(
        (report_discover(args.report_id) / "themes.json").read_text())
    valid_themes = {t["slug"] for t in discover_themes.get("themes", [])}
    chapter_theme_counts: dict[str, int] = {}
    for ch in parsed["chapters"]:
        ts = ch.get("theme_slug")
        if ts is None:
            continue
        if ts not in valid_themes:
            print(f"  briefs validation failed: chapter {ch['slug']} references "
                  f"unknown theme_slug={ts!r}; known: {sorted(valid_themes)}",
                  file=sys.stderr)
            return 1
        chapter_theme_counts[ts] = chapter_theme_counts.get(ts, 0) + 1

    uncovered = sorted(valid_themes - set(chapter_theme_counts.keys()))
    if uncovered:
        print(f"  briefs validation failed: themes not covered by any chapter: "
              f"{uncovered}", file=sys.stderr)
        return 1
    duplicated = {k: v for k, v in chapter_theme_counts.items() if v > 1}
    if duplicated:
        print(f"  briefs validation failed: themes covered by multiple chapters: "
              f"{duplicated}", file=sys.stderr)
        return 1

    atomic_write(out_path, json.dumps(parsed, indent=2, ensure_ascii=False))
    n = len(parsed.get("chapters") or [])
    print(f"  → {out_path} ({n} chapters)")
    for ch in parsed.get("chapters") or []:
        theme = ch.get("theme_slug") or "—"
        print(f"    {ch.get('slug',''):25s} {ch.get('title',''):40s} "
              f"~{ch.get('n_words')}w  theme={theme}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
