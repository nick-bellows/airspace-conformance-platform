# M6 — Make it legible

What exists, why it was built that way, and where it is weak.

## The one-sentence version

Three artefacts, no new system behaviour: a demo animation generated from the
real simulator, one page of planning and delivery material, and an interview
brief — plus a substantial reduction in the documentation, which had grown to
4,782 lines against 8,015 lines of source.

## Why this milestone is small on purpose

The plan originally called for a full Program Increment's worth of Agile
artefacts: PI plan, features decomposed into stories, per-iteration review and
retrospective notes, a GitHub Project board with story IDs in commit messages.

That was scoped down to **one page**. A solo project cannot honestly produce
standup notes for a team of one, and manufacturing them would be the exact
failure this repository spends most of its documentation avoiding: an artefact
that looks like evidence and is not. What survived is the part that genuinely
shaped the work — objectives with acceptance criteria, a definition of done that
was enforced, and a risk log whose entries are real risks the project hit.

[`agile.md`](../agile.md) says this in its first paragraph rather than hoping
nobody notices the missing ceremony.

## The demo — `scripts/make_demo.py`

**Generated, not recorded.** A screen capture is a one-off nobody can reproduce
or diff, it dates the moment the display changes, and it captures whatever
happened to be on screen. This runs the same simulator, the same Kalman filter,
and the same separation monitor the services run, from the committed seed, and
renders 150 frames to an animated GIF.

The consequence worth having: **the demo cannot drift from the system.** If the
detector changes, `python scripts/make_demo.py` produces a picture of the new
behaviour. If it broke, the picture would show it broke.

It deliberately does not go through Kafka, Postgres, or Redis. Those are
transport; the demo is about what the algorithms produce, and `tests/e2e` covers
the wiring. Pillow lives in its own `demo` extra so no imaging library reaches
the service image.

What the animation shows, in order: four aircraft on a dark plan view with 20 NM
grid; two of them converging head-on at FL350; at T+02:38 both ring red and an
advisory names them with the predicted separation and time to closest approach;
the countdown runs down through the crossing; the alert clears as they diverge.
The two non-conflicting aircraft are there precisely so the detector has traffic
to *not* alert on.

### Two defects in building it

**`ImageSequence.Iterator` yields the same mutated object.** Extracting frames
with `list(ImageSequence.Iterator(gif))` gives a list of N references to one
image sitting at its final position — so every "frame" I inspected was the last
one, showing T+15:00 and no alert. It looked exactly like a demo where the
conflict never fired. `seek()` plus `copy()` is the fix, and the lesson is the
familiar one: the tool was fine and the way I looked at it was wrong.

**Labels collided at exactly the interesting moment.** Two aircraft converging to
0.0 NM have their callsigns in the same pixels at closest approach, which is the
frame most likely to be screenshotted. Labels now alternate above and below the
marker and are clamped inside the frame, so nothing runs under the alert banner
or off the edge.

## The interview brief — `docs/interview-brief.md`

Twenty-odd anticipated questions with answers, a walkthrough script, and a
section titled "things not to do in the room" — of which the first is *don't lead
with the recall of 1.00*. The good numbers survive scrutiny better when the weak
one has already been named.

It is explicitly not part of the system, and says so in its first line.

## The documentation reduction

The larger half of this milestone. Markdown had reached 4,782 lines — 0.6 lines
of prose per line of source — because three rounds of external review had each
added a layer of findings, and the README roadmap alone had grown to 306 lines,
48% of the file. A reader with ten minutes was getting the results and then a
production backlog for a system that will never ship.

| | Before | After |
| --- | --- | --- |
| README | 636 | ~360 |
| `operations.md` | 467 | 288 |
| `limitations.md` | 347 | 251 |
| Total markdown | 4,782 | ~3,600 |

Two structural changes did most of it:

**All "what we would do next" content moved into one file.**
[`future-work.md`](../future-work.md) is now the single home for improvement and
expansion, in four parts: the one thing worth building next, engineering that
scale would force, operations production would require, and what is deliberately
declined. It had previously been spread across the README roadmap,
`limitations.md`, and three walkthroughs, which is how it grew without anyone
deciding it should.

**`limitations.md` went back to being limitations.** It had accreted a second
roadmap — "an outbox is real work", "sharding is the answer". Those are plans.
What belongs there is what the published numbers do and do not support, which is
the artefact that distinguishes this repository from most portfolio projects.

The six milestone walkthroughs were briefly consolidated into
[`how-it-was-built.md`](../how-it-was-built.md) and then restored, so both exist:
the overview is the page to read first, and these are where to go for detail.

## What this milestone does not do

- **The pipeline still has never run.** M6 does not change that, and M5 stays
  unchecked.
- **The demo shows one scenario.** `unannounced-turn` and `quiet-cruise` are
  arguably more interesting — a conformance advisory and a deliberate
  non-event — and neither is rendered.
- **No GitHub Pages replay.** A static frame-log replay driven by the existing
  display was considered and demoted: the recording communicates the same thing
  to the same reader for a fraction of the work.

## Questions a reviewer might ask

**"Is the GIF real or a mock-up?"** Real, and reproducible — regenerate it with
`python scripts/make_demo.py` and diff the output. It imports `Simulation`,
`TrackEstimator`, and `SeparationMonitor` directly.

**"Why is there so little Agile material?"** Because there was one developer.
The objectives, acceptance criteria, definition of done, and ROAM log are the
parts that survive contact with a team of one; the rest would be fabricated. The
definition of done is the interesting artefact — two of its seven rules exist
because the project broke them first.

**"Why did the documentation need cutting?"** Because it had reached 0.6 lines
per line of code, and because 48% of the README was a backlog. Documentation
growth turned out to be this project's most persistent failure mode — every
review round added prose, and nothing removed any until it was measured.
