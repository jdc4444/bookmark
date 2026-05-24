"""Assemble per-chapter JSONs into one report.json.

Reads:
- data/longform/<report_id>/meta.json            (title, subtitle, byline)
- data/longform/<report_id>/discover/briefs.json (chapter ORDER)
- data/longform/<report_id>/prep/chapters/*.json (chapter bodies)

Writes:
- data/longform/<report_id>/reports/report.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _paths import (  # noqa: E402
    load_meta, report_chapters_dir, report_discover, report_inputs,
    report_prep, report_reports_dir, validate_report_id,
)


def word_count(s: str) -> int:
    return len(re.findall(r"\b\w+\b", s or ""))


def normalize_url(u: str) -> str:
    if not u:
        return ""
    u = u.strip()
    u = re.sub(r"#.*$", "", u)
    u = re.sub(r"/\?", "?", u)
    return u.rstrip("/")


def merge_sources(chapters: list[dict]) -> list[dict]:
    by_url: dict[str, dict] = {}
    for ch in chapters:
        for s in (ch.get("sources") or []):
            u = normalize_url(s.get("url") or "")
            if not u:
                continue
            cur = by_url.get(u)
            if not cur:
                by_url[u] = {
                    "url": u, "title": s.get("title", ""), "note": s.get("note", ""),
                    "first_used_in": ch["slug"],
                }
            else:
                if len(s.get("title") or "") > len(cur.get("title") or ""):
                    cur["title"] = s["title"]
    return sorted(by_url.values(), key=lambda r: r["first_used_in"])


def extract_blockquotes_for_ticker(ticker: str, chapters: list[dict]) -> list[dict]:
    """Pull every markdown block-quote from the chapter bodies whose preceding
    or following paragraph mentions this ticker. Used by the Companies
    appendix so each company card carries a Serenity quote in their voice."""
    if not ticker:
        return []
    pat_word = re.compile(rf"(?:\${re.escape(ticker)}\b|\b{re.escape(ticker)}\b)", re.IGNORECASE)
    out: list[dict] = []
    for ch in chapters:
        body = ch.get("body") or ""
        # Walk paragraphs; collect (chapter_slug, quote_text) when a > block
        # is adjacent to a paragraph that mentions the ticker.
        paras = body.split("\n\n")
        for i, para in enumerate(paras):
            if not para.strip().startswith(">"):
                continue
            quote = " ".join(line.lstrip(">").strip()
                              for line in para.splitlines() if line.strip().startswith(">"))
            if not quote.strip():
                continue
            window = " ".join([
                paras[i-1] if i-1 >= 0 else "",
                para,
                paras[i+1] if i+1 < len(paras) else "",
            ])
            if pat_word.search(window):
                out.append({
                    "chapter": ch["slug"],
                    "quote": quote,
                })
    # Dedupe by quote text, keep the first occurrence's chapter.
    seen: set[str] = set()
    deduped: list[dict] = []
    for q in out:
        if q["quote"] in seen:
            continue
        seen.add(q["quote"])
        deduped.append(q)
    return deduped[:3]  # cap per-card to keep the appendix tight


def load_ticker_module(ticker: str, modules_dir: Path) -> dict | None:
    if not ticker or not modules_dir.exists():
        return None
    p = modules_dir / f"ticker_{ticker.lower()}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def load_dossier_index(dossier_path: Path | None) -> dict[str, dict]:
    """Load the xalpha dossier and key it by uppercased ticker."""
    if dossier_path is None or not dossier_path.exists():
        return {}
    try:
        rows = json.loads(dossier_path.read_text())
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for d in rows:
        tk = (d.get("ticker") or "").strip().upper()
        if not tk:
            continue
        out[tk] = {
            "tier": d.get("tier"),
            "score": d.get("score"),
            "total_mentions": d.get("total_mentions"),
            "first_mention": d.get("first_mention"),
            "last_mention": d.get("last_mention"),
            "return_pct": (d.get("return") or {}).get("pct"),
            "alpha_vs_spy": d.get("alpha"),
        }
    return out


def merge_entities(chapters: list[dict], modules_dir: Path | None = None,
                   dossier_index: dict[str, dict] | None = None) -> dict:
    """Build the entities block.

    Companies are keyed by uppercased ticker when present (falling back to
    name) so duplicate ticker cards can't appear.

    When `modules_dir` is provided, each company/ticker entry is enriched
    with the cached Bookmark ticker module (name, exchange, blurb, recent,
    sources) and per-ticker Serenity quotes pulled from the chapter bodies.

    When `dossier_index` is provided, each company/ticker entry is also
    enriched with `dossier` (tier, score, total_mentions, return_pct,
    alpha_vs_spy). Companies are then sorted by Serenity rank: dossier
    first, then `score` desc, then `total_mentions` desc, then name.
    """
    tickers: dict[str, dict] = {}
    companies: dict[str, dict] = {}  # keyed by ticker (uppercased) when present, else by name
    concepts: dict[str, dict] = {}
    for ch in chapters:
        for t in (ch.get("tickers") or []):
            t = (t or "").strip().upper()
            if not t:
                continue
            tickers.setdefault(t, {"ticker": t, "first_in": ch["slug"]})
        for c in (ch.get("companies") or []):
            name = (c.get("name") or "").strip()
            tk = (c.get("ticker") or "").strip().upper()
            if not name:
                continue
            key = tk if tk else f"name::{name.lower()}"
            existing = companies.get(key)
            if existing is None:
                companies[key] = {**c, "first_in": ch["slug"]}
            else:
                # Backfill ticker if previously missing.
                if not existing.get("ticker") and c.get("ticker"):
                    existing["ticker"] = c["ticker"]
                # Keep the earlier first_in (chapters iterate in order).
        for c in (ch.get("concepts") or []):
            key = (c.get("key") or "").strip().lower()
            if not key:
                continue
            concepts.setdefault(key, {**c, "first_in": ch["slug"]})

    # Enrichment pass: pull cached ticker modules + extract quotes + dossier.
    di = dossier_index or {}
    if modules_dir is not None:
        for t_entry in tickers.values():
            tk = t_entry["ticker"]
            mod = load_ticker_module(tk, modules_dir)
            if mod:
                t_entry["module"] = {
                    "name": mod.get("name"),
                    "exchange": mod.get("exchange"),
                    "blurb": mod.get("blurb"),
                    "recent": mod.get("recent"),
                    "sources": mod.get("sources") or [],
                }
            t_entry["quotes"] = extract_blockquotes_for_ticker(tk, chapters)
            if tk in di:
                t_entry["dossier"] = di[tk]
        for c_entry in companies.values():
            tk = (c_entry.get("ticker") or "").strip().upper()
            mod = load_ticker_module(tk, modules_dir) if tk else None
            if mod:
                c_entry["module"] = {
                    "name": mod.get("name"),
                    "exchange": mod.get("exchange"),
                    "blurb": mod.get("blurb"),
                    "recent": mod.get("recent"),
                    "sources": mod.get("sources") or [],
                }
            c_entry["quotes"] = extract_blockquotes_for_ticker(tk, chapters) if tk else []
            if tk and tk in di:
                c_entry["dossier"] = di[tk]
    elif dossier_index:
        # Modules not provided but dossier was — still attach.
        for t_entry in tickers.values():
            tk = t_entry["ticker"]
            if tk in di:
                t_entry["dossier"] = di[tk]
        for c_entry in companies.values():
            tk = (c_entry.get("ticker") or "").strip().upper()
            if tk and tk in di:
                c_entry["dossier"] = di[tk]

    # Serenity-rank sort: dossier first, then score desc, then total_mentions
    # desc, then name. Companies without a dossier sink to the bottom.
    def serenity_rank(entry: dict) -> tuple:
        d = entry.get("dossier") or {}
        has = 0 if d else 1                           # 0 sorts first (has dossier)
        score = -(d.get("score") or 0)                # higher score sorts earlier
        mentions = -(d.get("total_mentions") or 0)
        name = (entry.get("name") or entry.get("ticker") or "").lower()
        return (has, score, mentions, name)

    return {
        "tickers": sorted(tickers.values(), key=serenity_rank),
        "companies": sorted(companies.values(), key=serenity_rank),
        "concepts": sorted(concepts.values(), key=lambda r: r["key"]),
    }


def chapter_order(report_id: str) -> list[str]:
    """The authoritative chapter order is the order of briefs.json's
    'chapters' array. Falls back to alphabetical-by-slug if missing."""
    p = report_discover(report_id) / "briefs.json"
    if p.exists():
        data = json.loads(p.read_text())
        return [c["slug"] for c in (data.get("chapters") or []) if c.get("slug")]
    chapters = report_chapters_dir(report_id)
    if chapters.exists():
        return sorted(p.stem for p in chapters.glob("*.json"))
    return []


def assemble(report_id: str) -> dict:
    meta = load_meta(report_id) or {}
    chapters_dir = report_chapters_dir(report_id)
    order = chapter_order(report_id)

    chapters: list[dict] = []
    missing: list[str] = []
    for slug in order:
        p = chapters_dir / f"{slug}.json"
        if not p.exists():
            missing.append(slug)
            continue
        ch = json.loads(p.read_text())
        ch.setdefault("slug", slug)
        ch["word_count"] = word_count(ch.get("body", ""))
        chapters.append(ch)

    abstract = (chapters[0].get("lede") or "") if chapters else ""

    modules_dir = report_inputs(report_id) / "bookmark_modules"

    # Pull the xalpha dossier so each company/ticker entry can carry its
    # Serenity-rank fields (tier, score, total_mentions, alpha) and the
    # appendix can sort by signal rather than alphabetical.
    handle = meta.get("handle") or ""
    dossier_path = report_inputs(report_id) / "xalpha" / handle / "dossiers.json"
    dossier_index = load_dossier_index(dossier_path)

    entities = merge_entities(chapters, modules_dir=modules_dir,
                              dossier_index=dossier_index)

    # Optional: timeline.json (predictions extracted by discover/timeline.py).
    timeline_path = report_prep(report_id) / "timeline.json"
    timeline = json.loads(timeline_path.read_text()) if timeline_path.exists() else None

    return {
        "meta": {
            "report_id": report_id,
            "handle": meta.get("handle"),
            "display_name": meta.get("display_name"),
            "title": meta.get("title") or meta.get("display_name") or report_id,
            "subtitle": meta.get("subtitle") or "",
            "byline": meta.get("byline") or "",
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "snapshot_date": meta.get("snapshot_date"),
            "word_count": sum(c.get("word_count", 0) for c in chapters),
            "n_chapters": len(chapters),
            "n_sources": len(merge_sources(chapters)),
            "n_companies": len(entities["companies"]),
            "n_concepts": len(entities["concepts"]),
            "n_predictions": len((timeline or {}).get("predictions") or []),
            "missing_chapters": missing,
        },
        "abstract": abstract,
        "chapters": chapters,
        "entities": entities,
        "bibliography": merge_sources(chapters),
        "timeline": timeline,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report-id", required=True)
    p.add_argument("--allow-empty", action="store_true",
                   help="Don't error if no chapters exist; useful for setup runs.")
    args = p.parse_args(argv)
    if getattr(args, 'report_id', None):
        validate_report_id(args.report_id)
    if not chapter_order(args.report_id) and not args.allow_empty:
        sys.stderr.write(
            f"error: report {args.report_id!r} has no chapters in briefs.json or "
            f"chapter dir. Did you mean a different --report-id? Pass "
            f"--allow-empty if you really want an empty assembly.\n"
        )
        return 2
    report = assemble(args.report_id)
    out = report_reports_dir(args.report_id) / "report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    m = report["meta"]
    print(f"→ {out}")
    print(f"  chapters: {m['n_chapters']}/{len(chapter_order(args.report_id))} "
          f"(missing: {', '.join(m['missing_chapters']) or 'none'})")
    print(f"  word count: {m['word_count']:,}")
    print(f"  sources: {m['n_sources']}")
    print(f"  entities: {len(report['entities']['tickers'])} tickers, "
          f"{len(report['entities']['companies'])} companies, "
          f"{len(report['entities']['concepts'])} concepts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
