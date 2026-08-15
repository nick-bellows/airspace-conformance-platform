# AI-assisted development

This codebase was written with heavy AI assistance, using Claude Code. That is
worth stating plainly rather than leaving to be inferred, and worth separating
from a different question the project answers differently.

## Two distinct questions

**Using a generative model to write software** — which this project does
extensively, and which is the subject of this document.

**Putting a generative model inside the software** — which this project
deliberately does not do, for reasons recorded in
[ADR 0008](adr/0008-no-generative-model-in-the-safety-path.md).

The distinction is where the review happens. AI-written code is read, tested,
and typed before it runs. An AI component *inside* a system produces output that
is acted on before anyone can review it. The first is a productivity question;
the second is a safety question.

## What the assistance actually did

- Drafted implementations from a stated design, which were then reviewed line by
  line rather than accepted wholesale.
- Wrote the bulk of the test suite, including the property tests and the
  architecture fitness tests.
- Produced first drafts of the ADRs and the evaluation reports, which were
  edited for accuracy against the actual numbers.
- Ran the tooling — lint, type check, tests, Docker, evaluation runs — and fed
  the results back into the next change.

## Guardrails that were actually applied

**Every claim is checked against something that runs.** The most useful
discipline was refusing to let prose stand unverified. Concretely, this caught:

- A docstring claiming the flat-earth projection was accurate "to under 0.1%
  within 100 NM". Measuring it showed **1.3% at 100 NM** for the quantity the
  conflict detector actually computes. The claim was plausible, confidently
  written, and wrong. The corrected figures are now a table pinned by a test.
- A claim that whole-degree reference points let the conflict detector compare
  positions without re-projecting. It does re-project. The test written to
  confirm the claim failed and the docstring was corrected.

**Tests are written to fail first where it matters.** The architecture fitness
test caught its own author importing one service from another within a day of
being written. The NOMINAL scenario fingerprint test failed the first time it
ran, catching a reordered random draw that would have silently invalidated a
committed evaluation report.

**Metrics are never adjusted to look better.** Several results here are
unflattering and are reported as they came out:

- Conflict detection precision is 0.57, and the report leads with why rather
  than with the recall of 1.00.
- The trajectory model is *worse than the physics baseline* at predicting
  altitude at a 30 s horizon, and the model card says which to use instead.
- The constant-turn baseline is worse than assuming straight flight.

The evaluation harness has a meta-test that pins the definitions of "detected"
and "false alert", so relaxing them to improve a number has to break a test.

**Generated numbers never appear in prose until the generator is committed and
reproducible.** Every report is stamped with component versions, the seed, and a
hash of the input set.

## What went wrong

Worth recording, because "AI wrote it and it was fine" is not a useful account.

- **Plausible-but-wrong documentation was the dominant failure mode.** Not broken
  code — the type checker and tests catch that — but confident, specific, false
  claims in comments and docstrings. Both examples above are of this kind. The
  mitigation is measuring rather than reviewing: a claim about a number needs a
  script, not a careful reading.
- **A first attempt at the conformance threshold could never fire.** It scaled
  the threshold by the same sample's baseline error, so in physics-fallback mode
  the comparison reduced to `error > 3 × error`. It looked reasonable and was
  logically impossible. A test caught it.
- **A first attempt at stratifying the ML evaluation used the noisy filtered
  turn rate** and put three quarters of steady cruise into the "manoeuvring"
  bucket, making the model look useless. Then a second attempt merged turns with
  climbs, burying 907 hard samples under 3,121 easy ones. Both were caught by
  looking at the sample counts and asking whether they were plausible.

The pattern is consistent: the errors were not in code that fails loudly, but in
reasoning that produces a number or a sentence which looks right. The defence is
to make as many claims as possible executable.

## What this does not claim

No productivity metrics are offered. Counting AI-assisted commits or estimating
time saved would be unfalsifiable here, and this project's whole posture is that
unverifiable numbers do not get published.
