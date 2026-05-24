#!/usr/bin/env python3
"""Assemble per-chapter JSONs into one report.json for COMPANY-shaped dossiers.

Companion to longform/compose/assemble.py (profile-shaped). This variant
targets data/longform/companies/<report_id>/ and merges company-specific
prep files (financials, peers, counterparties, leadership, etc.) into
the entities/appendix sections of the report.

Reads:
- data/longform/companies/<id>/meta.json
- data/longform/companies/<id>/discover/briefs.json (chapter ORDER)
- data/longform/companies/<id>/prep/chapters/*.json
- data/longform/companies/<id>/prep/{financials,leadership,counterparties,peers,timeline,press_release_index}.json
- data/longform/companies/<id>/inputs/news/*.md   (for bibliography mining)

Writes:
- data/longform/companies/<id>/reports/report.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path("/Users/alphaone/Documents/Code/Bookmark")


def company_dir(report_id: str) -> Path:
    return ROOT / "data" / "longform" / "companies" / report_id


def word_count(s: str) -> int:
    return len(re.findall(r"\b\w+\b", s or ""))


def normalize_url(u: str) -> str:
    if not u:
        return ""
    u = u.strip()
    u = re.sub(r"#.*$", "", u)
    return u.rstrip("/")


def load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def extract_inline_citations(body: str) -> list[dict]:
    """Pull [text](url) citations from chapter prose."""
    out = []
    seen = set()
    for m in re.finditer(r"\[([^\]]+)\]\((https?://[^)]+)\)", body or ""):
        text, url = m.group(1).strip(), m.group(2).strip()
        u = normalize_url(url)
        if not u or u in seen:
            continue
        seen.add(u)
        out.append({"title": text, "url": u})
    return out


def build_bibliography(chapters: list[dict], briefs: dict, prep_dir: Path,
                        news_dir: Path, press_release_index: dict | None) -> list[dict]:
    by_url: dict[str, dict] = {}

    def add(url: str, title: str = "", note: str = "", first_used_in: str = ""):
        u = normalize_url(url)
        if not u:
            return
        if u not in by_url:
            by_url[u] = {"url": u, "title": title, "note": note,
                         "first_used_in": first_used_in}
        else:
            # keep the longer title; record where it was first cited
            if not by_url[u].get("title") and title:
                by_url[u]["title"] = title

    # 1. Inline citations from chapter prose
    for ch in chapters:
        for cite in extract_inline_citations(ch.get("body", "")):
            add(cite["url"], cite["title"], first_used_in=ch.get("slug", ""))

    # 2. Press release index — always include
    if press_release_index:
        for it in press_release_index.get("items", []):
            add(it.get("url", ""), it.get("title", ""), note=it.get("type", ""))

    # 3. News inputs — markdown files often link primary sources
    if news_dir.exists():
        for md in sorted(news_dir.glob("*.md")):
            text = md.read_text(encoding="utf-8")
            # extract URLs from markdown
            for url in re.findall(r"https?://\S+?(?=[)\s,;])", text):
                add(url, "", note=f"referenced in {md.name}")

    return sorted(by_url.values(), key=lambda s: s["url"])


def build_companies_appendix(counterparties: dict | None,
                              peers: dict | None) -> list[dict]:
    """Build a companies-style appendix from counterparties + peers."""
    out = []
    if counterparties:
        for b in counterparties.get("divestiture_buyers", []):
            out.append({
                "name": b.get("buyer"),
                "ticker": b.get("ticker"),
                "role": "divestiture_buyer",
                "first_in": "customers_supply_chain",
                "module": {
                    "name": b.get("buyer"),
                    "exchange": "TSE" if (b.get("ticker") or "").endswith(".T") else None,
                    "blurb": b.get("what_acquired", ""),
                },
                "dossier": {
                    "status": b.get("status"),
                    "structure": b.get("structure"),
                    "source": b.get("source"),
                },
            })
        for p in counterparties.get("joint_development_partners", []):
            out.append({
                "name": p.get("partner"),
                "ticker": p.get("ticker"),
                "role": "joint_development_partner",
                "first_in": "customers_supply_chain",
                "module": {
                    "name": p.get("partner"),
                    "blurb": p.get("scope", ""),
                },
            })
        rev = counterparties.get("majority_shareholder")
        if rev:
            out.append({
                "name": rev.get("name"),
                "role": "majority_shareholder",
                "first_in": "snapshot",
                "module": {
                    "name": rev.get("name"),
                    "blurb": f"{rev.get('stake_pct')}% voting via {rev.get('instrument')}; ¥20B injection {rev.get('via')}",
                },
            })
    if peers:
        for peer in (peers.get("peer_set_japanese_polymer_fiber_specialty", []) +
                     peers.get("global_specialty_materials_peers_for_context", [])):
            out.append({
                "name": peer.get("name"),
                "ticker": peer.get("ticker"),
                "role": "peer",
                "first_in": "competitive_position",
                "module": {
                    "name": peer.get("name"),
                    "blurb": peer.get("color", ""),
                },
            })
    return out


def build_abstract(meta: dict, snapshot: dict | None,
                    chapters: list[dict] | None = None) -> str:
    """Return the report abstract.

    Order of preference:
      1. `meta.abstract` (hand-written per-company in meta.json)
      2. `snapshot.abstract` (hand-written per-company in prep/snapshot.json)
      3. The first paragraph of the snapshot chapter body (chapters[0].body)
      4. Empty string
    """
    if meta and isinstance(meta.get("abstract"), str) and meta["abstract"].strip():
        return meta["abstract"].strip()
    if snapshot and isinstance(snapshot.get("abstract"), str) and snapshot["abstract"].strip():
        return snapshot["abstract"].strip()
    # Fall back to the first paragraph of the snapshot chapter.
    if chapters:
        for ch in chapters:
            if ch.get("slug") == "snapshot":
                body = (ch.get("body") or "").strip()
                # Skip H2 subheaders; grab the first non-empty paragraph.
                for para in re.split(r"\n\s*\n", body):
                    para = para.strip()
                    if not para or para.startswith(("#", "*", "|", ">")):
                        continue
                    return para
                break
    return ""


def assemble(report_id: str) -> dict:
    base = company_dir(report_id)
    meta = load_json(base / "meta.json") or {}
    briefs = load_json(base / "discover" / "briefs.json") or {}
    chapters_dir = base / "prep" / "chapters"
    chapter_specs = sorted(
        briefs.get("chapters", []),
        key=lambda c: c.get("order", 999),
    )

    # Load chapter bodies in brief order
    chapters: list[dict] = []
    missing: list[str] = []
    for spec in chapter_specs:
        slug = spec.get("slug")
        path = chapters_dir / f"{slug}.json"
        ch_data = load_json(path)
        if not ch_data:
            missing.append(slug)
            continue
        body = ch_data.get("body", "")
        # Add a chapter-level inline citations list
        chapter = {
            "slug": slug,
            "title": spec.get("title") or ch_data.get("title"),
            "order": spec.get("order") or ch_data.get("order"),
            "body": body,
            "word_count": word_count(body),
            "generated_at": ch_data.get("generated_at"),
        }
        # Pull tickers + concept-like terms from the body for sidebar
        tickers = sorted({m.group(1) for m in re.finditer(r"\b([A-Z0-9]{3,5}\.?(?:T|TO|JP|HK)?)\b", body)
                          if m.group(1) not in {"USD", "JPY", "EUR", "USA", "AAA", "TLDR"}})
        # Keep that simple — don't pollute
        chapter["tickers"] = []  # leave empty for now; company report focus is the company itself
        chapters.append(chapter)

    # Sidebar entities
    counterparties = load_json(base / "prep" / "counterparties.json")
    peers = load_json(base / "prep" / "peers.json")
    leadership = load_json(base / "prep" / "leadership.json")
    financials = load_json(base / "prep" / "financials.json")
    timeline = load_json(base / "prep" / "timeline.json")
    press_release_index = load_json(base / "prep" / "press_release_index.json")
    snapshot_prep = load_json(base / "prep" / "snapshot.json")

    # Bibliography
    news_dir = base / "inputs" / "news"
    bibliography = build_bibliography(chapters, briefs, base / "prep",
                                       news_dir, press_release_index)

    # Companies appendix
    companies_appendix = build_companies_appendix(counterparties, peers)

    # Abstract
    abstract = build_abstract(meta, snapshot_prep, chapters)

    total_words = sum(c["word_count"] for c in chapters)

    report = {
        "meta": {
            **meta,
            "n_chapters": len(chapters),
            "n_chapter_targets": len(chapter_specs),
            "missing_chapters": missing,
            "word_count": total_words,
            "n_sources": len(bibliography),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        },
        "abstract": abstract,
        "chapters": chapters,
        "entities": {
            "companies": companies_appendix,
            "tickers": [],
            "concepts": [],
        },
        "appendix": {
            "financials": financials,
            "leadership": leadership,
            "counterparties": counterparties,
            "peers": peers,
            "press_release_index": press_release_index,
            "timeline_events": timeline.get("events") if timeline else None,
        },
        "bibliography": bibliography,
        "timeline": None,  # Company reports use appendix.timeline_events instead
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-id", required=True)
    parser.add_argument("--out", help="override output path")
    args = parser.parse_args(argv)

    base = company_dir(args.report_id)
    if not base.exists():
        print(f"no company dir at {base}", file=sys.stderr)
        return 1

    report = assemble(args.report_id)
    out_path = Path(args.out) if args.out else (base / "reports" / "report.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    print(f"report_id: {args.report_id}")
    print(f"chapters: {report['meta']['n_chapters']}/{report['meta']['n_chapter_targets']}")
    print(f"missing: {report['meta']['missing_chapters']}")
    print(f"word_count: {report['meta']['word_count']}")
    print(f"sources: {report['meta']['n_sources']}")
    print(f"wrote: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
