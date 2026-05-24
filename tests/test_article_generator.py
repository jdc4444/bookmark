import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import article_generator as generator  # noqa: E402


class ArticleGeneratorTests(unittest.TestCase):
    def test_prompt_instructs_fetching_bookmark_own_short_status(self):
        self.assertIn("Apply the same rule to the BOOKMARK's own tweet", generator.ARTICLE_PROMPT)
        self.assertIn("WebFetch the bookmark URL", generator.ARTICLE_PROMPT)

    def test_context_includes_status_page_post_text(self):
        context = generator.build_bookmark_context(
            {
                "id": "1",
                "status_context": {
                    "url": "https://x.com/dev/status/1",
                    "loaded_at": "2026-05-09T00:00:00Z",
                    "post": {"text": "Full status-page body"},
                },
            }
        )
        self.assertIn("status_page_post", context)
        self.assertIn("Full status-page body", context)


if __name__ == "__main__":
    unittest.main()
