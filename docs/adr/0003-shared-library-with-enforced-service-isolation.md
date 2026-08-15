# ADR 0003 — One repository, one shared library, four isolated services

Status: accepted · Date: 2026-08-15

## Context

Four services need the same wire contracts, the same geometry, the same
configuration, and the same log format. There are three ways to arrange that.

1. **Four repositories, a published shared package.** What a real programme
   does. Requires a package registry, version pinning across four repos, and a
   release dance every time a contract changes.
2. **One repository, four independent packages.** Realistic, but four
   `pyproject.toml` files and four dependency graphs for a portfolio project is
   ceremony without benefit.
3. **One repository, one installable package, four entry points.** Each service
   is a module with its own `__main__`, its own container image, and its own
   Kubernetes Deployment.

The risk in option 3 is real: with everything importable, nothing stops a
developer from calling into another service directly, and the architecture
quietly becomes a monolith while the README still says "microservices".

## Decision

Option 3 — one `pyproject.toml`, one `acp` package — **with the isolation rule
enforced by a test rather than by good intentions.**

`tests/unit/test_architecture.py` parses the AST of every module under `src/acp`
and fails the build if:

- `acp.common` imports anything from `acp.services`, `acp.sim`, or `acp.ml`;
- any module under `acp.services.<x>` imports from `acp.services.<y>`;
- `acp.sim` imports from `acp.services`.

## Consequences

- The "microservices" claim is checkable in ten seconds by running one test,
  rather than being an assertion in a README. If someone takes the shortcut, CI
  says so with the offending module named.
- Contract changes are atomic: the model, the regenerated JSON Schema, and every
  consumer move in one commit. In a four-repo layout the same change is four
  pull requests and a compatibility window.
- Services remain independently deployable and independently scalable. They
  share a library, not a process — in the same sense that two Java services
  sharing a JAR are still two services.

**The honest caveat.** This is a monorepo with enforced module boundaries, not
four separately released artefacts. It does not exercise independent versioning,
staged rollout of a contract change, or the coordination problems those create.
Those are real parts of microservice work that this layout skips, and the
counter-argument — that services which cannot be released independently are not
really independent — is a fair one to raise.

**Rejected: four repositories.** Closest to production practice, and rejected
because the coordination overhead would dominate the build without demonstrating
anything the enforced boundaries do not already show.
