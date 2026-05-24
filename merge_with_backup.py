"""Merge a freshly-scraped Safari archive with a backup so no bookmarks are lost.

Usage:
    python3 merge_with_backup.py <backup.json> <current.json> <out.json>

- Reads backup and current archives.
- Unions bookmarks by tweet ID. New entries from `current` win (fresher engagement),
  but bookmarks present only in `backup` are preserved.
- Sorts by created_at desc and writes the merged payload to <out.json>.
- Prints counts: backup_only, both, current_only, total.
"""

import json
import sys
import datetime as dt
from pathlib import Path

from archive_io import write_archive_locked


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def parse_dt(s: str | None):
    if not s:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def main(backup_path: str, current_path: str, out_path: str) -> None:
    backup = load(Path(backup_path))
    current = load(Path(current_path))

    backup_bms = {str(b.get("id")): b for b in backup.get("bookmarks", []) if b.get("id")}
    current_bms = {str(b.get("id")): b for b in current.get("bookmarks", []) if b.get("id")}

    backup_only = set(backup_bms) - set(current_bms)
    current_only = set(current_bms) - set(backup_bms)
    both = set(backup_bms) & set(current_bms)

    merged = dict(backup_bms)
    merged.update(current_bms)
    bookmarks = list(merged.values())
    bookmarks.sort(key=lambda b: parse_dt(b.get("created_at")), reverse=True)

    payload = dict(current)
    payload["bookmarks"] = bookmarks
    payload["bookmark_count"] = len(bookmarks)
    payload["merge"] = {
        "backup_path": backup_path,
        "backup_count": len(backup_bms),
        "current_count": len(current_bms),
        "merged_count": len(bookmarks),
        "backup_only": len(backup_only),
        "current_only": len(current_only),
        "in_both": len(both),
        "merged_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    write_archive_locked(Path(out_path), payload)

    print(f"backup_count    = {len(backup_bms)}")
    print(f"current_count   = {len(current_bms)}")
    print(f"in_both         = {len(both)}")
    print(f"backup_only     = {len(backup_only)}  (preserved from backup)")
    print(f"current_only    = {len(current_only)}  (newly added by Safari import)")
    print(f"merged_total    = {len(bookmarks)}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
