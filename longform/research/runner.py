"""Per-chapter research + writing. One Sonnet sub-agent call per chapter,
using the same harness as Bookmark's article_generator.py — `claude -p` with
Agent + WebSearch + WebFetch enabled, --effort high, sonnet-4-6.

Inputs (frozen, no LLM calls):
- data/longform/<report_id>/meta.json       — handle, display_name, etc.
- data/longform/<report_id>/discover/briefs.json — chapter list, in order
- data/longform/<report_id>/prep/{profile,themes,corpus_mentions,reconstruction}.json
- data/longform/<report_id>/inputs/{bookmark_modules,kb_sources,xalpha_engine}/

Output: data/longform/<report_id>/prep/chapters/<slug>.json with the chapter
body, sources, and the entities the chapter mentions.

Usage:
    python3 longform/research/runner.py <slug> --report-id <id>
    python3 longform/research/runner.py --all --report-id <id>
    python3 longform/research/runner.py --plan-only <slug> --report-id <id>
    python3 longform/research/runner.py --list --report-id <id>
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import (  # noqa: E402
    load_meta, report_chapters_dir, report_discover, report_inputs, report_prep, validate_report_id
)

CLAUDE_MODEL = "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Bind paths once we know the report_id (set in main()).
# ---------------------------------------------------------------------------
_paths_bound = False
REPORT_ID = ""
INPUTS = Path()
PREP = Path()
CHAPTERS = Path()
DISCOVER = Path()
META: dict = {}


def bind_paths(report_id: str) -> None:
    global _paths_bound, REPORT_ID, INPUTS, PREP, CHAPTERS, DISCOVER, META
    REPORT_ID = report_id
    INPUTS = report_inputs(report_id)
    PREP = report_prep(report_id)
    CHAPTERS = report_chapters_dir(report_id)
    DISCOVER = report_discover(report_id)
    CHAPTERS.mkdir(parents=True, exist_ok=True)
    META = load_meta(report_id) or {}
    _paths_bound = True


def ensure_bound() -> None:
    if not _paths_bound:
        raise RuntimeError(
            "runner globals not bound. Call bind_paths(report_id) before "
            "load_chapter_spec / build_prompt / run_chapter."
        )


def load_chapter_spec() -> dict:
    """Authoritative chapter list lives at <report>/discover/briefs.json."""
    ensure_bound()
    p = DISCOVER / "briefs.json"
    if not p.exists():
        raise FileNotFoundError(
            f"missing {p}. Run discover/briefs.py to generate it, or write "
            f"it by hand. Schema: {{'chapters': [{{'slug','title','n_words',"
            f"'theme_slug','brief'}}]}}."
        )
    data = json.loads(p.read_text())
    out: dict[str, dict] = {}
    for ch in data.get("chapters") or []:
        slug = ch.get("slug")
        if not slug:
            continue
        out[slug] = {
            "title": ch.get("title", slug),
            "n_words": int(ch.get("n_words") or 700),
            "theme_slug": ch.get("theme_slug"),
            "brief": ch.get("brief", ""),
        }
    if not out:
        raise ValueError(f"no chapters found in {p}")
    return out


# ---------------------------------------------------------------------------
# Same harness as Bookmark/article_generator.py:243.
# ---------------------------------------------------------------------------
def claude_call(prompt: str, *,
                allowed_tools: str = "Agent,WebSearch,WebFetch",
                effort: str = "high",
                timeout: float = 3000.0) -> tuple[bool, str]:
    cmd = [
        "claude", "-p", prompt,
        "--model", CLAUDE_MODEL,
        "--permission-mode", "bypassPermissions",
        "--allowed-tools", allowed_tools,
        "--effort", effort,
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


def parse_json_response(raw: str) -> dict | None:
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


# ---------------------------------------------------------------------------
# Known-entity injection: pre-cached Bookmark ticker + poster modules so the
# sub-agent doesn't re-research what we already paid for.
# ---------------------------------------------------------------------------
def load_known_entities(spec: dict) -> str:
    handle = META.get("handle", "")
    parts: list[str] = []

    poster = INPUTS / "bookmark_modules" / f"poster_{handle}.json"
    if poster.exists() and handle:
        m = json.loads(poster.read_text())
        parts.append(f"\n@{handle} — {m.get('display_name','')}\n  {(m.get('blurb') or '').strip()}")
        if m.get("recent"):
            parts.append(f"  Recent: {m['recent'].strip()}")

    theme_slug = spec.get("theme_slug")
    tickers: list[str] = []
    if theme_slug:
        themes = json.loads((PREP / "themes.json").read_text())
        ch = themes["chapters"].get(theme_slug)
        if ch:
            tickers = ch["tickers"]

    for t in tickers:
        m_path = INPUTS / "bookmark_modules" / f"ticker_{t.lower()}.json"
        if not m_path.exists():
            continue
        try:
            mod = json.loads(m_path.read_text())
        except Exception:
            continue
        name = mod.get("name") or t
        exch = mod.get("exchange") or ""
        parts.append(f"\n${t} — {name}{f' ({exch})' if exch else ''}\n  {(mod.get('blurb') or '').strip()}")
        if mod.get("recent"):
            parts.append(f"  Recent: {mod['recent'].strip()}")

    if not parts:
        return ""
    return ("KNOWN ENTITIES (already in our knowledge base — reference these "
            "directly, don't re-research)\n" + "\n".join(parts) + "\n")


def load_kb_sources_for_chapter(slug: str) -> str:
    """Surface kb/sources entries by tag matching. The tag-set is intentionally
    short — over-listing fin_v1 priors crowds out the sub-agent's own
    research. Slugs we don't have a tag-set for return empty."""
    src_dir = INPUTS / "kb_sources"
    if not src_dir.exists():
        return ""
    spec_def = load_chapter_spec().get(slug, {})
    theme_slug = spec_def.get("theme_slug")
    interesting_tags = {
        "photonics": {"capex", "infrastructure", "tsmc"},
        "hbm_packaging": {"capex", "tsmc", "packaging", "hbm"},
        "ai_infra": {"capex", "us", "infrastructure", "neocloud"},
        "hyperscaler_sovereign": {"capex", "anthropic", "us", "infrastructure"},
        "worldview": {"anthropic", "agi", "tsmc"},
    }.get(theme_slug or slug, set())
    if not interesting_tags:
        return ""
    rows: list[str] = []
    for p in sorted(src_dir.glob("*.json"))[:80]:
        try:
            s = json.loads(p.read_text())
        except Exception:
            continue
        tags = set(s.get("tags") or [])
        if interesting_tags & tags:
            rows.append(f"- [{s.get('title','')}]({s.get('url','')}) "
                        f"— {s.get('date','')} ({s.get('category','')})")
    if not rows:
        return ""
    return ("FIN_V1 KB PRIMARY SOURCES (already-vetted citations — feel free "
            "to use)\n" + "\n".join(rows[:30]) + "\n")


