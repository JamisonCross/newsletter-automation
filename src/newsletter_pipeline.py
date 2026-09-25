#!/usr/bin/env python3
"""Sanitized newsletter research and prompt-pack pipeline."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

try:
    import feedparser
except ImportError:  # Offline fixture mode does not require it.
    feedparser = None

try:
    import requests
except ImportError:
    requests = None

try:
    import trafilatura
except ImportError:
    trafilatura = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None


SPORTS = re.compile(r"\b(nba|nfl|mlb|nhl|soccer|football|basketball|playoffs)\b", re.I)
GALLERY = re.compile(r"\b(photo|photos|gallery|slideshow)\b", re.I)
NEWS = re.compile(r"\b(bill|budget|committee|court|election|governor|lawmakers|senate|audit|agency)\b", re.I)
ACTION = re.compile(r"\b(public hearing|public comment|vote|ballot|petition|contact|proposal)\b", re.I)
SPOTLIGHT = re.compile(r"\b(community|volunteer|free|program|scholarship|clinic|library|workshop|mentorship)\b", re.I)
NEGATIVE_SPOTLIGHT = re.compile(r"\b(obituary|arrested|shooting|homicide|fatal)\b", re.I)


@dataclass(frozen=True)
class Story:
    title: str
    link: str
    source: str
    published: Optional[str] = None
    author: Optional[str] = None
    article_text: Optional[str] = None
    extraction_note: Optional[str] = None


def normalized_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip()


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", normalized_title(title).lower())


def keep_title(title: str) -> bool:
    title = normalized_title(title)
    return len(title) >= 10 and not SPORTS.search(title) and not GALLERY.search(title)


def dedupe(stories: Iterable[Story]) -> list[Story]:
    links: set[str] = set()
    titles: set[str] = set()
    result: list[Story] = []
    for story in stories:
        key = title_key(story.title)
        if story.link in links or key in titles:
            continue
        links.add(story.link)
        titles.add(key)
        result.append(story)
    return result


def classify(story: Story, locality_keywords: list[str]) -> set[str]:
    text = f"{story.title} {story.source}".lower()
    local = any(keyword.lower() in text for keyword in locality_keywords)
    labels: set[str] = set()
    if local:
        labels.add("general")
        if NEWS.search(text):
            labels.add("news")
        if ACTION.search(text):
            labels.add("take_action")
        if SPOTLIGHT.search(text) and not NEGATIVE_SPOTLIGHT.search(text):
            labels.add("local_spotlight")
    return labels


def sort_recent(stories: Iterable[Story]) -> list[Story]:
    return sorted(stories, key=lambda story: story.published or "", reverse=True)


def select_candidates(stories: list[Story], keywords: list[str], limits: dict[str, int]) -> dict[str, list[Story]]:
    buckets = {"news": [], "take_action": [], "local_spotlight": [], "general": []}
    for story in sort_recent(stories):
        for label in classify(story, keywords):
            buckets[label].append(story)

    used: set[str] = set()

    def take(pool: list[Story], count: int) -> list[Story]:
        chosen: list[Story] = []
        for story in pool:
            if story.link in used:
                continue
            chosen.append(story)
            used.add(story.link)
            if len(chosen) == count:
                break
        return chosen

    return {
        "news": take(buckets["news"] + buckets["general"], limits.get("news", 3)),
        "take_action": take(buckets["take_action"], limits.get("take_action", 2)),
        "local_spotlight": take(buckets["local_spotlight"], limits.get("local_spotlight", 2)),
    }


def load_fixture(path: Path) -> list[Story]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Story fixture must be a JSON array")
    return [Story(**item) for item in data if keep_title(str(item.get("title", "")))]


def parse_time(entry: dict) -> Optional[str]:
    value = entry.get("published_parsed") or entry.get("updated_parsed")
    if not value:
        return None
    return datetime.fromtimestamp(time.mktime(value), tz=timezone.utc).isoformat()


def fetch_feeds(urls: list[str]) -> list[Story]:
    if feedparser is None:
        raise RuntimeError("Install requirements.txt to use RSS mode")
    stories: list[Story] = []
    for url in urls:
        feed = feedparser.parse(url)
        source = str(feed.feed.get("title") or "Unknown source").strip()
        for entry in feed.entries[:80]:
            title = normalized_title(str(entry.get("title") or ""))
            link = str(entry.get("link") or "").strip()
            if title and link and keep_title(title):
                stories.append(Story(title, link, source, parse_time(entry)))
    return stories


def extract_text(story: Story) -> Story:
    if requests is None:
        return Story(**{**asdict(story), "extraction_note": "requests is not installed"})
    try:
        response = requests.get(
            story.link,
            timeout=20,
            headers={"User-Agent": "NewsletterPortfolioDemo/1.0"},
            allow_redirects=True,
        )
        response.raise_for_status()
    except Exception as error:
        return Story(**{**asdict(story), "extraction_note": f"extraction failed: {type(error).__name__}"})

    text = None
    if trafilatura is not None:
        text = trafilatura.extract(response.text, url=response.url, include_comments=False, include_tables=False)
    if (not text or len(text) < 300) and BeautifulSoup is not None:
        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        container = soup.find("article") or soup.find("body")
        text = container.get_text(" ", strip=True) if container else None
    if not text or len(text) < 300:
        return Story(**{**asdict(story), "extraction_note": "article text unavailable or too short"})
    text = re.sub(r"\s+", " ", text).strip()[:45000]
    return Story(**{**asdict(story), "link": response.url, "article_text": text})


def render_story(story: Story) -> str:
    parts = [f"- **{story.title}** ({story.source})", f"  - Source: {story.link}"]
    if story.published:
        parts.append(f"  - Published: {story.published}")
    if story.extraction_note:
        parts.append(f"  - Review note: {story.extraction_note}")
    if story.article_text:
        parts.extend(["  - Extracted text:", "", "```text", story.article_text, "```"])
    return "\n".join(parts)


def build_prompt_pack(config: dict, selected: dict[str, list[Story]], guide: str) -> str:
    lines = [
        f"# {config['publication_name']} source pack",
        "",
        "## Editorial guidance",
        "",
        guide.strip(),
        "",
    ]
    labels = {
        "news": "News candidates",
        "take_action": "Take-action candidates",
        "local_spotlight": "Local-spotlight candidates",
    }
    for key, label in labels.items():
        lines.extend([f"## {label}", ""])
        stories = selected[key]
        lines.append("\n\n".join(render_story(story) for story in stories) if stories else "_No candidates selected._")
        lines.append("")
    lines.extend([
        "## Drafting request",
        "",
        "Create a concise Markdown draft using only supported facts from these sources. Keep source links, preserve uncertainty, and flag anything that needs human verification.",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--input", type=Path, help="Optional offline JSON story fixture")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--full-text", action="store_true")
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    stories = load_fixture(args.input) if args.input else fetch_feeds(config.get("feed_urls", []))
    stories = dedupe(stories)
    selected = select_candidates(stories, config.get("locality_keywords", []), config.get("limits", {}))
    if args.full_text:
        selected = {key: [extract_text(story) for story in value] for key, value in selected.items()}

    guide_path = args.config.parent.parent / config["editorial_guide"]
    guide = guide_path.read_text(encoding="utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build_prompt_pack(config, selected, guide), encoding="utf-8")
    counts = ", ".join(f"{key}={len(value)}" for key, value in selected.items())
    print(f"Wrote {args.output} ({counts})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
