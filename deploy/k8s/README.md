# Kubernetes manifests

Plain YAML, no Helm. Four services, their infrastructure, and the configuration
that binds them.

## Why plain manifests

Helm's value is templating across environments. There is one environment here,
so a chart would add a layer of indirection whose only purpose is to be
parameterised by nothing. Plain manifests are also readable by someone who does
not know Helm, which matters more for a repository meant to be read than for one
meant to be operated.

Kustomize would be the next step if a second environment ever existed — the
manifests are already shaped for it, with configuration in a single ConfigMap
rather than scattered through each Deployment.

## Applying

```bash
kind create cluster --name acp
docker build --tag acp:dev .
kind load docker-image acp:dev --name acp     # no registry involved

kubectl apply -f deploy/k8s/                  # numeric prefixes order the apply
kubectl -n acp rollout status deploy/api --timeout=240s
kubectl -n acp port-forward svc/api 8000:8000
```

### Redeploying is not the same command again

Running the block above a second time with a rebuilt `acp:dev` **does not deploy
the new code**, and an external review caught the earlier version of this file
claiming it did. Every workload names the same mutable tag, so the pod template
is byte-identical, so `kubectl apply` changes nothing and no new ReplicaSet is
created. `rollout status` then reports success immediately — for the *old*
pods. Worse, the migration Job does rerun, so a new schema is applied underneath
processes still running the old code.

```bash
docker build --tag acp:dev .
kind load docker-image acp:dev --name acp

kubectl -n acp delete job migrate --ignore-not-found   # a completed Job is not re-run
kubectl apply -f deploy/k8s/
kubectl -n acp rollout restart deployment/track deployment/api \
                               deployment/feed deployment/conformance
kubectl -n acp rollout status deployment/api --timeout=240s
```

`rollout restart` is what forces new pods — and therefore what makes the
`wait-for-schema` init containers run again. Verify with pod UIDs, not with
`rollout status`:

```bash
kubectl -n acp get pods -l app=track -o jsonpath='{.items[*].metadata.uid}'
```

**A real deployment would not need any of this**, because it would reference an
immutable tag or digest per build — which is exactly what CI publishes
(`sha-<commit>`). The mutable `acp:dev` exists so a clean checkout works with no
registry at all, and this is what that convenience costs.

**`kind load` is not optional.** The manifests name `acp:dev` with
`imagePullPolicy: IfNotPresent`, and that tag exists in no registry. Skipping the
load leaves every application pod in `ImagePullBackOff` with an error that blames
Docker Hub rather than the missing side-load.

The numeric filename prefixes exist because `kubectl apply -f` on a directory
processes files in lexical order, and the namespace has to exist before anything
claims to be in it.

CI is **configured** to apply these to a `kind` cluster on every push and to
assert the stack serves traffic and a worker's metrics endpoint answers. That job
has never run — this repository has no remote yet — so treat it as unproven.
`tests/unit/test_deployment.py` does run, and checks the things that drift
statically: scrape annotations against container ports, the ConfigMap metrics
port against the annotations, the security context on every container including
init containers, and that the image the manifests name is the one CI builds and
side-loads.

## Schema ordering, and why it needed two mechanisms

`kubectl apply` does **not** re-run a completed `Job` — the object already
exists, so applying it is a no-op, and editing its pod template fails outright
because the template is immutable. An annotation on the Job used to claim the
opposite. A second deployment carrying a new Alembic revision therefore started
application pods against the old schema, and an external review found it.

Compose expresses the ordering directly with
`condition: service_completed_successfully`. Kubernetes has no equivalent: a
Deployment cannot depend on a Job, and everything in this directory is applied
at once. So the fix is two things, and both are needed:

1. **`ttlSecondsAfterFinished: 600`** on the Job, so the object garbage-collects
   itself and a later apply creates a genuinely new one. Plus the explicit
   `kubectl delete job migrate` above, for a redeploy inside that window.
