"""Propose 3-7 thematic clusters for a longform report.

Reads:
  data/longform/<report_id>/meta.json
  data/longform/<report_id>/prep/profile.json    (top dossiers, portfolio)
  data/longform/<report_id>/inputs/xalpha/<handle>/dossiers.json
  data/longform/<report_id>/inputs/xalpha/<handle>/calls_llm.json (sample)

Asks Sonnet to cluster the dossier tickers into themes — sector chokepoints,
trade styles, whatever the data supports — and return a {themes: [...]}
JSON shaped exactly like the manual file at
data/longform/aleabit/discover/themes.json.

Writes (only if missing or --force):
  data/longform/<report_id>/discover/themes.json

The output is intentionally editable: a human is expected to review it
before research stage runs. The script does not run if themes.json already
exists; pass --force to overwrite.
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


PROMPT = """You're proposing a chapter structure for a longform Economist-style report on the X account @{handle} (display name "{display}"). The report will be ~5,000–10,000 words across 4–8 thematic chapters plus profile/worldview/what-didn't-work scaffolding chapters.

Your job is just the THEMATIC CLUSTERING step. Look at the data below and propose 3–7 thematic clusters. Each cluster groups tickers that belong together — a sector chokepoint, a trade style, an industry vertical. The goal is that each cluster could anchor a single chapter where a smart reader gets a coherent picture of one slice of the account's thesis.

Some accounts have a single dominant theme (e.g. all photonics) plus a couple of adjacent ones; others spread across many sectors. Let the data drive the count and the names.

DATA — top dossier tickers (xalpha-ranked by score, with tier and mention count)
{dossier_block}

DATA — portfolio summary
{portfolio_block}

DATA — sample of the highest-conviction trade calls (from calls_llm.json)
{calls_block}

DATA — bio
{bio_block}

OUTPUT — return ONLY a JSON object, no prose, no code fences. Schema:

{{
  "report_id": "{report_id}",
  "handle": "{handle}",
  "display_name": "{display}",
  "discovery_method": "llm",
  "themes": [
    {{
      "slug": "<kebab-case, ≤30 chars, descriptive of the cluster>",
      "title": "<3–6 word noun phrase, e.g. 'The photonics supercycle'>",
      "tickers": ["<ticker>", "<ticker>", ...],
      "rationale": "<1 sentence: why these tickers belong together in this account's portfolio>"
    }}
  ]
}}

