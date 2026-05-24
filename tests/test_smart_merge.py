import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import smart_merge  # noqa: E402


class SmartMergeTests(unittest.TestCase):
    def test_merge_refreshes_top_level_generated_at(self):
        backup_payload = {
            "generated_at": "2026-05-05T07:22:50Z",
            "source": "backup",
            "bookmark_count": 1,
            "bookmarks": [
                {"id": "old", "created_at": "2026-05-01T00:00:00Z", "text": "old bookmark"},
            ],
        }
        fresh_payload = {
            "generated_at": "2026-05-09T00:00:00Z",
            "source": "fresh",
            "bookmark_count": 1,
            "bookmarks": [
                {"id": "new", "created_at": "2026-05-09T00:00:00Z", "text": "new bookmark"},
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backup_path = root / "backup.json"
            fresh_path = root / "fresh.json"
            out_path = root / "archive.json"
            backup_path.write_text(json.dumps(backup_payload), encoding="utf-8")
            fresh_path.write_text(json.dumps(fresh_payload), encoding="utf-8")

            smart_merge.main(str(backup_path), str(fresh_path), str(out_path))

            merged = json.loads(out_path.read_text(encoding="utf-8"))

        self.assertNotEqual(merged["generated_at"], backup_payload["generated_at"])
        self.assertTrue(merged["generated_at"].endswith("Z"))
        self.assertEqual(merged["bookmark_count"], 2)
        self.assertEqual([bookmark["id"] for bookmark in merged["bookmarks"]], ["new", "old"])
        by_id = {bookmark["id"]: bookmark for bookmark in merged["bookmarks"]}
        self.assertIn("bookmarked_at", by_id["new"])
        self.assertNotIn("bookmarked_at", by_id["old"])

    def test_backfill_bookmarked_at_sets_created_at_for_missing_only(self):
        payload = {
            "bookmarks": [
                {"id": "1", "created_at": "2026-01-01T00:00:00Z"},
                {
                    "id": "2",
                    "created_at": "2026-01-02T00:00:00Z",
                    "bookmarked_at": "2026-05-09T00:00:00Z",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "archive.json"
            archive.write_text(json.dumps(payload), encoding="utf-8")

            updated = smart_merge.backfill_bookmarked_at(str(archive))
            backfilled = json.loads(archive.read_text(encoding="utf-8"))

        self.assertEqual(updated, 1)
        self.assertEqual(backfilled["bookmarks"][0]["bookmarked_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(backfilled["bookmarks"][1]["bookmarked_at"], "2026-05-09T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
