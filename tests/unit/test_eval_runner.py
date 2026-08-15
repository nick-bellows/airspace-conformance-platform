"""Meta-test: the evaluation harness itself.

A committed metric is only credible if anyone can regenerate it. This runs the
real evaluation end to end on a small scenario set and checks the report comes
back with the fields that make it auditable -- component versions, the seed, and
the hash of the scenario set it scored.

It also pins the two definitions that decide what the headline numbers mean. If
someone later relaxes "detected" to include alerts raised *after* separation was
lost, recall jumps and the number silently stops meaning what the report says it
means. That is exactly the kind of change that should have to break a test.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from eval.conflict_truth import EVENT_BRIDGE_S, ConflictEvent, TruthConflictFinder
from eval.run_conflict_eval import RaisedAlert, ScenarioOutcome, summarise

from acp.common.contracts import TruthState

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def a_truth_state(icao24: str, *, lat: float, lon: float, altitude_ft: float) -> TruthState:
    return TruthState(
        icao24=icao24,
        scenario_id="test",
        sim_version="test",
        valid_at=NOW,
        lat=lat,
        lon=lon,
        altitude_ft=altitude_ft,
        ground_speed_kt=450.0,
        track_deg=90.0,
        vertical_rate_fpm=0.0,
        phase="cruise",
    )


# --------------------------------------------------------------------------
# Ground truth
# --------------------------------------------------------------------------


def test_a_violation_needs_both_standards_breached() -> None:
    """Same rule as the detector, applied to truth. If these disagreed, the
    metric would be scoring the detector against a different question."""
    finder = TruthConflictFinder()
    finder.observe(
        (
            a_truth_state("aaaaaa", lat=40.0, lon=-75.0, altitude_ft=35000.0),
            # 1 NM apart laterally but 4000 ft above: legal.
            a_truth_state("bbbbbb", lat=40.0167, lon=-75.0, altitude_ft=39000.0),
        ),
        NOW,
    )
    assert finder.events() == []


def test_a_real_violation_is_recorded() -> None:
    finder = TruthConflictFinder()
    finder.observe(
        (
            a_truth_state("aaaaaa", lat=40.0, lon=-75.0, altitude_ft=35000.0),
            a_truth_state("bbbbbb", lat=40.0167, lon=-75.0, altitude_ft=35200.0),
        ),
        NOW,
    )
    events = finder.events()
    assert len(events) == 1
    assert events[0].pair == ("aaaaaa", "bbbbbb")


def test_consecutive_violations_are_one_event() -> None:
    """One encounter dipping below the standard for 90 s is one thing that
    happened, not 90. Counting instants would inflate the event count and make
    recall meaningless."""
    finder = TruthConflictFinder()
    for second in range(90):
        finder.observe(
            (
                a_truth_state("aaaaaa", lat=40.0, lon=-75.0, altitude_ft=35000.0),
                a_truth_state("bbbbbb", lat=40.0167, lon=-75.0, altitude_ft=35200.0),
            ),
            NOW + timedelta(seconds=second),
        )
    events = finder.events()
    assert len(events) == 1
    assert (events[0].ended_at - events[0].started_at).total_seconds() == 89


def test_violations_far_apart_in_time_are_separate_events() -> None:
    finder = TruthConflictFinder()
    for offset in (0.0, EVENT_BRIDGE_S * 4):
        finder.observe(
            (
                a_truth_state("aaaaaa", lat=40.0, lon=-75.0, altitude_ft=35000.0),
                a_truth_state("bbbbbb", lat=40.0167, lon=-75.0, altitude_ft=35200.0),
            ),
            NOW + timedelta(seconds=offset),
        )
    assert len(finder.events()) == 2


def test_a_brief_gap_does_not_split_one_encounter_in_two() -> None:
    """An encounter that pops above the standard for a few seconds and drops
    back is one continuous problem. Splitting it would double-count."""
    finder = TruthConflictFinder()
    for offset in (0.0, EVENT_BRIDGE_S / 2):
        finder.observe(
            (
                a_truth_state("aaaaaa", lat=40.0, lon=-75.0, altitude_ft=35000.0),
                a_truth_state("bbbbbb", lat=40.0167, lon=-75.0, altitude_ft=35200.0),
            ),
            NOW + timedelta(seconds=offset),
        )
    assert len(finder.events()) == 1


# --------------------------------------------------------------------------
# Scoring definitions
# --------------------------------------------------------------------------


def an_event(pair: tuple[str, str] = ("aaaaaa", "bbbbbb"), *, at: datetime = NOW) -> ConflictEvent:
    return ConflictEvent(
        first_icao24=pair[0],
        second_icao24=pair[1],
        started_at=at,
        ended_at=at + timedelta(seconds=30),
        min_horizontal_nm=2.0,
        min_vertical_ft=200.0,
    )


def test_an_alert_raised_after_the_violation_is_not_a_detection() -> None:
    """A warning that arrives once the aircraft are already too close has not
    done its job, and counting it would flatter recall."""
    outcome = ScenarioOutcome(
        scenario_id="t",
        seed=1,
        duration_s=600.0,
        truth_events=[an_event(at=NOW)],
        alerts=[
            RaisedAlert(key="k", pair=("aaaaaa", "bbbbbb"), raised_at=NOW + timedelta(seconds=10))
        ],
    )
    assert outcome.detected == 0


def test_an_alert_raised_before_the_violation_is_a_detection() -> None:
    outcome = ScenarioOutcome(
        scenario_id="t",
        seed=1,
        duration_s=600.0,
        truth_events=[an_event(at=NOW + timedelta(seconds=100))],
        alerts=[RaisedAlert(key="k", pair=("aaaaaa", "bbbbbb"), raised_at=NOW)],
    )
    assert outcome.detected == 1


def test_an_alert_for_a_pair_that_never_conflicted_is_a_false_alert() -> None:
    outcome = ScenarioOutcome(
        scenario_id="t",
        seed=1,
        duration_s=3600.0,
        truth_events=[],
        alerts=[RaisedAlert(key="k", pair=("cccccc", "dddddd"), raised_at=NOW)],
    )
    assert outcome.false_alerts == 1


def test_the_summary_reports_rates_as_well_as_counts() -> None:
    outcome = ScenarioOutcome(
        scenario_id="t",
        seed=1,
        duration_s=3600.0,
        truth_events=[],
        alerts=[RaisedAlert(key="k", pair=("cccccc", "dddddd"), raised_at=NOW)],
    )
    summary = summarise([outcome])
    assert summary["false_alerts_per_hour"] == 1.0
    assert summary["simulated_hours"] == 1.0
    # No truth events means recall is undefined, not zero. Reporting 0.0 would
    # be a claim the data cannot support.
    assert summary["recall"] is None


# --------------------------------------------------------------------------
# The report regenerates
# --------------------------------------------------------------------------


def test_the_evaluation_reproduces_and_stamps_its_inputs(tmp_path: Path) -> None:
    """Runs the real harness end to end.

    Output goes to a temporary directory: a four-scenario smoke run must not
    overwrite the committed result, which was produced from 122.
    """
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "eval/run_conflict_eval.py",
            "--scenarios",
            "4",
            "--seed",
            "1",
            "--output-dir",
            str(tmp_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr

    payload = json.loads((tmp_path / "conflict_detection.json").read_text())
    # Every version that could change a number is stamped on the report, so a
    # stale result is identifiable rather than merely suspicious.
    for field in (
        "sim_version",
        "generator_version",
        "filter_version",
        "detector_version",
        "scenario_set_sha256_16",
        "seed",
    ):
        assert payload[field], f"report is missing {field}"
    assert payload["summary"]["scenarios"] == 4
