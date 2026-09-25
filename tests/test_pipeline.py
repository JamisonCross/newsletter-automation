import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


MODULE_PATH = Path(__file__).parents[1] / "src" / "newsletter_pipeline.py"
SPEC = importlib.util.spec_from_file_location("newsletter_pipeline", MODULE_PATH)
pipeline = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = pipeline
SPEC.loader.exec_module(pipeline)


class PipelineTests(unittest.TestCase):
    def story(self, title, link, source="Example State Desk", published="2026-09-20T10:00:00+00:00"):
        return pipeline.Story(title, link, source, published)

    def test_title_filter_rejects_noise(self):
        self.assertFalse(pipeline.keep_title("NBA playoffs gallery"))
        self.assertFalse(pipeline.keep_title("Short"))
        self.assertTrue(pipeline.keep_title("Example State committee reviews budget"))

    def test_dedupe_uses_links_and_normalized_titles(self):
        stories = [
            self.story("Example State budget update", "https://example.invalid/a"),
            self.story("Example   State budget update", "https://example.invalid/b"),
            self.story("Different useful update", "https://example.invalid/a"),
        ]
        self.assertEqual(len(pipeline.dedupe(stories)), 1)

    def test_classification_is_local_and_explainable(self):
        labels = pipeline.classify(
            self.story("Metroville schedules public hearing on budget", "https://example.invalid/a"),
            ["metroville"],
        )
        self.assertEqual(labels, {"general", "news", "take_action"})
        outside = pipeline.classify(
            self.story("Elsewhere schedules public hearing on budget", "https://example.invalid/b", "Elsewhere Desk"),
            ["metroville"],
        )
        self.assertEqual(outside, set())

    def test_selection_does_not_repeat_links(self):
        stories = [
            self.story("Metroville public hearing on budget", "https://example.invalid/a"),
            self.story("Metroville community library program", "https://example.invalid/b"),
            self.story("Example State agency audit released", "https://example.invalid/c"),
        ]
        selected = pipeline.select_candidates(stories, ["metroville", "example state"], {"news": 2, "take_action": 2, "local_spotlight": 2})
        links = [story.link for group in selected.values() for story in group]
        self.assertEqual(len(links), len(set(links)))

    def test_fixture_to_prompt_pack_preserves_source_link(self):
        config = {
            "publication_name": "Example Brief",
            "locality_keywords": ["example state"],
            "limits": {"news": 1, "take_action": 1, "local_spotlight": 1},
        }
        stories = [self.story("Example State agency budget review", "https://example.invalid/a")]
        selected = pipeline.select_candidates(stories, config["locality_keywords"], config["limits"])
        result = pipeline.build_prompt_pack(config, selected, "Generic guide")
        self.assertIn("Example Brief source pack", result)
        self.assertIn("https://example.invalid/a", result)

    def test_zero_limit_leaves_candidates_for_other_sections(self):
        stories = [self.story("Example State public hearing on budget", "https://example.invalid/a")]
        selected = pipeline.select_candidates(stories, ["example state"], {"news": 0, "take_action": 1})
        self.assertEqual(selected["news"], [])
        self.assertEqual(selected["take_action"], stories)

    def test_invalid_limits_are_rejected(self):
        for value in (-1, 1.5, "2", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pipeline.select_candidates([], [], {"news": value})

    def test_sort_uses_instants_across_time_zones(self):
        earlier = self.story("Earlier article", "https://example.invalid/a", published="2026-09-20T12:00:00+05:00")
        later = self.story("Later article", "https://example.invalid/b", published="2026-09-20T09:00:00Z")
        unknown = self.story("Unknown publication", "https://example.invalid/c", published="not-a-date")
        self.assertEqual(pipeline.sort_recent([earlier, unknown, later]), [later, earlier, unknown])

    @unittest.skipUnless(hasattr(time, "tzset"), "tzset unavailable")
    def test_rss_dates_are_utc_even_in_another_local_timezone(self):
        try:
            with patch.dict(os.environ, {"TZ": "EST5EDT"}):
                time.tzset()
                result = pipeline.parse_time({"published_parsed": (2026, 9, 20, 12, 0, 0, 6, 263, 0)})
                self.assertEqual(result, "2026-09-20T12:00:00+00:00")
        finally:
            time.tzset()

    def test_extraction_failure_keeps_story_and_review_note(self):
        response = SimpleNamespace(text="<article>Fixture</article>", url="https://example.invalid/a", raise_for_status=lambda: None)
        request = SimpleNamespace(get=Mock(return_value=response))
        extractor = SimpleNamespace(extract=Mock(side_effect=ValueError("Malformed article")))
        with patch.object(pipeline, "requests", request), patch.object(pipeline, "trafilatura", extractor), patch.object(pipeline, "BeautifulSoup", None):
            result = pipeline.extract_text(self.story("Example State update", response.url))
        self.assertEqual(result.link, response.url)
        self.assertIsNone(result.article_text)
        self.assertTrue(result.extraction_note)

    def test_demo_runs_with_external_configuration_and_no_site_packages(self):
        root = MODULE_PATH.parents[1]
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            config = json.loads((root / "examples/config.json").read_text())
            (folder / "config.json").write_text(json.dumps(config))
            (folder / "editorial-guide.md").write_text("Fictional test guidance.")
            output = folder / "result.md"
            result = subprocess.run([
                sys.executable, "-S", str(MODULE_PATH),
                "--config", str(folder / "config.json"),
                "--input", str(root / "examples/stories.json"),
                "--output", str(output),
            ], cwd=folder, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Fictional test guidance.", output.read_text())
            self.assertIn("news=3, take_action=1, local_spotlight=2", result.stdout)


if __name__ == "__main__":
    unittest.main()