# ---------------------------------------------------------------------------
# Prompt construction. Every account-specific string is templated from
# meta.json / profile.json / corpus_mentions.json — no hardcodes.
# ---------------------------------------------------------------------------
def build_prompt(slug: str, spec: dict) -> str:
    ensure_bound()
    profile = json.loads((PREP / "profile.json").read_text())
    corpus = json.loads((PREP / "corpus_mentions.json").read_text())
    recon_path = PREP / "reconstruction.json"
    recon = json.loads(recon_path.read_text()) if recon_path.exists() else {}

    handle = META.get("handle", profile.get("handle", ""))
    display = META.get("display_name", profile.get("display_name", handle))
    tweet_count = profile.get("tweet_volume", {}).get("total")
    date_range = (
        profile.get("tweet_volume", {}).get("first_tweet_date"),
        profile.get("tweet_volume", {}).get("last_tweet_date"),
    )

    theme_block = ""
    if spec.get("theme_slug"):
        themes = json.loads((PREP / "themes.json").read_text())
        ch = themes["chapters"].get(spec["theme_slug"], {})
        theme_block = (
            "CLUSTER DATA (from prep/themes.py — drives chapter focus)\n"
            f"Tickers: {', '.join(ch.get('tickers') or [])}\n"
            f"Total calls in cluster: {ch.get('n_calls_total')}; "
            f"YTD: {ch.get('n_calls_ytd')}; last 4 weeks: {ch.get('n_calls_last_4w')}\n"
            f"Monthly mention counts: {json.dumps(ch.get('monthly_mention_counts') or {})}\n"
            f"Per-ticker representative posts: see prep/themes.json -> "
            f"chapters.{spec['theme_slug']}.per_ticker_representatives\n"
            f"Dossier summary (xalpha tier/score): "
            f"{json.dumps(ch.get('dossier_summary') or [], indent=None)}\n"
        )

    known = load_known_entities(spec)
    kb_block = load_kb_sources_for_chapter(slug)

    recon_line = ""
    if recon:
        bi = recon.get("best_interpretable", {}) or {}
        if bi.get("final") is not None:
            recon_line = (
                f"\n- prep/reconstruction.json — sidebar metric: a sensible "
                f"portfolio reconstruction lands ~{bi['final']:.0f}% on the "
                f"frozen window. Mention only if there's a natural place; "
                f"do NOT make this the focus.\n"
            )

    return f"""You are writing one chapter of a longform Economist-style report on
@{handle} (display name "{display}"). The chapter is grounded in a frozen
snapshot of {tweet_count or 'their'} tweets ({date_range[0] or '?'} → {date_range[1] or '?'}),
with primary sources to be pulled live during research.

This is **chapter "{slug}"** ("{spec['title']}"). Target length: ~{spec['n_words']} words.

EDITORIAL STYLE
- Economist register: measured, informed, allergic to hype. Smart reader, not specialist.
- Flowing paragraphs separated by blank lines. Use markdown `## subhead` for internal divisions if the chapter is long; no h1.
- **bold** for the key claim or pivotal noun, *italic* for terms-of-art and titles. Sparingly.
- When you name a publicly-traded company, append the ticker on first mention in the paragraph: "Sumitomo Electric (5802.T)", "Coherent (COHR)", "Taiwan Semiconductor Manufacturing (TSM)". Use the canonical exchange-suffixed ticker for non-US listings. If private, use just the name.
- Direct quotes from {display} are welcome — quote as much as makes sense. Use `> ` block-quote markdown for any quote >15 words.
- Use **they / them / their** when referring to {display} (gender-neutral by default; the source data does not disclose).

WHAT THIS CHAPTER COVERS
{spec['brief']}

RESEARCH APPROACH
Before writing, dispatch the Agent tool (subagent_type="general-purpose") to do the research. Tell it: pull 12–18 *primary* sources for this chapter — filings (SEC, EDGAR, regional regulators), the company's own IR pages, technical conference papers (OFC, JEDEC, IEEE), reputable sell-side initiations, semianalysis, Stratechery / The Information / FT reporting where it adds detail. Avoid SEO blogs, CNBC summaries, and aggregators. Prefer documents the reader can pull up themselves.

Have it return a structured digest with quotes and URLs. Then write the chapter from that digest, weaving in the cluster data and {display}'s own posts where they make the strongest claim.

OUTPUT — return ONLY a JSON object, no prose outside it, no code fences.

CRITICAL JSON ESCAPING: every double-quote inside a string MUST be escaped as \\". When quoting {display}, prefer curly quotes (e.g. "they're buying X to make Y work") or escape with \\". NEVER use raw straight double-quotes inside a string — they break the JSON.

{{
  "slug": "{slug}",
  "title": "{spec['title']}",
  "lede": "<2–3 sentence opening that orients a smart reader>",
  "body": "<the chapter body in markdown — flowing paragraphs separated by blank lines, optional ## subheads, target {spec['n_words']} words. Use **bold** sparingly, *italic* for terms, > for block-quotes from {display}. Embed brief inline definitions for any non-obvious jargon.>",
  "tickers": ["<every public-company ticker that appears in your body, bare ticker no $>"],
  "companies": [
    {{"name": "<canonical company name>", "ticker": "<ticker if publicly traded, else null>"}}
  ],
  "concepts": [
    {{"key": "<kebab-case>", "term": "<display name>", "definition": "<2–4 sentence plain-language explanation>"}}
  ],
  "sources": [
    {{"url": "<source url>", "title": "<source title>", "note": "<one-line why this source>"}}
  ],
  "subject_quotes_used": [
    {{"tweet_id": "<id from frozen tweets.json if quoted>", "url": "<x.com url>", "context": "<what claim it supports>"}}
  ]
}}

CONTEXT — frozen data files in this snapshot
- prep/profile.json — bio, tweet volume, follow graph, reply graph, top dossiers
- prep/themes.json — chapter clusters with per-ticker representative posts
- prep/corpus_mentions.json — accounts in the wider xalpha corpus that mention @{handle}, with samples{recon_line}
- inputs/xalpha/{handle}/tweets.json — the full tweet corpus, frozen

PROFILE.JSON HIGHLIGHTS
{json.dumps({
    'bio': profile.get('bio'),
    'display_name': profile.get('display_name'),
    'tweet_count': profile.get('tweet_volume', {}).get('total'),
    'date_range': [profile.get('tweet_volume', {}).get('first_tweet_date'),
                   profile.get('tweet_volume', {}).get('last_tweet_date')],
    'monthly_volume': profile.get('tweet_volume', {}).get('by_month'),
    'top_replied_to': profile.get('network', {}).get('top_replied_to', [])[:8],
    'reposted_from': profile.get('network', {}).get('reposted_from'),
    'portfolio_summary': profile.get('portfolio_summary'),
    'top_dossiers_first6': profile.get('top_dossiers', [])[:6],
}, indent=2)}

CORPUS MENTION HIGHLIGHTS
- Total mentioners: {corpus.get('n_handles_mentioning', 0)}, total mentions {corpus.get('total_mentions', 0)} across {corpus.get('n_handles_scanned', 0)} scanned handles.
- Top mentioners: {", ".join("@" + m['handle'] for m in (corpus.get('top_mentioners') or [])[:8])}
- Full samples in prep/corpus_mentions.json (samples_by_handle).

{theme_block}
{known}
{kb_block}
"""


