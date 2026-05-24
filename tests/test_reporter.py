import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import x_bookmark_reporter as reporter  # noqa: E402


def sample_bookmark(tweet_id, text, username, created_at, urls=None, metrics=None):
    bookmark = {
        "id": tweet_id,
        "url": f"https://x.com/{username}/status/{tweet_id}",
        "text": text,
        "created_at": created_at,
        "lang": "en",
        "author": {"id": username, "username": username, "name": username.title()},
        "public_metrics": metrics or {},
        "urls": urls or [],
        "media": [],
        "referenced_tweets": [],
        "context_annotations": [],
    }
    bookmark["category"] = reporter.categorize_bookmark(bookmark)
    bookmark["engagement_score"] = reporter.engagement_score(bookmark)
    return bookmark


class ReporterTests(unittest.TestCase):
    def test_categorizes_ai_bookmark(self):
        bookmark = sample_bookmark(
            "1",
            "A practical RAG pattern for LLM agents using evals and embeddings.",
            "builder",
            "2026-01-01T00:00:00Z",
        )
        self.assertEqual(bookmark["category"], "AI and machine learning")

    def test_report_contains_expected_sections(self):
        payload = {
            "generated_at": "2026-04-29T00:00:00Z",
            "authenticated_user": {"username": "me", "name": "Me"},
            "bookmarks": [
                sample_bookmark(
                    "1",
                    "A practical RAG pattern for LLM agents using evals and embeddings.",
                    "builder",
                    "2026-01-01T00:00:00Z",
                    urls=[{"expanded_url": "https://example.com/rag"}],
                    metrics={"like_count": 100, "retweet_count": 10, "reply_count": 4, "quote_count": 2},
                ),
                sample_bookmark(
                    "2",
                    "A concise guide to startup pricing and customer discovery.",
                    "founder",
                    "2025-12-01T00:00:00Z",
                    urls=[{"expanded_url": "https://startup.example/pricing"}],
                    metrics={"like_count": 50},
                ),
            ],
        }
        report = reporter.build_report(payload, index_limit=10)
        self.assertIn("# X Bookmark Report", report)
        self.assertIn("Theme Breakdown", report)
        self.assertIn("AI and machine learning", report)
        self.assertIn("startup.example", report)
        self.assertIn("High-Signal Saves", report)

    def test_writes_report_file(self):
        payload = {
            "generated_at": "2026-04-29T00:00:00Z",
            "authenticated_user": {"username": "me", "name": "Me"},
            "bookmarks": [
                sample_bookmark("1", "Python testing notes for backend APIs.", "dev", "2026-01-01T00:00:00Z")
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "report.md"
            reporter.write_report_from_payload(payload, output, index_limit=5)
            self.assertTrue(output.exists())
            self.assertIn("Software engineering", output.read_text(encoding="utf-8"))

    def test_normalizes_safari_bookmark(self):
        bookmark = reporter.normalize_safari_bookmark(
            {
                "id": "123",
                "url": "https://x.com/dev/status/123",
                "text": "Python API testing notes",
                "created_at": "2026-01-01T00:00:00.000Z",
                "author": {"username": "dev", "name": "Dev"},
                "public_metrics": {"like_count": 10},
                "urls": [{"expanded_url": "https://example.com"}],
            }
        )
        self.assertEqual(bookmark["id"], "123")
        self.assertEqual(bookmark["author"]["username"], "dev")
        self.assertEqual(bookmark["category"], "Software engineering")

    def test_safari_import_reloads_timeline_before_scraping(self):
        page = {
            "href": "https://x.com/i/bookmarks",
            "article_count": 1,
            "scroll_y": 0,
            "inner_height": 800,
            "scroll_height": 1200,
            "articles": [
                {
                    "id": "123",
                    "url": "https://x.com/dev/status/123",
                    "text": "Fresh bookmark",
                    "created_at": "2026-05-09T00:00:00.000Z",
                    "author": {"username": "dev", "name": "Dev"},
                    "public_metrics": {},
                    "urls": [],
                    "media": [],
                }
            ],
        }
        events = []
        original_extract = reporter.safari_extract_bookmark_page
        original_reload = reporter.safari_reload_bookmarks_timeline
        original_js = reporter.safari_do_javascript
        original_sleep = reporter.time.sleep
        try:
            reporter.safari_extract_bookmark_page = lambda: events.append("extract") or page
            reporter.safari_reload_bookmarks_timeline = lambda: events.append("reload")
            reporter.safari_do_javascript = lambda *args, **kwargs: events.append("scroll_top") or "{}"
            reporter.time.sleep = lambda *_args, **_kwargs: None
            with tempfile.TemporaryDirectory() as temp_dir:
                output = Path(temp_dir) / "fresh.json"
                args = SimpleNamespace(
                    output=str(output),
                    csv=None,
                    limit=1,
                    max_scrolls=5,
                    idle_rounds=1,
                    scroll_pause=0.1,
                    scroll_step=0.85,
                    stop_after_known=0,
                )
                payload = reporter.import_bookmarks_from_safari(args)

            self.assertEqual(payload["bookmark_count"], 1)
            self.assertIn("reload", events)
            self.assertLess(events.index("reload"), events.index("scroll_top"))
        finally:
            reporter.safari_extract_bookmark_page = original_extract
            reporter.safari_reload_bookmarks_timeline = original_reload
            reporter.safari_do_javascript = original_js
            reporter.time.sleep = original_sleep


if __name__ == "__main__":
    unittest.main()