2. **An `acp-wait-for-schema` init container** on every Deployment that touches
   Postgres. It compares `alembic_version` against the head revision the
   committed migration scripts declare, and blocks until they match.

The init container **waits; it does not migrate.** Letting every replica run
`alembic upgrade head` on startup would be simpler and would put concurrent
migrations on one database — a race whose failure mode is far worse than a pod
waiting thirty seconds. One Job migrates; everything else waits for it.

It also fails rather than hanging: a pod stuck in `Init` forever is harder to
diagnose than one that reports a timeout and the revision it was waiting for.

## What is deliberately not here

**No Ingress.** It would be untested in `kind` and is entirely
environment-specific; `port-forward` is honest about what has actually been
verified.

**No HorizontalPodAutoscaler.** It needs metrics-server, and an autoscaler that
has never been observed to scale anything is decoration. The tracker *can* scale
— partitions are assigned across a consumer group, and the manifests set the
replica count — so the scaling story is real even without the HPA object.

**No PersistentVolumeClaims.** Postgres and Redis run with ephemeral storage
because this is a demonstration stack: losing track history on a pod restart is
acceptable here and would obviously not be in production. Stated rather than
hidden behind a StatefulSet that implies durability it does not have.

**No secrets management.** The database password is in a Secret manifest with a
value committed to the repository. That is correct *only* because it is a
development credential for a system holding synthetic data; a real deployment
would source it from a secret store. See `docs/limitations.md`.

**No NetworkPolicy and no TLS.** Every pod can reach every other pod, and
nothing is encrypted in transit. Correct to fix before this held anything real;
listed here so the omission is a decision rather than an oversight.

**No ServiceMonitor.** Prometheus discovery is by pod annotation
(`prometheus.io/scrape`), which works with a plain Prometheus install. A
`ServiceMonitor` needs the Prometheus Operator's CRDs, which `kind` does not have
and this stack does not install.

## Shape

| File | Contents |
| --- | --- |
| `00-namespace.yaml` | Everything lives in `acp` |
| `10-config.yaml` | One ConfigMap for all services, plus the dev-only Secret |
| `20-infrastructure.yaml` | Redpanda, Postgres, Redis with readiness probes |
| `30-services.yaml` | The migration Job, four Deployments, and the API Service |

| Workload | Replicas | Why that number |
| --- | --- | --- |
| `migrate` | Job | Alembic to head. A completed Job is **not** re-run by `kubectl apply` -- see the redeploy section above. Everything touching Postgres waits for the schema via an init container rather than trusting apply ordering |
| `feed` | 1 | Owns the simulation clock. Two would produce two independent airspaces |
| `track` | 2 | The one service that genuinely scales out — Kafka assigns partitions across the group and each aircraft keeps its ordering. Useful up to the 6 topic partitions |
| `conformance` | 1 | Pinned. Every replica would hold the whole picture and publish duplicate alerts. It does not shard |
| `api` | 2 | Stateless: no state, no Kafka. Scales with viewers, independently of everything else |

## Security context

Every application pod runs as non-root UID 1001 with a read-only root
filesystem, all capabilities dropped, `allowPrivilegeEscalation: false`, and
`seccompProfile: RuntimeDefault`. These are cheap to set at the start and
expensive to retrofit once something has started writing to `/tmp` — which is
also why the `k8s` CI job exists: applying the manifests to a real cluster is the
only thing that proves a pod actually starts under the context it declares.

Liveness and readiness are split the same way they are in compose: liveness
checks nothing external, so a database blip cannot trigger a restart loop, while
readiness does check and takes the pod out of the Service without killing it.

## Metrics

The workers have no Service in front of them — nothing calls them, they consume
Kafka — so Prometheus finds them by pod annotation. `ACP_METRICS_PORT` in the
ConfigMap, the `prometheus.io/port` annotation, and the `containerPort` must all
agree; a unit test fails if they stop agreeing, because the failure mode
otherwise is a target that is permanently down with no error anywhere.
