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


# --------------------------------------------------------------------------
# Counts that appear on more than one surface are counts that drift. These
# three had, twice each: the page said 553 tests while the README said 707 and
# the truth was 720; the page claimed "ten third-party actions" against eight;
# and the ADR badge sat at 11 and then 13 while the directory grew.
# --------------------------------------------------------------------------


def test_the_published_adr_count_matches_the_directory() -> None:
    count = len(list((ROOT / "docs" / "adr").glob("*.md")))
    assert f">{count} ADRs<" in SITE.read_text(encoding="utf-8"), (
        f"the page's ADR badge disagrees with the {count} files in docs/adr/"
    )


def test_the_published_action_count_matches_the_workflow() -> None:
    """Pinning actions by SHA is a real claim; the number attached to it should be too."""
    workflow = (ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")
    unique = {
        line.split("uses:")[1].split("@")[0].strip()
        for line in workflow.splitlines()
        if "uses:" in line
    }
    words = {
        1: "one",
        2: "two",
        3: "three",
        4: "four",
        5: "five",
        6: "six",
        7: "seven",
        8: "eight",
        9: "nine",
        10: "ten",
    }
    spelled = words.get(len(unique), str(len(unique)))
    assert f"All {spelled} third-party actions pinned" in SITE.read_text(encoding="utf-8"), (
        f"the page's action count disagrees with the {len(unique)} unique actions in the workflow"
    )


def test_the_page_and_the_readme_agree_on_the_test_count() -> None:
    """They diverged by 167 once, which is how you learn to check.

    Neither is the source of truth -- the suite is -- but the suite's count
    cannot be established cheaply from inside the suite. Agreement between the
    two published surfaces is the affordable half of the guarantee, and it is
    the half that failed.
    """
    import re

    site = re.search(r"(\d+)\+? unit and contract tests", SITE.read_text(encoding="utf-8"))
    readme = re.search(
        r"reports \d+% over (\d+)\+? tests", (ROOT / "README.md").read_text(encoding="utf-8")
    )
    assert site and readme, "both surfaces must state a test count in the expected shape"
    assert site.group(1) == readme.group(1), (
        f"the page says {site.group(1)} tests and the README says {readme.group(1)}"
    )
