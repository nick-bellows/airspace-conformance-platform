"""Render the head-on conflict scenario to an animated GIF.

## Why generate it rather than screen-record it

A screen recording is a one-off artefact nobody can reproduce or diff, it dates
the moment the display changes, and it captures whatever happened to be on the
screen. This runs the same simulator, the same Kalman filter, and the same
separation monitor the services run, from the same committed seed -- so the
picture is reproducible, regenerable after a change, and demonstrably the real
system rather than a mock-up of it.

It deliberately does **not** go through Kafka, Postgres, or Redis. Those are
transport; the demo is about what the algorithms produce. `tests/e2e` covers the
wiring.

    python scripts/make_demo.py --out docs/assets/demo.gif

Requires the `demo` extra (Pillow).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from acp.common.geodesy import to_local_enu
from acp.services.conformance.separation import SeparationMonitor
from acp.services.track.estimator import TrackEstimator
from acp.sim.engine import Simulation
from acp.sim.scenario import load_scenario

REPO_ROOT = Path(__file__).resolve().parents[1]

# Lifted from the display's stylesheet so the GIF and the live page agree.
IN_K = (5, 8, 11)
GRID = (20, 32, 42)
TEXT = (215, 227, 234)
DIM = (111, 133, 147)
BLUE = (74, 168, 255)
AMBER = (224, 163, 58)
RED = (255, 107, 94)

WIDTH, HEIGHT = 720, 420
MARGIN = 56
TRAIL = 120  # seconds of history drawn behind each aircraft
TRAIL_COLOUR = (38, 62, 82)

# Candidate label positions relative to the marker, tried in order: right then
# left, near then far. Four aircraft can meet at a point in this scenario, so
# there are more slots than aircraft.
LABEL_SLOTS = (
    (12, -16),
    (12, 8),
    (-56, -16),
    (-56, 8),
    (12, -30),
    (12, 22),
)


def _overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


@dataclass
class Track:
    callsign: str
    points: list[tuple[float, float]] = field(default_factory=list)
    conflicted: bool = False


def _project(
    lat: float, lon: float, ref: tuple[float, float], span_nm: float
) -> tuple[float, float]:
    """Aircraft position to pixels, north up."""
    east, north = to_local_enu(lat, lon, ref[0], ref[1])
    scale = (min(WIDTH, HEIGHT) - 2 * MARGIN) / span_nm
    return WIDTH / 2 + east * scale, HEIGHT / 2 - north * scale


def _draw_frame(
    tracks: dict[str, Track],
    alert: str | None,
    elapsed_s: float,
    ref: tuple[float, float],
    span_nm: float,
) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), IN_K)
    draw = ImageDraw.Draw(image)

    for offset in range(-3, 4):
        step = (min(WIDTH, HEIGHT) - 2 * MARGIN) / span_nm * 20.0  # 20 NM grid
        draw.line([(WIDTH / 2 + offset * step, 0), (WIDTH / 2 + offset * step, HEIGHT)], GRID)
        draw.line([(0, HEIGHT / 2 + offset * step), (WIDTH, HEIGHT / 2 + offset * step)], GRID)

    placed: list[tuple[float, float, float, float]] = []
    for track in sorted(tracks.values(), key=lambda t: t.callsign):
        if not track.points:
            continue
        colour = RED if track.conflicted else BLUE
        if len(track.points) > 1:
            draw.line(
                [_project(la, lo, ref, span_nm) for la, lo in track.points],
                TRAIL_COLOUR,
                width=2,
            )
        x, y = _project(*track.points[-1], ref, span_nm)
        if track.conflicted:
            draw.ellipse([x - 14, y - 14, x + 14, y + 14], outline=RED, width=2)
        draw.ellipse([x - 4, y - 4, x + 4, y + 4], fill=colour)
        # Place the label in the first candidate slot that is not already
        # taken. Alternating two slots by index was enough while only a
        # converging *pair* met at a point; `head-on-conflict` now sends a
        # third aircraft through the same point 4000 ft above -- the whole
        # reason that aircraft exists -- and three labels in two slots overlap
        # into an unreadable smear at exactly the frame worth screenshotting.
        for dx, dy in LABEL_SLOTS:
            # Clamp inside the frame so a label never runs under the alert
            # banner or off the right edge -- both happen at the geometry that
            # matters.
            label_x = min(max(x + dx, 4), WIDTH - 60)
            label_y = min(max(y + dy, 6), HEIGHT - 74)
            box = (label_x, label_y, label_x + 44, label_y + 12)
            if not any(_overlaps(box, taken) for taken in placed):
                break
        placed.append(box)
        draw.text((label_x, label_y), track.callsign, fill=colour)

    draw.text((MARGIN // 2, 14), "AIRSPACE CONFORMANCE PLATFORM", fill=DIM)
    draw.text(
        (WIDTH - MARGIN * 2, 14),
        f"T+{int(elapsed_s // 60):02d}:{int(elapsed_s % 60):02d}",
        fill=DIM,
    )
    draw.text(
        (MARGIN // 2, HEIGHT - 24), "synthetic traffic  ·  advisory only  ·  20 NM grid", fill=DIM
    )

    if alert:
        draw.rectangle([0, HEIGHT - 58, WIDTH, HEIGHT - 34], fill=(18, 6, 10))
        draw.text((MARGIN // 2, HEIGHT - 52), f"PREDICTED CONFLICT   {alert}", fill=AMBER)
    return image


def build(
    scenario_path: Path,
    out: Path,
    *,
    every: int,
    span_nm: float,
    frames_out: Path | None = None,
) -> None:
    scenario = load_scenario(scenario_path)
    simulation = Simulation(scenario, datetime(2026, 8, 16, 12, 0, tzinfo=UTC))
    estimator = TrackEstimator()
    monitor = SeparationMonitor(horizontal_nm=5.0, vertical_ft=1000.0, lookahead_s=300.0)
    ref = (scenario.reference.lat, scenario.reference.lon)

    tracks: dict[str, Track] = {}
    frames: list[Image.Image] = []
    replay: list[dict[str, Any]] = []
    step = 0

    while not simulation.finished:
        simulation.advance(scenario.sensor.report_interval_s)
        step += 1

        updates = [estimator.on_report(report) for report in simulation.observe()]
        for update in updates:
            track = tracks.setdefault(update.track_id, Track(update.callsign or update.icao24))
            track.points.append((update.lat, update.lon))
            track.points = track.points[-TRAIL:]
            track.conflicted = False

        conflicts = monitor.scan(updates)
        alert = None
        if conflicts:
            worst = min(conflicts, key=lambda c: c.time_to_cpa_s)
            for track_id in (worst.first_track_id, worst.second_track_id):
                if track_id in tracks:
                    tracks[track_id].conflicted = True
            alert = (
                f"{worst.first_callsign} / {worst.second_callsign}   "
                f"{worst.min_horizontal_nm:.1f} NM / {int(worst.min_vertical_ft)} ft "
                f"in {int(worst.time_to_cpa_s)}s"
            )

        if step % every == 0:
            elapsed = step * scenario.sensor.report_interval_s
            frames.append(_draw_frame(tracks, alert, elapsed, ref, span_nm))
            if frames_out is not None:
                # The same state the GIF frame was drawn from, as data. The web
                # replay renders this in a canvas, so the page and the animation
                # cannot disagree about what happened -- they come from one run.
                replay.append(
                    {
                        "t": round(elapsed, 1),
                        "alert": alert,
                        "aircraft": [
                            {
                                "callsign": track.callsign,
                                "conflicted": track.conflicted,
                                "trail": [
                                    [round(e, 3), round(n, 3)]
                                    for e, n in (
                                        to_local_enu(la, lo, ref[0], ref[1])
                                        for la, lo in track.points[-30:]
                                    )
                                ],
                            }
                            for track in sorted(tracks.values(), key=lambda x: x.callsign)
                            if track.points
                        ],
                    }
                )

    out.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        out,
        save_all=True,
        append_images=frames[1:],
        duration=90,
        loop=0,
        optimize=True,
    )
    size_kb = out.stat().st_size / 1024
    print(f"{out}: {len(frames)} frames, {size_kb:.0f} KB")

    if frames_out is not None:
        frames_out.parent.mkdir(parents=True, exist_ok=True)
        payload = {"span_nm": span_nm, "interval_ms": 90, "frames": replay}
        frames_out.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        print(f"{frames_out}: {len(replay)} frames, {frames_out.stat().st_size / 1024:.0f} KB")
    if size_kb > 4096:
        print("  warning: over 4 MB; raise --every or shorten the scenario")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="make-demo", description=__doc__)
    parser.add_argument(
        "--scenario", type=Path, default=REPO_ROOT / "scenarios/head-on-conflict.yaml"
    )
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "docs/assets/demo.gif")
    parser.add_argument(
        "--every", type=int, default=6, help="render one frame per N simulated seconds"
    )
    parser.add_argument("--span-nm", type=float, default=95.0, help="width of the view")
    parser.add_argument(
        "--frames-out",
        type=Path,
        default=REPO_ROOT / "docs/assets/replay.json",
        help="also write the replay log the web page plays back",
    )
    args = parser.parse_args(argv)

    build(
        args.scenario,
        args.out,
        every=args.every,
        span_nm=args.span_nm,
        frames_out=args.frames_out,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
