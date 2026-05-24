"""Build a longform report end-to-end for any X handle in the xalpha corpus.

Stages:
  freeze     — copy xalpha + Bookmark + kb_sources into a frozen snapshot
  prep       — profile, corpus_scan, themes, reconstruction (pure-data)
  discover   — LLM proposes themes.json + briefs.json (editable)
  research   — Sonnet sub-agent writes each chapter (12-18 sources each)
  assemble   — merge chapters into report.json

By default `build.py <handle>` runs every stage, skipping discover if
themes.json + briefs.json already exist (so manual edits aren't clobbered).

Usage:
  python3 longform/build.py aleabitoreddit
  python3 longform/build.py pelositracker --report-id pelosi
  python3 longform/build.py aleabitoreddit --stages freeze prep
  python3 longform/build.py aleabitoreddit --stages research --chapter photonics
  python3 longform/build.py aleabitoreddit --force-discover  # re-run LLM stages
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import (  # noqa: E402
    derive_report_id, list_reports, load_meta,
    report_chapters_dir, report_discover, report_dir, report_inputs, report_prep,
)

LF = Path(__file__).resolve().parent  # Bookmark/longform/

ALL_STAGES = ["freeze", "prep", "discover", "research", "assemble"]


def run_py(script: Path, args: list[str]) -> int:
    cmd = [sys.executable, str(script)] + args
    print(f"\n$ {' '.join(cmd[1:])}")
    return subprocess.call(cmd)


def stage_freeze(handle: str, report_id: str) -> int:
    return run_py(LF / "prep" / "freeze.py",
                  ["--handle", handle, "--report-id", report_id])


def stage_prep(handle: str, report_id: str, do_reconstruct: bool) -> int:
    rc = run_py(LF / "prep" / "profile.py",
                ["--handle", handle, "--report-id", report_id])
    if rc: return rc
    rc = run_py(LF / "prep" / "corpus_scan.py",
                ["--handle", handle, "--report-id", report_id])
    if rc: return rc
    if do_reconstruct:
        rc = run_py(LF / "prep" / "reconstruct.py",
                    ["--handle", handle, "--report-id", report_id])
        if rc:
            print(f"  reconstruction failed (non-fatal): {rc}", file=sys.stderr)
    # themes.py runs after discover, since it needs themes.json to exist
    return 0


def stage_themes(report_id: str) -> int:
    return run_py(LF / "prep" / "themes.py", ["--report-id", report_id])


def stage_discover(report_id: str, force: bool) -> int:
    """Run discover stages with the correct sequencing:
        1. discover/themes.py    — LLM proposes ticker clusters
        2. prep/themes.py        — joins cluster sizes/dossier data into themes.json
        3. discover/briefs.py    — LLM writes chapter briefs (uses joined data)

    Steps 1 and 3 skip if their JSON output already exists, unless `force`
    is true (in which case `--force` is propagated to the subprocess)."""
    discover_dir = LF / "discover"
    themes_script = discover_dir / "themes.py"
    briefs_script = discover_dir / "briefs.py"

    out_themes = report_discover(report_id) / "themes.json"
    out_briefs = report_discover(report_id) / "briefs.json"

    if not themes_script.exists() and not briefs_script.exists():
        print("  discover stage skipped (scripts not present yet)")
        return 0

    # Step 1: discover/themes.py
    if themes_script.exists():
        if force or not out_themes.exists():
            args = ["--report-id", report_id]
            if force:
                args.append("--force")
            rc = run_py(themes_script, args)
            if rc:
                return rc
        else:
            print(f"  themes.json exists ({out_themes}) — skipping. "
                  f"--force-discover to re-run.")

    # Step 2: prep/themes.py joins cluster data — must run before briefs.py
    rc = stage_themes(report_id)
    if rc:
        return rc

    # Step 3: discover/briefs.py
    if briefs_script.exists():
        if force or not out_briefs.exists():
            args = ["--report-id", report_id]
            if force:
                args.append("--force")
            rc = run_py(briefs_script, args)
            if rc:
                return rc
        else:
            print(f"  briefs.json exists ({out_briefs}) — skipping. "
                  f"--force-discover to re-run.")

    return 0


def stage_research(report_id: str, chapter: str | None) -> int:
    runner = LF / "research" / "runner.py"
    if chapter:
        return run_py(runner, [chapter, "--report-id", report_id])
    return run_py(runner, ["--all", "--report-id", report_id])


def stage_assemble(report_id: str) -> int:
    return run_py(LF / "compose" / "assemble.py", ["--report-id", report_id])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("handle", nargs="?", help="X handle (with or without @)")
    p.add_argument("--report-id", default=None,
                   help="Filesystem slug (default: derived from handle)")
    p.add_argument("--stages", nargs="+", choices=ALL_STAGES, default=ALL_STAGES,
                   help="Subset of stages to run")
    p.add_argument("--chapter", default=None,
                   help="In research stage, run only this chapter slug")
    p.add_argument("--force-discover", action="store_true",
                   help="Re-run themes/briefs LLM calls even if outputs exist")
    p.add_argument("--no-reconstruct", action="store_true",
                   help="Skip the in-sample reconstruction sidebar (faster)")
    p.add_argument("--list", action="store_true",
                   help="List existing reports and exit")
    args = p.parse_args(argv)

    if args.list:
        for r in list_reports():
            meta = load_meta(r)
            print(f"  {r:20s} @{meta.get('handle','?'):20s} "
                  f"{meta.get('display_name','')}")
        return 0

    # Stages that don't touch the source xalpha account (assemble) can run
    # from --report-id alone; we look up the handle from meta.json.
    stages_needing_handle = {"freeze", "prep", "discover", "research"}
    needs_handle = bool(set(args.stages) & stages_needing_handle)

    if needs_handle and not args.handle:
        p.error(f"handle is required for stages {sorted(set(args.stages) & stages_needing_handle)} "
                f"(or pass --list)")
    if not args.handle and not args.report_id:
        p.error("must pass either handle or --report-id (for assemble-only runs)")

    handle = (args.handle or "").strip().lstrip("@").lower()
    if args.report_id:
        report_id = args.report_id.strip().lower()
    elif handle:
        report_id = derive_report_id(handle) or handle
    else:
        report_id = ""

    if not report_id or not re.match(r"^[a-z0-9_-]+$", report_id):
        p.error(f"invalid report_id: {report_id!r}. Must be [a-z0-9_-]+. "
                f"Pass --report-id explicitly.")

    if args.chapter and "research" not in args.stages:
        print(f"warning: --chapter {args.chapter} is ignored unless 'research' "
              f"is in --stages", file=sys.stderr)

    print(f"@{handle} → report_id={report_id}")
    print(f"  inputs:   {report_inputs(report_id)}")
    print(f"  prep:     {report_prep(report_id)}")
    print(f"  discover: {report_discover(report_id)}")
    print(f"  chapters: {report_chapters_dir(report_id)}")

    for stage in args.stages:
        print(f"\n========== stage: {stage} ==========")
        if stage == "freeze":
            rc = stage_freeze(handle, report_id)
        elif stage == "prep":
            rc = stage_prep(handle, report_id,
                            do_reconstruct=not args.no_reconstruct)
        elif stage == "discover":
            rc = stage_discover(report_id, force=args.force_discover)
        elif stage == "research":
            rc = stage_research(report_id, args.chapter)
        elif stage == "assemble":
            rc = stage_assemble(report_id)
        else:
            rc = 1
        if rc != 0:
            print(f"\nstage {stage!r} failed with exit code {rc}", file=sys.stderr)
            return rc

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
