# ADR 0008 — No generative model in the runtime path

Status: accepted · Date: 2026-08-15

## Context

This is an "AI-enabled" system in a safety-adjacent domain, built in a period
when the default assumption about adding AI to a product is that it means adding
a large language model. Several places in this system look like they would
accept one:

- turning structured alerts into readable incident narratives;
- summarising an airspace picture for a shift handover;
- explaining why a conflict was predicted, in prose;
- classifying manoeuvres from a track description.

None of these are absurd. Each would demonstrate LLM integration, which is
visibly in demand.

## Decision

No generative model anywhere in the runtime path. The intelligence in this
system is a 3,523-parameter neural network predicting a residual, and it sits
behind a physics baseline it degrades to on any failure.

## Consequences

**The reasons, in order of weight:**

1. **Determinism is a property of the whole pipeline and would be lost.** Every
   number this project publishes is reproducible from a seed — the simulator,
   the scenario generator, the split, the training, the evaluation. A generative
   model in the path makes the output non-reproducible in principle, and a
   safety advisory that cannot be reproduced cannot be investigated after the
   fact.
2. **Latency is a stated requirement.** The conflict path has a budget measured
   in seconds end to end. A network round trip to an inference API is not
   compatible with that, and a local model large enough to be worth calling is
   not compatible with the CPU footprint.
3. **The failure modes do not compose with the design.** The whole serving path
   is built so that anything going wrong degrades to physics. "Degrade to
   physics" is not available for a component whose output is prose, and a
   fluent, confident, wrong narrative attached to a safety alert is worse than
   no narrative — it is the failure mode most likely to be believed.
4. **Nothing here needs generation.** The alerts are structured data with reason
   codes. A template renders them into a sentence deterministically, and that
   sentence can be tested. Language generation would add a way to be wrong
   without adding a capability.

**What is given up.** A demonstrable LLM integration, which is a real cost for a
portfolio project in 2026. The judgement is that a deliberate, written argument
for *not* using one is the stronger signal in a domain where "performance,
precision, and reliability matter every second".

**Where a generative model would be legitimate**, and would be added first if it
were added at all: entirely **offline**, outside the runtime path, over
already-published structured alerts, with the structured record remaining the
source of truth and the prose explicitly marked as derived. Post-incident
narrative generation for a human reviewer is a genuine use. It is not what this
system does today.

**AI-assisted development is a separate question** and is not restricted by this
decision. This codebase was written with heavy AI assistance; see
`docs/ai-assisted-development.md`. The distinction is between using a generative
model to *write* software that is then reviewed and tested, and putting one
inside the software where its output cannot be reviewed before it is acted on.
