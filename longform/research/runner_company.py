#!/usr/bin/env python3
"""Per-chapter Sonnet sub-agent runner for company-shaped longform reports.

Companion to longform/research/runner.py (which is profile-shaped). This
variant loads the briefs from data/longform/companies/<report_id>/discover/briefs.json,
pulls the referenced prep/* files into the prompt, and dispatches one
claude -p invocation per chapter.

Usage:
    python3 longform/research/runner_company.py --report-id unitika --chapter snapshot
    python3 longform/research/runner_company.py --report-id unitika --all
    python3 longform/research/runner_company.py --report-id unitika --list

Constraints:
- Uses claude -p CLI (NEVER the API).
- Model defaults to claude-sonnet-4-6; --effort high gives more research depth.
- Saves output to data/longform/companies/<report_id>/prep/chapters/<slug>.json.
- Will skip chapters already written unless --force.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path("/Users/alphaone/Documents/Code/Bookmark")


def company_dir(report_id: str) -> Path:
    return ROOT / "data" / "longform" / "companies" / report_id


def load_brief(report_id: str, chapter_slug: str) -> tuple[dict, dict]:
    briefs_path = company_dir(report_id) / "discover" / "briefs.json"
    briefs = json.loads(briefs_path.read_text())
    for ch in briefs.get("chapters", []):
        if ch.get("slug") == chapter_slug:
            return briefs, ch
    raise ValueError(f"chapter {chapter_slug!r} not found in briefs.json")


def load_prep_files(report_id: str, file_paths: list[str]) -> dict[str, str]:
    """Load prep files referenced by a brief. Each entry is a path
    relative to the company's directory root (so 'snapshot.json' →
    data/longform/companies/<id>/prep/snapshot.json, but
    'inputs/news/foo.md' → data/longform/companies/<id>/inputs/news/foo.md)."""
    out: dict[str, str] = {}
    base = company_dir(report_id)
    for p in file_paths or []:
        # default: look in prep/
        if "/" in p:
            full = base / p
        else:
            full = base / "prep" / p
        if not full.exists():
            # also try inputs/
            alt = base / "inputs" / p
            if alt.exists():
                full = alt
        if full.exists():
            out[p] = full.read_text(encoding="utf-8")
    return out


def build_prompt(briefs: dict, chapter: dict, prep_files: dict[str, str]) -> str:
    style = briefs.get("voice_and_style", "")
    title = briefs.get("report_title", "")
    subtitle = briefs.get("report_subtitle", "")
    snapshot_date = briefs.get("frozen_snapshot_date", "")
    target_words = chapter.get("target_words", 800)
    directives = chapter.get("directives", [])
    chapter_title = chapter.get("title", "")
    chapter_slug = chapter.get("slug", "")

    prep_section = ""
    for fname, content in prep_files.items():
        prep_section += f"\n\n=== {fname} ===\n{content}\n"

    extra = chapter.get("may_need_extra_research", "")
    extra_note = ""
    if extra:
        extra_note = (
            f"\n\nThis chapter is flagged as needing external research: {extra}. "
            f"Use the WebSearch and WebFetch tools to fill in any gaps before writing. "
            f"Cite specific URLs in your output."
        )

    return f"""You are writing one chapter of a longform company dossier on {title}. {subtitle}

Frozen snapshot date: {snapshot_date}. Treat anything later than this as "after the frozen window."

## Voice and style
{style}

## Chapter to write
**Title:** {chapter_title}
**Slug:** {chapter_slug}
**Target length:** {target_words} words (give or take 15%)

## Research directives
{chr(10).join(f"- {d}" for d in directives)}
{extra_note}

## Prep data (this is what the previous pipeline stages assembled — use it heavily)
{prep_section}

## Output format
Write the chapter body as flowing markdown prose. Use H2 (`##`) subheaders to break up sections. Inline citations as `[Source name](URL)` where you can attribute a specific fact. Use markdown tables when comparing numerics across periods or peers. Quote-blockquote (`>`) management statements verbatim when you can.

Do NOT include the chapter title H1 — the renderer adds it. Start directly with the first paragraph or a `##` subheader.

Output ONLY the chapter body. No preamble, no meta-commentary, no "Here is the chapter:". Start with prose.

