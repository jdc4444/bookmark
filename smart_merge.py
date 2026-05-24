"""Smart merge after safari-import: preserve all enrichment from backup,
add only NEW bookmarks from the fresh scrape.

Why: safari-import overwrites x-bookmarks.json with raw scraped data, dropping
our tags, OCR text, and OCR-fail markers. This merge keeps the current output
archive (or backup if the output does not exist yet) as the source of truth for
everything we already have, and only pulls in IDs that the new scrape captured
but weren't in that base.

For IDs in both: we keep the base entry verbatim (we already have its tags,
OCR, processed media, status_context, etc.). The fresh metrics in the new
scrape are dropped — by design; engagement counts go stale anyway.

Usage:
    python3 smart_merge.py <backup.json> <fresh.json> <out.json>
    python3 smart_merge.py --backfill-bookmarked-at <archive.json>
"""

import datetime as dt
import json
import sys
from pathlib import Path

from archive_io import archive_file_lock, write_json_atomic


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def backfill_bookmarked_at(archive_path: str) -> int:
    path = Path(archive_path)
    with archive_file_lock(path):
        payload = json.loads(path.read_text())
        changed = 0
        for bookmark in payload.get("bookmarks", []):
            if not isinstance(bookmark, dict):
                continue
            if "bookmarked_at" not in bookmark:
                bookmark["bookmarked_at"] = bookmark.get("created_at")
                changed += 1
        if changed:
            write_json_atomic(path, payload)
    return changed


def main(backup_path: str, fresh_path: str, out_path: str) -> None:
    out = Path(out_path)
    with archive_file_lock(out):
        merge_base_path = out if out.exists() else Path(backup_path)
        backup = json.loads(merge_base_path.read_text())
        fresh = json.loads(Path(fresh_path).read_text())

        backup_bms = {str(b.get("id")): b for b in backup.get("bookmarks", []) if b.get("id")}
        fresh_bms = {str(b.get("id")): b for b in fresh.get("bookmarks", []) if b.get("id")}

        new_ids = set(fresh_bms) - set(backup_bms)
        overlap = set(fresh_bms) & set(backup_bms)
        backup_only = set(backup_bms) - set(fresh_bms)

        merged: dict[str, dict] = dict(backup_bms)
        bookmarked_at = utc_now_iso()
        for nid in new_ids:
            new_bookmark = dict(fresh_bms[nid])
            new_bookmark["bookmarked_at"] = bookmarked_at
            merged[nid] = new_bookmark

        # Text-update pass: if the fresh scrape got a longer (un-truncated) text
        # for a bookmark we already had, take it. Preserves all enrichment fields
        # (tags, ocr_text, etc.) since we only mutate the text field.
        text_updates = 0
        reply_fills = 0
        quote_fills = 0
        for tid, existing in merged.items():
            fresh_bookmark = fresh_bms.get(tid)
            if not fresh_bookmark:
                continue
            if not fresh_bookmark.get("truncated"):
                fresh_text = (fresh_bookmark.get("text") or "").strip()
                old_text = (existing.get("text") or "").strip()
                if fresh_text and len(fresh_text) > len(old_text) + 10:
                    existing["text"] = fresh_text
                    existing.pop("text_truncated", None)
                    text_updates += 1
            # Fill-in (don't overwrite) reply_to / quoted_tweet — Safari import now
            # captures these, so on a re-pull we backfill missing context onto
            # bookmarks that hadn't been enriched yet. The enrich-pass version
            # tends to be richer (full thread, recursive ancestors), so we never
            # clobber existing values.
            if fresh_bookmark.get("reply_to") and not existing.get("reply_to"):
                existing["reply_to"] = list(fresh_bookmark["reply_to"])
                reply_fills += 1
            if fresh_bookmark.get("quoted_tweet") and not existing.get("quoted_tweet"):
                existing["quoted_tweet"] = fresh_bookmark["quoted_tweet"]
                quote_fills += 1

        bookmarks = list(merged.values())

        def parse(s):
            try:
                return dt.datetime.fromisoformat((s or "").replace("Z", "+00:00"))
            except Exception:
                return dt.datetime.min.replace(tzinfo=dt.timezone.utc)

        bookmarks.sort(key=lambda b: parse(b.get("created_at")), reverse=True)

        payload = dict(backup)
        payload["generated_at"] = utc_now_iso()
        payload["bookmarks"] = bookmarks
        payload["bookmark_count"] = len(bookmarks)
        payload["last_pull"] = {
            "fresh_path": fresh_path,
            "backup_path": backup_path,
            "merge_base_path": str(merge_base_path),
            "fresh_count": len(fresh_bms),
            "backup_count": len(backup_bms),
            "new_added": len(new_ids),
            "overlap": len(overlap),
            "backup_only": len(backup_only),
            "total_after_merge": len(bookmarks),
            "merged_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        }

        write_json_atomic(out, payload)

    print(f"backup_count   = {len(backup_bms)}")
    print(f"fresh_count    = {len(fresh_bms)}")
    print(f"overlap        = {len(overlap)}")
    print(f"backup_only    = {len(backup_only)}")
    print(f"NEW            = {len(new_ids)}  <- pulled in this run")
    print(f"text_updated   = {text_updates}  (longer text from fresh scrape)")
    print(f"reply_to filled= {reply_fills}  (from fresh scrape)")
    print(f"quoted filled  = {quote_fills}  (from fresh scrape)")
    print(f"merged_total   = {len(bookmarks)}")
    if new_ids:
        print()
        print("New bookmark IDs (first 10):")
        for nid in list(new_ids)[:10]:
            text = (fresh_bms[nid].get("text") or "").replace("\n", " ")[:80]
            author = ((fresh_bms[nid].get("author") or {}).get("username") or "?")
            print(f"  {nid}  @{author}  {text}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--backfill-bookmarked-at":
        updated = backfill_bookmarked_at(sys.argv[2])
        print(f"bookmarked_at backfilled = {updated}")
        sys.exit(0)
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
