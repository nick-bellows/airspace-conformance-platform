"""Fitness functions for the deployment artefacts.

Compose files, Kubernetes manifests, scrape configs, and a Grafana dashboard are
all code that nothing type-checks and nothing imports. They drift silently: a
metric gets renamed, the dashboard keeps querying the old name, and the panel
shows "No data" -- which looks exactly like a healthy idle system.

These tests are the seam. They do not check that Kubernetes is configured well;
they check that the four places a port number, a service name, or a metric name
appears still agree with each other.

Deliberately not tested here: whether the cluster works. That is the `k8s` CI
job, which applies these manifests to a real `kind` cluster, because no amount
of YAML parsing proves a pod starts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"
OBSERVABILITY = DEPLOY / "observability"


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_all(path: Path) -> list[Any]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def _declared_metric_names() -> set[str]:
    """Every `acp_*` series name the code creates.

    Read from the source rather than by importing `Metrics`, so this test says
    the same thing whether or not the observability extra is installed.
    """
    source = (REPO_ROOT / "src/acp/common/metrics.py").read_text(encoding="utf-8")
    return set(re.findall(r'"(acp_[a-z_]+)"', source))


@pytest.fixture(scope="module")
def compose() -> Any:
    return _load(DEPLOY / "compose.yml")


@pytest.fixture(scope="module")
def dashboard() -> Any:
    return json.loads((OBSERVABILITY / "dashboard.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Metric names: code -> dashboard -> scrape config
# ---------------------------------------------------------------------------


def test_dashboard_only_queries_metrics_that_exist(dashboard: Any) -> None:
    """A renamed metric leaves a panel reading "No data", which looks like calm."""
    declared = _declared_metric_names()
    assert declared, "no metrics found in metrics.py; the parser is wrong, not the code"

    unknown: list[tuple[str, str]] = []
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            for token in re.findall(r"\bacp_[a-z_]+\b", target["expr"]):
                # Histograms expose _bucket/_count/_sum series derived from the
                # declared name; strip the suffix before comparing.
                base = token.removesuffix("_bucket").removesuffix("_count").removesuffix("_sum")
                if base not in declared:
                    unknown.append((panel["title"], token))

    assert not unknown, f"dashboard queries metrics that no longer exist: {unknown}"


def test_every_panel_has_a_query(dashboard: Any) -> None:
    for panel in dashboard["panels"]:
        if panel["type"] == "row":
            continue
        targets = panel.get("targets", [])
        assert targets, f"panel {panel['title']!r} has no query"
        for target in targets:
            assert target["expr"].strip(), panel["title"]


def test_panels_fit_the_grid(dashboard: Any) -> None:
    """Grafana silently reflows overflowing panels; the committed JSON should not."""
    for panel in dashboard["panels"]:
        grid = panel["gridPos"]
        assert grid["x"] + grid["w"] <= 24, panel["title"]


def _documented_budget_s(stage: str) -> float:
    """The p95 budget `latency-budget.md` states for one stage, in seconds."""
    table = (REPO_ROOT / "docs/latency-budget.md").read_text(encoding="utf-8")
    row = next(line for line in table.splitlines() if line.startswith(f"| {stage}"))
    value, unit = re.search(r"\*\*([\d.]+)\s*(ms|s)\*\*", row).groups()
    return float(value) / (1000.0 if unit == "ms" else 1.0)


def test_the_latency_panel_threshold_is_the_documented_budget(dashboard: Any) -> None:
    """The panel drew its red line three orders of magnitude off the budget.

    `latency-budget.md` sets 1 ms p95 for the filter stage; the panel's
    threshold was 1 s and the histogram's smallest bucket was 5 ms, so it could
    neither measure nor display the number it named. A panel that is green
    because it cannot see the failure is worse than no panel.

    The perf suite already reads this document so the budget cannot drift from
    what is enforced. This does the same for what is *displayed*.
    """
    budget = _documented_budget_s("One report through the filter")
    panel = next(p for p in dashboard["panels"] if p["title"] == "Report filter latency")
    steps = panel["fieldConfig"]["defaults"]["thresholds"]["steps"]
    red = next(step["value"] for step in steps if step["color"] == "red")
    assert red == budget, f"panel threshold {red}s, documented budget {budget}s"


def test_the_filter_histogram_can_resolve_its_own_budget() -> None:
    """A bucket ladder that starts above the budget cannot measure compliance."""
    from acp.common.metrics import FILTER_BUCKETS

    budget = _documented_budget_s("One report through the filter")
    assert min(FILTER_BUCKETS) < budget, "no bucket below the budget; p95 is unmeasurable"
    assert budget in FILTER_BUCKETS, "the budget itself should be a bucket boundary"


def test_the_scan_histogram_can_resolve_its_own_budget() -> None:
    from acp.common.metrics import SCAN_BUCKETS

    budget = _documented_budget_s("One conflict scan of the full picture")
    assert min(SCAN_BUCKETS) < budget
    assert budget in SCAN_BUCKETS


def test_replica_aggregation_matches_what_each_replica_holds(dashboard: Any) -> None:
    """`max()` over partial pictures undercounts; `sum()` is the right answer.

    Each tracker replica sets `acp_live_tracks` from its own estimator, which
    holds only the partitions Kafka assigned it. The manifests ship two
    replicas and `operations.md` suggests three, so the old `max()` displayed
    roughly half or a third of the fleet with no visible error.
    """
    panel = next(p for p in dashboard["panels"] if p["title"] == "Live tracks")
    expr = panel["targets"][0]["expr"]
    assert expr.startswith("sum("), f"live tracks must be summed across replicas, got {expr!r}"


def test_dashboard_datasource_uid_matches_the_provisioned_one(dashboard: Any) -> None:
    """A dashboard pointing at a datasource uid nobody provisions renders empty."""
    datasources = _load(OBSERVABILITY / "grafana-datasources.yml")
    uids = {entry["uid"] for entry in datasources["datasources"]}
    assert "acp-prometheus" in uids

    variable = next(v for v in dashboard["templating"]["list"] if v["name"] == "datasource")
    assert variable["current"]["value"] in uids


# ---------------------------------------------------------------------------
# Ports and hostnames: compose <-> prometheus <-> settings
# ---------------------------------------------------------------------------


def test_prometheus_scrapes_only_real_compose_services(compose: Any) -> None:
    """A typo in a target is invisible: the job just shows as down forever."""
    prometheus = _load(OBSERVABILITY / "prometheus.yml")
    names = set(compose["services"])
    for scrape in prometheus["scrape_configs"]:
        for static in scrape.get("static_configs", []):
            for target in static["targets"]:
                host = target.split(":")[0]
                if host in {"localhost", "127.0.0.1"}:
                    continue
                assert host in names, f"{scrape['job_name']} scrapes unknown service {host!r}"
        for dns in scrape.get("dns_sd_configs", []):
            for name in dns["names"]:
                assert name in names, f"{scrape['job_name']} resolves unknown service {name!r}"


def test_workers_are_discovered_by_dns_so_replicas_are_separate_targets(compose: Any) -> None:
    """A static target is one series however many replicas answer the name.

    Docker round-robins DNS, so Prometheus would scrape whichever replica it
    reached and store it under one identity — and the live-track panel *sums*
    across replicas, so with `--scale track=3` it would show one third of the
    fleet while looking correct. `dns_sd_configs` gives each address its own
    target, which is what makes the sum mean anything.
    """
    prometheus = _load(OBSERVABILITY / "prometheus.yml")
    workers = next(s for s in prometheus["scrape_configs"] if s["job_name"] == "acp-workers")

    assert "static_configs" not in workers, "static targets collapse replicas into one series"
    (dns,) = workers["dns_sd_configs"]
    assert dns["type"] == "A"

    configured = int(compose["x-service-base"]["environment"]["ACP_METRICS_PORT"])
    assert dns["port"] == configured, f"discovery port {dns['port']}, configured {configured}"
    assert set(dns["names"]) == {"feed", "track", "conformance"}


def test_compose_metrics_port_matches_the_settings_default(compose: Any) -> None:
    """Compose states the default explicitly; the two must not drift apart."""
    from acp.common.config import Settings

    assert int(compose["x-service-base"]["environment"]["ACP_METRICS_PORT"]) == (
        Settings().metrics_port
    )


def test_metrics_port_is_not_published_to_the_host(compose: Any) -> None:
    """Prometheus reaches it over the compose network. A host port is exposure."""
    for name, service in compose["services"].items():
        for mapping in service.get("ports", []):
            assert not str(mapping).endswith(":9464"), f"{name} publishes the metrics port"


def test_every_published_port_binds_to_loopback(compose: Any) -> None:
    """Development credentials on a LAN-reachable port is the classic mistake."""
    for name, service in compose["services"].items():
        for mapping in service.get("ports", []):
            assert str(mapping).startswith("127.0.0.1:"), (
                f"{name} publishes {mapping} on all interfaces"
            )


# ---------------------------------------------------------------------------
# Kubernetes manifests
# ---------------------------------------------------------------------------


def _k8s_docs() -> list[Any]:
    docs: list[Any] = []
    for path in sorted((DEPLOY / "k8s").glob("*.yaml")):
        docs.extend(_load_all(path))
    return docs


def _workloads() -> list[Any]:
    return [doc for doc in _k8s_docs() if doc.get("kind") in {"Deployment", "Job"}]


def _all_containers(doc: Any) -> list[Any]:
    """Containers and init containers. Both run, so both need checking."""
    pod = doc["spec"]["template"]["spec"]
    return list(pod.get("initContainers", [])) + list(pod["containers"])


def test_application_pods_run_non_root_with_a_read_only_root_filesystem() -> None:
    """Cheap to set at the start, expensive to retrofit once something writes to /."""
    application = {"migrate", "feed", "track", "conformance", "api"}
    checked = set()
    for doc in _workloads():
        name = doc["metadata"]["name"]
        if name not in application:
            continue
        checked.add(name)
        pod = doc["spec"]["template"]["spec"]
        assert pod["securityContext"]["runAsNonRoot"] is True, name
        for container in _all_containers(doc):
            security = container["securityContext"]
            assert security["readOnlyRootFilesystem"] is True, name
            assert security["allowPrivilegeEscalation"] is False, name
            assert security["capabilities"]["drop"] == ["ALL"], name
    assert checked == application


def test_every_container_declares_resource_limits() -> None:
    """Without a limit one runaway pod evicts its neighbours."""
    for doc in _workloads():
        for container in _all_containers(doc):
            resources = container.get("resources", {})
            assert resources.get("requests"), f"{doc['metadata']['name']}: no requests"
            assert resources.get("limits"), f"{doc['metadata']['name']}: no limits"


def test_scrape_annotations_agree_with_container_ports() -> None:
    """An annotation naming a port nothing listens on produces a permanently down target."""
    for doc in _workloads():
        annotations = doc["spec"]["template"]["metadata"].get("annotations", {})
        if annotations.get("prometheus.io/scrape") != "true":
            continue
        declared = int(annotations["prometheus.io/port"])
        ports = [
            port["containerPort"]
            for container in doc["spec"]["template"]["spec"]["containers"]
            for port in container.get("ports", [])
        ]
        assert declared in ports, f"{doc['metadata']['name']}: {declared} not in {ports}"


def test_worker_scrape_annotations_use_the_configmap_port() -> None:
    config = next(doc for doc in _k8s_docs() if doc.get("kind") == "ConfigMap")
    expected = config["data"]["ACP_METRICS_PORT"]
    workers = {"feed", "track", "conformance"}
    seen = set()
    for doc in _workloads():
        if doc["metadata"]["name"] not in workers:
            continue
        seen.add(doc["metadata"]["name"])
        annotations = doc["spec"]["template"]["metadata"]["annotations"]
        assert annotations["prometheus.io/port"] == expected, doc["metadata"]["name"]
    assert seen == workers


def test_conformance_is_pinned_to_one_replica() -> None:
    """It holds the whole picture and does not shard. Two would duplicate every alert."""
    conformance = next(doc for doc in _workloads() if doc["metadata"]["name"] == "conformance")
    assert conformance["spec"]["replicas"] == 1


def test_every_manifest_is_namespaced() -> None:
    """A manifest without a namespace lands in `default` and is missed by cleanup."""
    for doc in _k8s_docs():
        if doc.get("kind") == "Namespace":
            continue
        assert doc["metadata"].get("namespace") == "acp", doc["metadata"]["name"]


# ---------------------------------------------------------------------------
# The local gate against CI
# ---------------------------------------------------------------------------


def test_the_local_script_lints_the_same_paths_as_ci() -> None:
    """`run_checks.ps1` promises that a local pass means a CI pass.

    It did not: CI linted `migrations` and the script did not, so a lint error
    in an Alembic revision was invisible locally and red in CI. A promise of
    lockstep that nothing enforces is worth less than no promise, because it
    stops people checking.
    """
    script = (REPO_ROOT / "scripts/run_checks.ps1").read_text(encoding="utf-8")
    declared = re.search(r"^\$targets = @\((.*?)\)", script, re.MULTILINE)
    assert declared, "could not find $targets in run_checks.ps1"
    local = {path.strip().strip('"') for path in declared.group(1).split(",")}

    workflow = (REPO_ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")
    ci = set(re.search(r"ruff check ([^\n]+)", workflow).group(1).split())

    assert local == ci, f"local lints {sorted(local)}, CI lints {sorted(ci)}"


def test_every_action_is_pinned_to_a_commit_sha() -> None:
    """A moveable tag is a supply-chain risk, and one of them did not even exist.

    `aquasecurity/trivy-action@0.28.0` was referenced for the whole of M5. That
    ref has never existed -- the tag is `v0.28.0` -- so the security job could
    not have started, let alone scanned anything. Tags also move, which is how
    the 2026 trivy-action compromise reached users who thought they had pinned.
    """
    workflow = (REPO_ROOT / ".github/workflows/quality.yml").read_text(encoding="utf-8")
    refs = re.findall(r"uses:\s*(\S+)", workflow)
    assert refs, "no actions found; this test is stale"
    unpinned = [ref for ref in refs if not re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref)]
    assert not unpinned, f"actions not pinned to a full commit SHA: {unpinned}"


def test_publish_depends_on_every_blocking_job() -> None:
    """GitHub blocks a job only on failures among its declared dependencies.

    An unrelated red job does not stop a publish. `integration` and `compose`
    were missing, so a broken idempotent upsert -- a guarantee that lives only
    in the integration suite -- could have shipped an image.
    """
    workflow = _load(REPO_ROOT / ".github/workflows/quality.yml")
    jobs = workflow["jobs"]
    needs = set(jobs["publish"]["needs"])

    # Everything except publish itself and the jobs that cannot block a release.
    informational = {job for job, spec in jobs.items() if spec.get("continue-on-error")}
    expected = set(jobs) - {"publish"} - informational

    assert needs == expected, f"missing from publish needs: {sorted(expected - needs)}"
    assert informational, "no continue-on-error job found; this test is stale"


def test_the_manifests_name_the_image_ci_builds_and_side_loads() -> None:
    """`imagePullPolicy: IfNotPresent` on a tag no registry has only works side-loaded.

    Infrastructure images (Redpanda, Postgres, Redis) are pulled normally and
    are not the concern. The application image exists nowhere but the build, so
    a mismatch between the tag the manifests name and the tag `kind load` pushes
    into the cluster leaves every application pod stuck in ImagePullBackOff --
    with a message that blames the registry rather than the tag.
    """
    workflow = _load(REPO_ROOT / ".github/workflows/quality.yml")
    built = next(
        step
        for step in workflow["jobs"]["image"]["steps"]
        if "docker build" in str(step.get("run", ""))
    )
    loaded = next(
        step for step in workflow["jobs"]["k8s"]["steps"] if "kind load" in str(step.get("run", ""))
    )

    local_images = {
        container["image"]
        for doc in _workloads()
        for container in _all_containers(doc)
        if container["image"].startswith("acp:")
    }
    assert local_images, "no locally built image in the manifests; this test is stale"
    for image in local_images:
        assert image in built["run"], f"{image} is never built"
        assert image in loaded["run"], f"{image} is never loaded into the cluster"


def test_the_published_image_is_the_one_that_was_tested() -> None:
    """One build, one artefact, every consumer checking it is the same one.

    The image used to be built four times on four runners -- scanned in
    `security`, exercised in `compose`, deployed in `k8s`, and then rebuilt in
    `publish`. With a mutable base tag and unpinned dependency ranges, those are
    not guaranteed to be the same image, so every gate could be green while the
    pushed artefact was one nobody had tested and the SBOM described something
    else. An external review traced it.
    """
    workflow = _load(REPO_ROOT / ".github/workflows/quality.yml")
    jobs = workflow["jobs"]

    builders = {
        name
        for name, job in jobs.items()
        for step in job.get("steps", [])
        if "docker build" in str(step.get("run", ""))
    }
    assert builders == {"image"}, f"the image must be built once, but {sorted(builders)} build it"

    for consumer in ("security", "compose", "e2e", "k8s", "publish"):
        steps = jobs[consumer]["steps"]
        assert "image" in jobs[consumer]["needs"], f"{consumer} does not depend on the build"
        assert any("docker load" in str(s.get("run", "")) for s in steps), (
            f"{consumer} does not load the built artefact"
        )
        assert any("needs.image.outputs.image-id" in str(s.get("run", "")) for s in steps), (
            f"{consumer} loads an image without checking it is the one that was built"
        )


# ---------------------------------------------------------------------------
# The README demo
# ---------------------------------------------------------------------------


def test_the_demo_animation_exists_and_is_small_enough_to_load() -> None:
    """It is the first thing a reader sees, so it must load before they leave.

    GitHub serves README images without lazy loading, and a multi-megabyte GIF
    above the fold is worse than no GIF.
    """
    demo = REPO_ROOT / "docs/assets/demo.gif"
    assert demo.is_file(), "run: python scripts/make_demo.py"
    size_mb = demo.stat().st_size / 1024 / 1024
    assert size_mb < 4.0, f"demo.gif is {size_mb:.1f} MB; raise --every in make_demo.py"


def test_the_demo_renders_from_the_committed_scenario() -> None:
    """A hand-made picture could drift from the system; a generated one cannot.

    This checks the generator reaches for the real components rather than
    reimplementing them, which is the property that makes the demo evidence.
    """
    source = (REPO_ROOT / "scripts/make_demo.py").read_text(encoding="utf-8")
    for component in ("Simulation", "TrackEstimator", "SeparationMonitor", "load_scenario"):
        assert component in source, f"the demo no longer uses {component}"