Length target: about {target_words} words. Quality and concrete-fact density matter more than hitting the exact word count."""


def invoke_sonnet(prompt: str, timeout: int = 1200) -> tuple[bool, str, str]:
    """Run claude -p with the prompt. Returns (ok, stdout, stderr)."""
    cmd = [
        "claude", "-p", prompt,
        "--model", "claude-sonnet-4-6",
        "--permission-mode", "bypassPermissions",
        "--allowed-tools", "WebSearch,WebFetch",
        "--output-format", "text",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return False, "", f"timeout after {timeout}s: {exc}"
    ok = proc.returncode == 0 and bool(proc.stdout.strip())
    return ok, proc.stdout, proc.stderr


_PREAMBLE_KEYWORDS = (
    "i have", "now i have", "let me", "here is", "i'll write", "i will write",
    "writing the chapter", "writing this chapter", "ok, here", "okay, here",
    "alright,", "i'm now",
)


def _strip_preamble(body: str) -> str:
    """Sonnet occasionally leaks a thinking-out-loud line before the chapter
    starts despite the brief saying not to. Strip anything before the first
    real prose marker (a `##` subhead OR a sentence that doesn't look like
    self-talk)."""
    body = body.strip()
    if not body:
        return body
    # If the body has a `---` separator inside the first ~500 chars, take
    # everything after it — that's the model breaking out of its scratch pad.
    head = body[:600]
    if "---" in head:
        idx = body.index("---")
        rest = body[idx + 3:].lstrip("\n").lstrip()
        if rest:
            return rest
    # If the first line is self-talk (starts with "I have", "Now I", "Let me",
    # etc.) and isn't a heading, drop it and any blank line that follows.
    first_line, _, remainder = body.partition("\n")
    if (not first_line.startswith(("#", "*", "|", ">", "1.", "-"))
            and any(kw in first_line.lower() for kw in _PREAMBLE_KEYWORDS)):
        return remainder.lstrip()
    return body


def write_chapter_output(report_id: str, chapter: dict, body: str) -> Path:
    slug = chapter["slug"]
    out_path = company_dir(report_id) / "prep" / "chapters" / f"{slug}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    body = _strip_preamble(body)
    payload = {
        "slug": slug,
        "title": chapter.get("title"),
        "order": chapter.get("order"),
        "target_words": chapter.get("target_words"),
        "word_count": len(body.split()),
        "body": body,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "model": "claude-sonnet-4-6",
        "via": "claude -p (CLI)",
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    return out_path


def write_one(report_id: str, chapter_slug: str, force: bool = False) -> bool:
    out_path = company_dir(report_id) / "prep" / "chapters" / f"{chapter_slug}.json"
    if out_path.exists() and not force:
        print(f"skip {chapter_slug}: already written ({out_path})")
        return True
    briefs, chapter = load_brief(report_id, chapter_slug)
    prep_files = load_prep_files(report_id, chapter.get("use_prep_files", []))
    prompt = build_prompt(briefs, chapter, prep_files)
    print(f"writing {chapter_slug} ({chapter.get('target_words', 800)}w target)...")
    ok, stdout, stderr = invoke_sonnet(prompt)
    if not ok:
        print(f"FAILED {chapter_slug}: {stderr[:300]}")
        return False
    out_path = write_chapter_output(report_id, chapter, stdout.strip())
    print(f"ok {chapter_slug}: {len(stdout.split())} words → {out_path}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-id", required=True)
    parser.add_argument("--chapter", help="single chapter slug")
    parser.add_argument("--all", action="store_true", help="write all chapters in brief order")
    parser.add_argument("--list", action="store_true", help="list chapters from brief")
    parser.add_argument("--force", action="store_true", help="overwrite existing chapter outputs")
    args = parser.parse_args(argv)

    briefs_path = company_dir(args.report_id) / "discover" / "briefs.json"
    if not briefs_path.exists():
        print(f"no briefs at {briefs_path}", file=sys.stderr)
        return 1
    briefs = json.loads(briefs_path.read_text())

    if args.list:
        for ch in briefs.get("chapters", []):
            print(f"  [{ch.get('order'):>2}] {ch.get('slug'):<24}  ({ch.get('target_words')}w)  {ch.get('title')}")
        return 0

    if args.chapter:
        return 0 if write_one(args.report_id, args.chapter, force=args.force) else 1

    if args.all:
        failures = []
        for ch in sorted(briefs.get("chapters", []), key=lambda c: c.get("order", 999)):
            slug = ch.get("slug")
            if not write_one(args.report_id, slug, force=args.force):
                failures.append(slug)
        if failures:
            print(f"failed: {failures}")
            return 1
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
