"""Path resolution + report metadata for the longform pipeline.

A "longform" report is identified by a slug (e.g. "aleabit"). All of its
inputs, intermediates, chapters and final assembled JSON live under
`Bookmark/data/longform/<slug>/`. Tooling lives under `Bookmark/longform/`.

`meta.json` at the report root carries the human-facing identity: the X
handle, display name, title, subtitle, byline, snapshot date. Written by
`prep/freeze.py`, consumed by everything downstream so display strings
don't need to be hardcoded anywhere.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

LONGFORM_DIR = Path(__file__).resolve().parent      # Bookmark/longform/
BOOKMARK_ROOT = LONGFORM_DIR.parent                  # Bookmark/
DATA_ROOT = BOOKMARK_ROOT / "data" / "longform"      # Bookmark/data/longform/

XALPHA_ROOT = Path("/Users/alphaone/Documents/Code/xalpha")
KB_ROOT = Path("/Users/alphaone/Documents/Code/fin_v1/kb")


def report_dir(report_id: str) -> Path:
    return DATA_ROOT / report_id


def report_meta_path(report_id: str) -> Path:
    return report_dir(report_id) / "meta.json"


def report_inputs(report_id: str) -> Path:
    return report_dir(report_id) / "inputs"


def report_prep(report_id: str) -> Path:
    return report_dir(report_id) / "prep"


def report_chapters_dir(report_id: str) -> Path:
    return report_prep(report_id) / "chapters"


def report_discover(report_id: str) -> Path:
    return report_dir(report_id) / "discover"


def report_reports_dir(report_id: str) -> Path:
    return report_dir(report_id) / "reports"


def list_reports() -> list[str]:
    if not DATA_ROOT.exists():
        return []
    return sorted(
        p.name for p in DATA_ROOT.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )


def load_meta(report_id: str) -> dict:
    p = report_meta_path(report_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def save_meta(report_id: str, meta: dict) -> None:
    p = report_meta_path(report_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(meta, indent=2, ensure_ascii=False))


def validate_report_id(report_id: str) -> str:
    """Raise ValueError if report_id is not a safe filesystem slug."""
    if not isinstance(report_id, str) or not re.match(r"^[a-z0-9_-]+$", report_id):
        raise ValueError(
            f"invalid report_id: {report_id!r}. "
            f"Must match ^[a-z0-9_-]+$. "
            f"Pass --report-id explicitly if derive_report_id() didn't get it right."
        )
    return report_id


def derive_report_id(handle: str) -> str:
    """Produce a clean filesystem slug from an X handle.

    Strips leading @, lowercases, and trims a few common suffixes that
    aren't the actual identity (e.g. 'aleabitoreddit' -> 'aleabit'). Falls
    back to the handle unchanged if no rule applies.
    """
    s = (handle or "").strip().lstrip("@").lower()
    s = re.sub(r"[^a-z0-9_-]", "", s)
    if not s:
        return s
    # Common cosmetic suffixes — drop only if it leaves a recognisable stem.
    # Order matters: try longer suffixes first so 'oreddit' beats 'reddit'.
    for suffix in ("oreddit", "reddit", "official", "_real"):
        if s.endswith(suffix) and len(s) - len(suffix) >= 4:
            return s[: -len(suffix)]
    return s


# Default report — overridable via env var so existing scripts that import
# DEFAULT_REPORT_ID keep working until they're refactored to take the
# handle explicitly.
DEFAULT_REPORT_ID = os.environ.get("LONGFORM_REPORT_ID", "aleabit")