RULES
- Each theme slug must be unique and filesystem-safe (lowercase, _ and - allowed only).
- Tickers in a theme should appear in the dossier data (don't invent).
- A ticker can appear in at most ONE theme — pick its dominant fit.
- Reserve a "bear_book" or "what_didnt_work" theme ONLY if the dossiers/portfolio show clear losers worth grouping. If the account is mostly winners, skip this.
- 4–6 themes is typical. 3 is fine if the account is narrow. Avoid more than 7 — chapters get thin.
- If the account doesn't have a recurring term-of-art framing, that's OK; the worldview chapter is opt-in (handled by briefs.py later).
- DON'T include profile/worldview/what_didnt_work as themes — they're scaffolding chapters, not thematic clusters.
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


def validate_themes(parsed: dict) -> tuple[bool, str]:
    """Sanity-check the LLM output before persisting. Returns (ok, reason)."""
    if not isinstance(parsed, dict):
        return False, f"expected dict, got {type(parsed).__name__}"
    themes = parsed.get("themes")
    if not isinstance(themes, list) or not themes:
        return False, "missing or empty 'themes' list"
    if len(themes) > 10:
        return False, f"too many themes ({len(themes)}); expected 3-7"
    seen_slugs: set[str] = set()
    seen_tickers: set[str] = set()
    for i, t in enumerate(themes):
        if not isinstance(t, dict):
            return False, f"themes[{i}] is not a dict"
        slug = t.get("slug")
        if not isinstance(slug, str) or not re.match(r"^[a-z][a-z0-9_-]{0,29}$", slug):
            return False, f"themes[{i}].slug invalid: {slug!r}"
        if slug in seen_slugs:
            return False, f"duplicate slug: {slug!r}"
        seen_slugs.add(slug)
        if not isinstance(t.get("title"), str) or len(t["title"]) < 3:
            return False, f"themes[{i}].title missing or too short"
        tickers = t.get("tickers")
        if not isinstance(tickers, list) or not tickers:
            return False, f"themes[{i}].tickers missing or empty"
        for tk in tickers:
            if not isinstance(tk, str):
                return False, f"themes[{i}] has non-string ticker: {tk!r}"
            # Normalise the way prep/themes.py does — uppercase, strip $ —
            # so 'NVDA' and '$nvda' don't slip through duplicate detection.
            norm = tk.strip().lstrip("$").upper()
            if not norm:
                return False, f"themes[{i}] has empty ticker"
            if norm in seen_tickers:
                return False, f"ticker {tk!r} (normalises to {norm!r}) appears in multiple themes"
            seen_tickers.add(norm)
    return True, ""


def atomic_write(path: Path, content: str) -> None:
    """Write to a sibling .tmp file and rename, so we never leave a half-
    written file at the canonical path on crash."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.replace(path)


def build_prompt(report_id: str, handle: str, display: str) -> str:
    profile = json.loads((report_prep(report_id) / "profile.json").read_text())
    in_dir = report_inputs(report_id) / "xalpha" / handle
    dossiers = json.loads((in_dir / "dossiers.json").read_text())
    calls = json.loads((in_dir / "calls_llm.json").read_text())

    # Top 40 dossiers by score
    top = sorted(dossiers, key=lambda d: -d.get("score", 0))[:40]
    dossier_lines = []
    for d in top:
        ret = (d.get("return") or {}).get("pct")
        ret_str = f"{ret*100:+.0f}%" if isinstance(ret, (int, float)) else "n/a"
        dossier_lines.append(
            f"  {d.get('ticker','?'):8s} tier={d.get('tier') or '?':4s} "
            f"score={d.get('score',0):3d} mentions={d.get('total_mentions',0):3d} "
            f"return={ret_str}"
        )
    dossier_block = "\n".join(dossier_lines) if dossier_lines else "(no dossier entries)"

    portfolio_block = json.dumps(profile.get("portfolio_summary") or {}, indent=2)

    # Sample 12 high-conviction trade calls
    high_conv = [
        c for c in calls
        if c.get("is_trade_call")
        and any(
            (e.get("conviction") if isinstance(e, dict) else None) == "high"
            for e in (c.get("tickers") or [])
        )
    ][:12]
    sample_lines = []
    for c in high_conv:
        tks = []
        for e in (c.get("tickers") or []):
            if isinstance(e, dict) and e.get("ticker"):
                tks.append(e["ticker"])
        sample_lines.append(
            f"  - row_type={c.get('row_type')} tickers={tks} "
            f"horizon={c.get('horizon')}"
        )
    calls_block = "\n".join(sample_lines) if sample_lines else "(no high-conviction calls)"

    bio_block = profile.get("bio", "(no bio)")

    return PROMPT.format(
        handle=handle, display=display, report_id=report_id,
        dossier_block=dossier_block, portfolio_block=portfolio_block,
        calls_block=calls_block, bio_block=bio_block,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report-id", required=True)
    p.add_argument("--force", action="store_true",
                   help="Overwrite themes.json even if it exists")
    p.add_argument("--plan-only", action="store_true",
                   help="Print prompt without calling Sonnet")
    args = p.parse_args(argv)
    if getattr(args, 'report_id', None):
        validate_report_id(args.report_id)
    out_dir = report_discover(args.report_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "themes.json"
    if out_path.exists() and not args.force and not args.plan_only:
        print(f"  themes.json exists at {out_path} — skipping. --force to overwrite.")
        return 0

    meta = load_meta(args.report_id)
    handle = meta.get("handle")
    display = meta.get("display_name") or handle
    if not handle:
        p.error("meta.json missing handle. Run freeze stage first.")

    prompt = build_prompt(args.report_id, handle, display)
    if args.plan_only:
        print(prompt)
        return 0

    print(f"[discover/themes] calling Sonnet 4.6 (medium effort, no tools)…")
    ok, raw = claude_call(prompt)
    if not ok:
        print(f"  FAILED: {raw[:300]}", file=sys.stderr)
        return 1
    parsed = parse_json(raw)
    if parsed is None:
        print(f"  could not parse JSON. First 400 chars:\n{raw[:400]}", file=sys.stderr)
        return 1

    ok_v, reason = validate_themes(parsed)
    if not ok_v:
        print(f"  themes validation failed: {reason}\n  raw output:\n{json.dumps(parsed, indent=2)[:800]}",
              file=sys.stderr)
        return 1

    atomic_write(out_path, json.dumps(parsed, indent=2, ensure_ascii=False))
    n = len(parsed.get("themes") or [])
    print(f"  → {out_path} ({n} themes)")
    for t in parsed.get("themes") or []:
        print(f"    {t.get('slug',''):25s} {t.get('title','')} "
              f"[{len(t.get('tickers') or [])} tickers]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