def write_chapter(slug: str, parsed: dict, backend: str) -> Path:
    parsed["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    parsed["backend"] = backend
    out_path = CHAPTERS / f"{slug}.json"
    out_path.write_text(json.dumps(parsed, indent=2, ensure_ascii=False))
    return out_path


def run_chapter(slug: str, plan_only: bool = False) -> int:
    spec = load_chapter_spec().get(slug)
    if not spec:
        print(f"unknown chapter slug: {slug}\nknown: {', '.join(load_chapter_spec())}",
              file=sys.stderr)
        return 2
    prompt = build_prompt(slug, spec)
    if plan_only:
        print(prompt)
        return 0
    print(f"[{slug}] calling Sonnet 4.6 (effort=high, Agent+WebSearch+WebFetch enabled)…")
    ok, raw = claude_call(prompt)
    if not ok:
        print(f"[{slug}] FAILED: {raw[:300]}", file=sys.stderr)
        return 1
    parsed = parse_json_response(raw)
    if parsed is None:
        retry = prompt + (
            "\n\nIMPORTANT: your previous attempt did not produce valid JSON — "
            "almost certainly an unescaped \" inside a string. Try again. "
            "Every double-quote inside a string MUST be escaped as \\\". "
            "Prefer curly-quote “…” pairs when quoting speech."
        )
        ok, raw = claude_call(retry)
        parsed = parse_json_response(raw)
    if parsed is None:
        print(f"[{slug}] could not parse JSON. First 400 chars:\n{raw[:400]}",
              file=sys.stderr)
        return 1
    out_path = write_chapter(slug, parsed, "claude-sonnet-subagent")
    print(f"[{slug}] → {out_path} "
          f"(body {len(parsed.get('body',''))} chars, "
          f"{len(parsed.get('sources') or [])} sources)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("slug", nargs="?", help="chapter slug, or omit with --all")
    p.add_argument("--report-id", required=True)
    p.add_argument("--all", action="store_true", help="run every chapter sequentially")
    p.add_argument("--plan-only", action="store_true",
                   help="print the prompt instead of calling Sonnet")
    p.add_argument("--list", action="store_true", help="list known chapter slugs")
    args = p.parse_args(argv)
    if getattr(args, 'report_id', None):
        validate_report_id(args.report_id)
    bind_paths(args.report_id)

    if args.list:
        for slug, spec in load_chapter_spec().items():
            print(f"  {slug:25s} {spec['title']} (~{spec['n_words']}w)")
        return 0
    if args.all:
        for slug in load_chapter_spec():
            rc = run_chapter(slug, plan_only=args.plan_only)
            if rc != 0:
                return rc
        return 0
    if not args.slug:
        p.error("must pass slug or --all")
    return run_chapter(args.slug, plan_only=args.plan_only)


if __name__ == "__main__":
    sys.exit(main())
