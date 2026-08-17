"""Executable architecture rules.

Calling something a microservice is a claim about coupling, and a claim that is
only made in a README rots the first time someone takes a shortcut. These tests
read the import graph out of the source and fail the build if a service reaches
into a sibling.

The layering is deliberately boring:

    acp.common   <- contracts, geometry, config, logging; depends on nothing of ours
    acp.storage  <- Postgres and Redis access; may use common
    acp.sim      <- may use common
    acp.ml       <- may use common
    acp.services <- may use common, storage, sim, ml; never another service

Services communicate over Kafka and HTTP only. The shared packages are libraries
in the same sense a shared JAR is: they carry contracts and access code, not
behaviour that couples two services together.

**This test has already earned its place.** During M1 the API service was
written to import `acp.services.track.store`, because that is where the Postgres
and Redis code happened to live and the API needed to read the same rows. It was
the exact shortcut described above, taken by the author of the rule, and the
build caught it. The fix was to lift storage into its own layer that both
services may depend on -- see ADR 0004 for why a shared read model is the right
answer here and what it costs.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
PACKAGE_ROOT = SRC / "acp"


def _module_name(path: Path) -> str:
    """Dotted module name for a file under `src/`."""
    parts = path.relative_to(SRC).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imported_modules(path: Path) -> set[str]:
    """Every `acp.*` module referenced by an import statement in this file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    package = (
        _module_name(path).rsplit(".", 1)[0] if path.name != "__init__.py" else _module_name(path)
    )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.startswith("acp"))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: resolve against the containing package
                base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                found.add(f"{base}.{node.module}" if node.module else base)
            elif node.module and node.module.startswith("acp"):
                found.add(node.module)
    return found


def _third_party_roots(path: Path) -> set[str]:
    """Top-level package of every absolute import in this file.

    Separate from `_imported_modules`, which filters to `acp.*` because it
    exists to police *internal* dependency direction. A test about third-party
    dependencies needs the imports that one deliberately discards -- and the
    first version of this reused it, so it saw nothing and passed against a
    file with `import scipy.stats` sitting at the top.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            found.add(node.module.split(".")[0])
    return found


def _source_files() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def test_the_package_actually_has_source_files() -> None:
    """Guard against the rules below passing vacuously on an empty tree."""
    assert len(_source_files()) >= 5


@pytest.mark.parametrize("path", _source_files(), ids=_module_name)
def test_source_files_are_utf8_without_a_byte_order_mark(path: Path) -> None:
    """A BOM is invisible in an editor and breaks tooling that reads raw bytes.

    Windows PowerShell writes one by default, so this is a real hazard on this
    machine rather than a hypothetical.
    """
    assert not path.read_bytes().startswith(b"\xef\xbb\xbf"), f"{path} starts with a UTF-8 BOM"


def _imports_any_of(path: Path, forbidden: set[str]) -> set[str]:
    return {
        imported
        for imported in _imported_modules(path)
        if any(imported == f or imported.startswith(f + ".") for f in forbidden)
    }


@pytest.mark.parametrize("path", _source_files(), ids=_module_name)
def test_only_the_ml_package_may_import_the_heavy_scientific_stack(path: Path) -> None:
    """Everything outside `acp.ml` runs on the base dependencies alone.

    The `degradation` CI job proves the services start and detect conflicts
    with the ml extra genuinely uninstalled, but it proves it for one code
    path at a time. This proves it for every module at once, statically.

    scipy is the one worth naming: it is not a declared dependency at all, it
    only arrives as a transitive of scikit-learn, so importing it anywhere in
    the runtime would work on a developer machine and fail in the service
    image. That is why `acp.services.conformance.probability` computes a
    non-central chi-squared CDF by hand instead of importing one, and this is
    the test that keeps it honest.
    """
    module = _module_name(path)
    if module.startswith("acp.ml"):
        pytest.skip("the ml package is where the heavy stack is allowed")

    offenders = _third_party_roots(path) & {"scipy", "sklearn", "torch"}
    assert not offenders, (
        f"{module} imports {sorted(offenders)}, which the base install does not have. "
        "Only acp.ml may, and only behind the ml extra."
    )


@pytest.mark.parametrize("path", _source_files(), ids=_module_name)
def test_shared_library_does_not_depend_on_its_consumers(path: Path) -> None:
    """`acp.common` is the bottom of the stack and must stay there."""
    module = _module_name(path)
    if not module.startswith("acp.common"):
        pytest.skip("not part of the shared library")
    offenders = _imports_any_of(path, {"acp.services", "acp.sim", "acp.ml", "acp.storage"})
    assert not offenders, f"{module} imports upward: {sorted(offenders)}"


@pytest.mark.parametrize("path", _source_files(), ids=_module_name)
def test_storage_depends_only_on_the_contracts(path: Path) -> None:
    """Storage is a library both services use; it must not know about either."""
    module = _module_name(path)
    if not module.startswith("acp.storage"):
        pytest.skip("not part of the storage layer")
    offenders = _imports_any_of(path, {"acp.services", "acp.sim", "acp.ml"})
    assert not offenders, f"{module} imports upward: {sorted(offenders)}"


@pytest.mark.parametrize("path", _source_files(), ids=_module_name)
def test_services_do_not_import_each_other(path: Path) -> None:
    """A service may only reach a sibling over the wire, never by import."""
    module = _module_name(path)
    if not module.startswith("acp.services."):
        pytest.skip("not a service module")

    own_service = ".".join(module.split(".")[:3])  # acp.services.<name>
    offenders = {
        imported
        for imported in _imported_modules(path)
        if imported.startswith("acp.services.") and not imported.startswith(own_service)
    }
    assert not offenders, (
        f"{module} imports another service: {sorted(offenders)}. "
        "Cross-service communication goes through Kafka or HTTP."
    )


@pytest.mark.parametrize("path", _source_files(), ids=_module_name)
def test_simulator_does_not_depend_on_services(path: Path) -> None:
    """The simulator is a library the feed service drives, not the other way round."""
    module = _module_name(path)
    if not module.startswith("acp.sim"):
        pytest.skip("not a simulator module")
    offenders = {i for i in _imported_modules(path) if i.startswith("acp.services")}
    assert not offenders, f"{module} imports a service: {sorted(offenders)}"
