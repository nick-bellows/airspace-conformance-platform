# M0 — Scaffold

What exists, why it was built that way, and where it is weak. Written for you,
not for a reader of the repository.

## The one-sentence version

Nothing runs yet. What exists is the agreement every future service will be held
to: the message formats, the geometry those messages describe, and five
automated checks that fail the build when either is broken.

## What was built

### 1. Wire contracts — `src/acp/common/contracts.py`

Four message types, one per Kafka topic:

| Topic | Message | Carries |
| --- | --- | --- |
| `surveillance.reports.v1` | `SurveillanceReport` | One noisy position report from the sensor layer |
| `tracks.updates.v1` | `TrackUpdate` | One filtered state estimate from the tracker |
| `airspace.alerts.v1` | `Alert` | One advisory, with the evidence behind it |
| `sim.truth.v1` | `TruthState` | Noiseless simulator state, for evaluation only |

Three deliberate properties:

- **Frozen.** A message cannot be modified after it is built. If two handlers
  hold the same object, neither can surprise the other.
- **`extra="forbid"`.** An unknown field is an error, not something quietly
  ignored. This is what makes the drift gate below possible.
- **Reports are optional-heavy, track updates are not.** A real ADS-B message
  frequently omits velocity or altitude, so those fields are nullable on
  `SurveillanceReport` and downstream code cannot assume they are present. A
  `TrackUpdate` always carries full kinematics, because the filter always has an
  estimate — even while coasting through a dropout. The difference between the
  two models *is* the difference between an observation and an estimate.

**If asked why the distinction matters:** conflating "we did not observe it"
with "it is zero" is how a tracker ends up reporting a stationary aircraft at
FL350. Making one model nullable and the other not moves that mistake from
runtime to type-check time.

### 2. Geometry — `src/acp/common/geodesy.py`

Great-circle distance, bearing, and forward projection, plus a flat-earth
projection used inside the conflict search.

The interesting decision is having **two** coordinate treatments. Conflict
detection is pairwise, so it is O(n²) in the worst case; doing several
trigonometric calls per pair per second does not scale. The local tangent-plane
projection flattens a neighbourhood to plain Cartesian nautical miles, and the
test suite asserts it stays within 0.1% of the great-circle answer at short
range. That is far below the sensor noise already in the data.

**Two real bugs, found by property tests before any service existed.** Both are
worth knowing in detail because they are the best evidence in the repo that the
testing approach is doing work:

1. `normalize_bearing(-3.98e-64)` returned exactly `360.0`. The true answer is
   `360 - 3.98e-64`, which has no float64 representation and rounds up. The
   `Bearing` contract type is bounded `lt=360`, so this value would have failed
   validation the moment it hit a message — an intermittent, input-dependent
   crash in production.
2. `to_local_enu` reported ~21,000 NM of separation for two aircraft a few miles
   apart either side of the antimeridian, because it took the difference in
   longitude without wrapping it. Two converging aircraft near the date line
   would have been silently excluded from conflict detection. **A missed alert,
   not a crash** — the failure mode you never notice.

Neither would have been found by example-based tests, because nobody writes the
example `-3.98e-64`. Hypothesis did, in under a second.

### 3. Executable architecture rules — `tests/unit/test_architecture.py`

Parses the abstract syntax tree of every source file and fails if a service
imports another service, or if the shared library imports upward into its own
consumers.

**Why this exists:** "microservices" is a claim about coupling. A README can
claim it; only a test can keep it true. The first time someone needs a function
that happens to live in another service, the shortcut is one import away and
nobody notices for months. Now CI notices immediately and names the file.

It also caught something unplanned on the first run: three `__init__.py` files
written through PowerShell had UTF-8 byte-order marks, which broke the AST
parser. There is now an explicit test for that too.

### 4. Contract drift gate — `scripts/contracts.py`

Generates a JSON Schema per topic into `contracts/`, committed to the
repository. Running it with `--check` regenerates them in memory and compares.

**The scenario it prevents:** someone adds a field to `TrackUpdate`, the
producer starts emitting it, and a consumer with `extra="forbid"` rejects every
message. Because the schemas are committed artefacts, the model change and the
schema change must land in the same commit or the build fails. This is the
"contract testing" line item in the job description, done without needing a
schema registry to be running.

### 5. Quality gate — `scripts/run_checks.ps1` and `.github/workflows/quality.yml`

Five checks, identical locally and in CI: ruff (now including flake8-bandit
security rules), ruff format, mypy strict, pytest with an 80% coverage floor,
and the contract drift gate. Currently 83 tests, 100% coverage, all green.

The local script and the workflow deliberately run the same commands. A local
script that drifts from CI is worse than not having one.

### 6. Development stack — `deploy/compose.yml`

Redpanda (Kafka protocol, single container), Postgres 17, Redis 8. Everything
binds to `127.0.0.1` only. Image tags are pinned to exact versions, because
`latest` makes a green build unreproducible next week.

Redpanda has two listeners — `redpanda:9092` for containers and
`localhost:19092` for your machine. This trips people up constantly: a Kafka
client connects to a bootstrap address, then gets *redirected* to whatever
address the broker advertises. Get the advertised address wrong and the client
connects fine and then hangs forever trying to reach a hostname it cannot
resolve.

## Where this is weak

- **Nothing runs.** M0 is entirely infrastructure and agreements. The first
  moving aircraft is M1.
- **The architecture tests report 29 skips.** Each parametrised rule skips the
  files it does not govern. Correct, but noisy; if it becomes annoying it can be
  restructured to filter before parametrising.
- **Docker was not running on this machine**, so `docker compose up` has not
  actually been executed — only `docker compose config`, which validates syntax
  and references but starts nothing. That gets verified at M1.
- **Coverage is 100%, which is not as impressive as it sounds.** There are 220
  statements and no I/O yet. The number will fall once there are services with
  network calls in them, and that is fine. Watch the trend, and do not chase the
  number.

## Questions a reviewer might ask

**"Is this really microservices, or a monolith in four folders?"**
Fair challenge, and ADR 0003 concedes the real part of it. They are independently
deployable processes with separate images and separate Deployments,
communicating only over Kafka — and a test enforces that. What it does *not*
demonstrate is independent versioning and staged rollout of a contract change,
because everything releases together from one repository. That is a genuine gap,
and it is written down rather than glossed over.

**"Why JSON on the wire instead of Avro or Protobuf?"**
JSON with committed JSON Schema is readable in the Redpanda console during a
demo, needs no schema registry running, and no code generation step. Avro would
be smaller and would give registry-enforced compatibility checking. At 1 Hz per
aircraft, message size is not the constraint. If throughput became the
constraint, that is the first thing to change.

**"Why a spherical earth rather than WGS-84?"**
The ellipsoidal correction is around 0.3%. Simulated sensor noise is larger than
that, so it would be precision the rest of the system cannot use. It also keeps
the property tests exact enough to assert genuine invariants instead of loose
tolerances.

## Next

M1 — the walking skeleton: straight-line traffic flowing all the way from the
simulator to a live display, with `docker compose up` working from a clean
clone. Thin on purpose; every later milestone deepens a path that already runs
end to end.
