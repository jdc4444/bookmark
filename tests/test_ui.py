import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import x_bookmark_ui as ui  # noqa: E402


class UITests(unittest.TestCase):
    def test_read_bookmarks_filters_archive(self):
        payload = {
            "generated_at": "2026-04-29T00:00:00Z",
            "bookmarks": [
                {
                    "id": "1",
                    "url": "https://x.com/dev/status/1",
                    "text": "Python API testing note",
                    "created_at": "2026-01-01T00:00:00Z",
                    "author": {"username": "dev", "name": "Dev"},
                    "public_metrics": {"like_count": 2},
                    "urls": [],
                    "media": [],
                    "referenced_tweets": [],
                    "category": "Software engineering",
                },
                {
                    "id": "2",
                    "url": "https://x.com/founder/status/2",
                    "text": "Customer pricing thread",
                    "created_at": "2026-01-02T00:00:00Z",
                    "author": {"username": "founder", "name": "Founder"},
                    "public_metrics": {"like_count": 4},
                    "urls": [],
                    "media": [],
                    "referenced_tweets": [],
                    "category": "Business and startups",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "archive.json"
            archive.write_text(ui.json.dumps(payload), encoding="utf-8")
            result = ui.read_bookmarks(archive, query="python", limit=10)
            self.assertEqual(result["matched"], 1)
            self.assertEqual(result["rows"][0]["id"], "1")

    def test_read_bookmarks_sorts_by_bookmarked_at_with_created_at_fallback(self):
        payload = {
            "generated_at": "2026-04-29T00:00:00Z",
            "bookmarks": [
                {
                    "id": "old-post-new-bookmark",
                    "text": "old tweet bookmarked recently",
                    "created_at": "2026-01-01T00:00:00Z",
                    "bookmarked_at": "2026-05-09T00:00:00Z",
                    "author": {"username": "a"},
                    "public_metrics": {},
                },
                {
                    "id": "new-post-no-bookmark-date",
                    "text": "newer tweet without bookmarked_at",
                    "created_at": "2026-04-01T00:00:00Z",
                    "author": {"username": "b"},
                    "public_metrics": {},
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "archive.json"
            archive.write_text(ui.json.dumps(payload), encoding="utf-8")

            recent = ui.read_bookmarks(archive, sort="bookmarked-recent", limit=10)
            oldest = ui.read_bookmarks(archive, sort="bookmarked-oldest", limit=10)

        self.assertEqual([row["id"] for row in recent["rows"]], ["old-post-new-bookmark", "new-post-no-bookmark-date"])
        self.assertEqual([row["id"] for row in oldest["rows"]], ["new-post-no-bookmark-date", "old-post-new-bookmark"])
        self.assertEqual(recent["rows"][0]["bookmarked_at"], "2026-05-09T00:00:00Z")

    def test_analysis_payload_summarizes_archive(self):
        payload = {
            "generated_at": "2026-04-29T00:00:00Z",
            "source": "test",
            "bookmarks": [
                {
                    "id": "1",
                    "url": "https://x.com/dev/status/1",
                    "text": "Python API testing note",
                    "created_at": "2026-01-01T00:00:00Z",
                    "author": {"username": "dev", "name": "Dev"},
                    "public_metrics": {"like_count": 2},
                    "urls": [{"expanded_url": "https://example.com/post"}],
                    "media": [],
                    "referenced_tweets": [],
                    "category": "Software engineering",
                },
                {
                    "id": "2",
                    "url": "https://x.com/ai/status/2",
                    "text": "LLM agents and evals",
                    "created_at": "2026-02-01T00:00:00Z",
                    "author": {"username": "ai", "name": "AI"},
                    "public_metrics": {"like_count": 8},
                    "urls": [],
                    "media": [],
                    "referenced_tweets": [],
                    "category": "AI and machine learning",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "archive.json"
            archive.write_text(ui.json.dumps(payload), encoding="utf-8")
            result = ui.analysis_payload(archive)
            self.assertEqual(result["total"], 2)
            self.assertEqual(result["stats"]["authors"], 2)
            self.assertEqual(result["stats"]["domains"], 1)
            self.assertEqual(result["categories"][0]["count"], 1)

    def test_extract_cli_content_from_chat_completion(self):
        raw = ui.json.dumps({"choices": [{"message": {"content": "{\"headline\":\"ok\"}"}}]})
        self.assertEqual(ui.extract_cli_content(raw), "{\"headline\":\"ok\"}")

    def test_chunk_items_for_cli(self):
        items = [{"text": "x" * 30}, {"text": "y" * 30}, {"text": "z" * 30}]
        chunks = ui.chunk_items_for_cli(items, max_chars=70)
        self.assertGreaterEqual(len(chunks), 2)

    def test_normalize_bookmark_analysis_adds_bookmark_metadata(self):
        bookmark = {
            "id": "abc",
            "url": "https://x.com/dev/status/abc",
            "text": "A useful note",
            "created_at": "2026-03-01T00:00:00Z",
            "author": {"username": "dev", "name": "Dev"},
            "public_metrics": {},
            "urls": [],
            "media": [],
            "referenced_tweets": [],
            "category": "Software engineering",
        }
        result = ui.normalize_bookmark_analysis(
            {
                "headline": "Useful note",
                "topics": "engineering",
                "key_points": ["point"],
            },
            bookmark,
            "test-model",
        )
        self.assertEqual(result["bookmark_id"], "abc")
        self.assertEqual(result["model"], "test-model")
        self.assertEqual(result["topics"], ["engineering"])
        self.assertEqual(result["key_points"], ["point"])

    def test_read_bookmark_analyses_missing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = ui.read_bookmark_analyses(Path(temp_dir) / "missing.json")
            self.assertFalse(result["exists"])
            self.assertEqual(result["analyses"], {})

    def test_seed_archive_from_fresh_if_missing_initializes_archive(self):
        payload = {
            "generated_at": "2026-04-29T00:00:00Z",
            "bookmark_count": 1,
            "bookmarks": [{"id": "1", "text": "first pull"}],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "archive.json"
            fresh = Path(temp_dir) / "fresh.json"
            fresh.write_text(ui.json.dumps(payload), encoding="utf-8")

            self.assertTrue(ui.seed_archive_from_fresh_if_missing(archive, fresh))
            seeded = ui.reporter.read_json(archive)
            self.assertNotEqual(seeded["generated_at"], payload["generated_at"])
            self.assertEqual(seeded["bookmark_count"], payload["bookmark_count"])
            self.assertEqual(seeded["bookmarks"], payload["bookmarks"])
            archive.write_text(ui.json.dumps({"bookmarks": []}), encoding="utf-8")
            self.assertFalse(ui.seed_archive_from_fresh_if_missing(archive, fresh))
            self.assertEqual(ui.reporter.read_json(archive), {"bookmarks": []})

    def test_new_ids_missing_subagent_article_selects_only_new_unupgraded_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backup = root / "x-bookmarks.backup.json"
            archive = root / "x-bookmarks.json"
            articles = root / "articles"
            articles.mkdir()
            backup.write_text(
                ui.json.dumps({"bookmarks": [{"id": "old"}]}),
                encoding="utf-8",
            )
            archive.write_text(
                ui.json.dumps({"bookmarks": [{"id": "old"}, {"id": "new1"}, {"id": "new2"}]}),
                encoding="utf-8",
            )
            (articles / "article_new2.json").write_text(
                ui.json.dumps({"backend": "claude-sonnet-subagent"}),
                encoding="utf-8",
            )

            self.assertEqual(ui.new_ids_missing_subagent_article(backup, archive), ["new1"])

    def test_settings_to_namespace_includes_bookmark_analysis_defaults(self):
        args = ui.settings_to_namespace({})
        self.assertEqual(args.per_bookmark_limit, ui.DEFAULT_PER_BOOKMARK_LIMIT)
        self.assertTrue(str(args.bookmark_analyses).endswith(ui.DEFAULT_BOOKMARK_ANALYSES_PATH))

    def test_merge_enriched_media_preserves_archive_items_and_ocr(self):
        latest_media = [
            {
                "url": "https://cdn.example/one.jpg",
                "type": "photo",
                "ocr_text": "existing text",
                "ocr_confidence": 0.94,
                "width": 800,
            },
            {
                "url": "https://cdn.example/two.jpg",
                "type": "photo",
                "ocr_text": "archive only",
            },
        ]
        enriched_media = [
            {
                "url": "https://cdn.example/one.jpg",
                "type": "animated_gif",
                "ocr_text": "",
                "height": 600,
            },
            {
                "url": "https://cdn.example/three.jpg",
                "type": "photo",
            },
        ]

        merged = ui._merge_enriched_media(latest_media, enriched_media)

        self.assertEqual(
            [item["url"] for item in merged],
            [
                "https://cdn.example/one.jpg",
                "https://cdn.example/two.jpg",
                "https://cdn.example/three.jpg",
            ],
        )
        self.assertEqual(merged[0]["type"], "animated_gif")
        self.assertEqual(merged[0]["height"], 600)
        self.assertEqual(merged[0]["width"], 800)
        self.assertEqual(merged[0]["ocr_text"], "existing text")
        self.assertEqual(merged[0]["ocr_confidence"], 0.94)
        self.assertEqual(merged[1]["ocr_text"], "archive only")

    def test_merge_bookmark_enrichment_updates_full_text(self):
        payload = {
            "bookmarks": [
                {
                    "id": "1",
                    "text": "Short clipped text",
                    "text_truncated": True,
                    "media": [],
                }
            ]
        }
        enriched = {
            "id": "1",
            "text": "Short clipped text plus the full long-form body.",
            "text_full": True,
            "status_context": {"post": {"text": "Short clipped text plus the full long-form body."}},
            "context_enriched_at": "2026-05-09T00:00:00Z",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = Path(temp_dir) / "archive.json"
            archive.write_text(ui.json.dumps(payload), encoding="utf-8")

            ui.merge_bookmark_enrichment_into_archive(archive, enriched)
            updated = ui.reporter.read_json(archive)["bookmarks"][0]

        self.assertEqual(updated["text"], enriched["text"])
        self.assertTrue(updated["text_full"])
        self.assertNotIn("text_truncated", updated)


if __name__ == "__main__":
    unittest.main()
