import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


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

    def test_fixture_to_prompt_pack_contains_no_private_material(self):
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
        self.assertNotIn("PrivateRegion", result)


if __name__ == "__main__":
    unittest.main()
