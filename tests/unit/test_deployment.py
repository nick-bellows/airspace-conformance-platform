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


def test_worker_scrape_ports_match_the_configured_metrics_port(compose: Any) -> None:
    configured = compose["x-service-base"]["environment"]["ACP_METRICS_PORT"]
    prometheus = _load(OBSERVABILITY / "prometheus.yml")
    workers = next(s for s in prometheus["scrape_configs"] if s["job_name"] == "acp-workers")
    for static in workers["static_configs"]:
        for target in static["targets"]:
            assert target.endswith(f":{configured}"), target


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
        for container in pod["containers"]:
            security = container["securityContext"]
            assert security["readOnlyRootFilesystem"] is True, name
            assert security["allowPrivilegeEscalation"] is False, name
            assert security["capabilities"]["drop"] == ["ALL"], name
    assert checked == application


def test_every_container_declares_resource_limits() -> None:
    """Without a limit one runaway pod evicts its neighbours."""
    for doc in _workloads():
        for container in doc["spec"]["template"]["spec"]["containers"]:
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


def test_the_locally_built_image_is_the_one_ci_side_loads() -> None:
    """`imagePullPolicy: IfNotPresent` on a tag no registry has only works side-loaded.

    Infrastructure images (Redpanda, Postgres, Redis) are pulled normally and
    are not the concern. The application image exists nowhere but the build, so
    a mismatch between the tag the manifests name and the tag `kind load` pushes
    into the cluster leaves every application pod stuck in ImagePullBackOff --
    with a message that blames the registry rather than the tag.
    """
    workflow = _load(REPO_ROOT / ".github/workflows/quality.yml")
    steps = workflow["jobs"]["k8s"]["steps"]
    loaded = next(step for step in steps if "kind load" in str(step.get("run", "")))
    built = next(step for step in steps if "docker build" in str(step.get("run", "")))

    local_images = {
        container["image"]
        for doc in _workloads()
        for container in doc["spec"]["template"]["spec"]["containers"]
        if container["image"].startswith("acp:")
    }
    assert local_images, "no locally built image in the manifests; this test is stale"
    for image in local_images:
        assert image in loaded["run"], f"{image} is never loaded into the cluster"
        assert image in built["run"], f"{image} is never built"
