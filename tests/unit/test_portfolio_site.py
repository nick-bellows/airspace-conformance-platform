"""Guard the recruiter-facing static site against stale or broken evidence."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "docs" / "index.html"
RESULTS = ROOT / "eval" / "results" / "conflict_detection.json"


class _SiteParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(values["id"] or "")
        if tag == "a" and values.get("href"):
            self.links.add(values["href"] or "")


def test_tour_has_stable_anchors_and_direct_evidence_links() -> None:
    markup = SITE.read_text(encoding="utf-8")
    parser = _SiteParser()
    parser.feed(markup)

    assert {"tour", "system-replay", "results", "architecture"} <= parser.ids
    assert any(link.endswith("/src/acp/common/contracts.py") for link in parser.links)
    assert any(link.endswith("/tests/integration/test_messaging.py") for link in parser.links)
    assert any(link.endswith("/tests/e2e/test_pipeline.py") for link in parser.links)


def test_replay_asset_is_local_and_bounded() -> None:
    replay = ROOT / "docs" / "assets" / "replay.json"
    assert replay.is_file()
    assert replay.stat().st_size < 1_000_000


def test_github_source_links_target_tracked_paths() -> None:
    parser = _SiteParser()
    parser.feed(SITE.read_text(encoding="utf-8"))
    prefix = "https://github.com/nick-bellows/airspace-conformance-platform/blob/main/"
    source_links = [link for link in parser.links if link.startswith(prefix)]
    assert source_links
    for link in source_links:
        parts = urlparse(link).path.strip("/").split("/")
        assert (ROOT.joinpath(*parts[4:])).is_file(), link


def test_published_conflict_metrics_match_retained_evidence() -> None:
    evidence = json.loads(RESULTS.read_text(encoding="utf-8"))
    markup = SITE.read_text(encoding="utf-8")

    summary = evidence["summary"]
    expected = (
        f"{summary['recall']:.2f}",
        f"{summary['precision']:.2f}",
        f"{summary['lead_time_s']['median']:.0f} s",
        f"{summary['scenarios']} scenarios / {summary['simulated_hours']:.2f} h",
    )
    for value in expected:
        assert value in markup


def test_accessibility_controls_are_explicit() -> None:
    markup = SITE.read_text(encoding="utf-8")
    assert ":focus-visible" in markup
    assert "prefers-reduced-motion: reduce" in markup
    assert 'aria-label="Scrub through the replay"' in markup
