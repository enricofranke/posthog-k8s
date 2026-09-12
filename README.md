# posthog-k8s

**A community Helm chart for self-hosting [PostHog](https://posthog.com) on Kubernetes.**
Session replay, error tracking, logs, feature flags, surveys and the whole ingestion pipeline,
generated from PostHog's own `docker-compose.hobby.yml` at a pinned commit and tested end-to-end
on every change.

[![CI](https://github.com/enricofranke/posthog-k8s/actions/workflows/ci.yml/badge.svg)](https://github.com/enricofranke/posthog-k8s/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Chart](https://img.shields.io/badge/chart-0.1.0-informational)
![Upstream](https://img.shields.io/badge/PostHog-18b824b7-informational)

> **Status: alpha (0.1.x).** Installs and passes an end-to-end test on every commit; running in
> production by the maintainers with external PostgreSQL and S3. Expect values to change before 1.0.
> Not tested yet: in-place upgrades between chart versions, Gateway API ingress.

> Not affiliated with or endorsed by PostHog Inc. PostHog stopped supporting Kubernetes in 2023
> and archived its Helm chart in May 2026. This project exists because a lot of us still want to
> run PostHog next to our other workloads, with GitOps, our own Postgres and our own S3.

---

## Why this chart

PostHog's only supported self-hosting path is a ~35-container docker-compose file on one VM. The
compose file is excellent and kept up to date. So instead of hand-writing 30 Deployments that drift
from upstream within weeks, this chart is **generated from that compose file**:

```
PostHog/posthog @ <pinned commit>
  docker-compose.base.yml ─┐
  docker-compose.hobby.yml ├─► tools/sync_upstream.py ─► charts/posthog/upstream.yaml
  .env.services            │      (rules.yaml)              charts/posthog/files/upstream/**
  docker/clickhouse/*.xml ─┘                                          │
                                                          generic templates render
                                                          Deployments, StatefulSets, Jobs,
                                                          ConfigMaps, the router, NetworkPolicies
```

What you get on top of a straight translation:

| | |
|---|---|
| **Two front doors** | A Caddy router with an `app` listener (full UI/API, put it behind your identity-aware proxy) and an `ingest` listener that forwards **only** SDK paths and strips identity headers. Your login page never has to be on the internet. |
| **Your datastores** | External PostgreSQL (CloudNativePG `uri` secret works out of the box) and S3-compatible object storage for replays and exports. Bundled single-node PostgreSQL and SeaweedFS for evaluation. |
| **Secrets by reference** | Every secret can live in a Secret you own (`existingSecret`, `secrets.keys`). Generated once and kept when you let the chart do it. Nothing secret in the pod spec. |
| **Pinned** | Every image pinned by digest at sync time. `appVersion` is the upstream commit. |
| **Secure defaults** | Non-root everywhere, all capabilities dropped, no service-account tokens, optional NetworkPolicies, PodDisruptionBudgets, ClickHouse app users with real passwords. |
| **Tested** | Generator unit tests, `helm lint`, `helm unittest`, `kubeconform`, and a full install on kind that signs up, sends an event, an exception, an OTLP log line and evaluates a flag. |

## Quick start

```bash
helm install posthog oci://ghcr.io/enricofranke/charts/posthog \
  --namespace posthog --create-namespace \
  --set posthog.siteUrl=https://posthog.example.com \
  --set posthog.publicUrl=https://ph.example.com \
  --set router.ingress.enabled=true \
  --set router.ingress.className=nginx
```

First start takes a few minutes: ClickHouse and Redpanda come up, the `migrate` job applies the
schema, then `web` becomes ready. Open the site URL and create the first user.

Or from a clone:

```bash
git clone https://github.com/enricofranke/posthog-k8s && cd posthog-k8s
helm install posthog charts/posthog -n posthog --create-namespace -f my-values.yaml
```

Minimum footprint with the defaults: about 18 GB RAM requested across ~34 pods, 160 GB of
volumes (ClickHouse 100, Redpanda 50, PostgreSQL 20, SeaweedFS 50 when bundled). The CI values
file (`charts/posthog/ci/kind-values.yaml`) shows a small configuration that fits a 16 GB machine.

## Production values

```yaml
posthog:
  siteUrl: https://posthog.example.com     # UI, behind your SSO proxy / private ingress
  publicUrl: https://ph.example.com        # what browsers and apps send events to
  existingSecret: posthog-app              # SECRET_KEY, ENCRYPTION_SALT_KEYS, BROWSERLESS_TOKEN, INTERNAL_API_SECRET

postgresql:
  external:
    host: posthog-pg-rw                    # e.g. a CloudNativePG cluster
    existingSecret: posthog-pg-app         # CNPG app secret
    urlKey: uri                            #   -> DATABASE_URL
    passwordKey: password                  #   -> PGPASSWORD
    username: app
    database: app

objectStorage:
  endpoint: https://s3.eu-central-1.amazonaws.com
  bucket: my-posthog
  region: eu-central-1
  forcePathStyle: false
  existingSecret: posthog-s3               # keys accessKeyId / secretAccessKey

router:
  ingress:
    enabled: true
    className: nginx
    hosts:
      - {host: ph.example.com, listener: ingest}   # public
    # the app listener is reached through Teleport / oauth2-proxy / a private ingress instead

services:
  clickhouse:
    persistence: {size: 500Gi, storageClass: fast}
    nodeSelector: {topology.kubernetes.io/zone: a}
  web:
    replicas: 2

networkPolicy:
  enabled: true
  ingestIngressFrom:
    - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: ingress-nginx}}
  appIngressFrom:
    - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: teleport}}

metrics:
  serviceMonitor: {enabled: true}
```

PostgreSQL needs these databases besides the main one (Temporal creates its two when the user
may `CREATEDB`, otherwise create them and set `temporal.skipDbCreate: true`): `temporal`,
`temporal_visibility`. The upstream init scripts in `charts/posthog/files/upstream/docker/postgres-init-scripts/`
list the additional databases the bundled instance creates; the hobby configuration keeps persons
and cohorts in the main database, so none of them is required for this chart's defaults.

### Argo CD

Helm hooks map to Argo sync hooks. Two things to know:

- `lookup` does not run in Argo's `helm template`, so **set every secret** (or use `existingSecret`
  / `secrets.keys`); otherwise generated secrets would change on every sync.
- Use `ServerSideApply=true`; the ClickHouse ConfigMaps are large.

### Behind an identity-aware proxy

Point your proxy (Teleport Application Access, oauth2-proxy, Pomerium, Authelia, Cloudflare
Access) at `<release>-router:8080` and expose `<release>-router:8081` publicly for SDK traffic.
The ingest listener answers 404 for anything that is not an SDK path (`/e`, `/i/v0`, `/i/v1/logs`,
`/s`, `/decide`, `/flags`, `/array`, `/static`, ...). Header-based automatic login
(`authProxy.*`) is on the roadmap, see [docs/auth-proxy.md](docs/auth-proxy.md).

## What is and is not in the box

**In:** every service from `docker-compose.hobby.yml` except the ones replaced by cluster-native
pieces: `db` → PostgreSQL template or external, `seaweedfs`/`objectstorage` → S3 or bundled
SeaweedFS, `proxy` → the router, `elasticsearch` → Temporal on PostgreSQL visibility,
`temporal-ui`/`temporal-admin-tools` → not workloads.

**Not (yet):** high availability for ClickHouse and Redpanda (single instance on a PersistentVolume,
same as the compose deployment; PostHog's replicated ClickHouse layout is a separate project),
external ClickHouse/Kafka, PostHog's paid features (they are Cloud-only; the `ee/` code has its own
license and this chart does not enable it), automatic upstream bumps without a human reading the diff.

PostHog says the hobby deployment is meant for "a couple hundred thousand events a month". People
run it well beyond that; watch ClickHouse memory and Kafka lag and scale the node.

## Fast first start (schema seed)

A fresh PostHog database needs roughly 1,500 PostgreSQL and 300 ClickHouse migrations. On a small
cluster that is 10–20 minutes during which `web` is not ready. The chart can restore a **schema
seed** instead: a `pg_dump` plus a native ClickHouse `BACKUP` of an already-migrated, empty
instance. The bundled datastores pick it up on their first start and the `migrate` job only has
to confirm that everything is applied.

```yaml
seed:
  hostPath: /var/lib/posthog-seed     # directory on the node with posthog.pgdump + clickhouse/seed/
  # or: existingClaim: posthog-seed   # a PVC with the same layout
```

Seeds are produced with `e2e/export-seed.sh` from a running release and are attached to chart
releases as `posthog-schema-seed-<version>.tar.gz`. Later upgrades migrate incrementally anyway; the seed only matters for the first
start. CI uses the same mechanism (cached per upstream commit): a cold end-to-end run installs in
~27 minutes on a 4 vCPU runner, a seeded one in ~10.

## Upgrading

```bash
make sync UPSTREAM=<commit sha on PostHog master>   # regenerates upstream.yaml + files
git diff charts/posthog/upstream.yaml               # read what changed upstream
make test-tools lint unittest kubeconform
```

When the compose file introduces something the rules do not know (a new service, a new variable,
a new host), the generator **fails instead of guessing**. Teach `tools/rules.yaml` about it and
re-run. `CONTRIBUTING.md` walks through it.

Data migrations run in the `migrate` hook job on every upgrade (`./bin/migrate`: Django,
ClickHouse, async migrations). Take a backup first. `helm rollback` rolls the pods back, not the
schema.

## Backups

- **PostgreSQL** (users, dashboards, flags, cohorts, error-tracking issues): back it up like any
  Postgres. This is the data you cannot lose. `ENCRYPTION_SALT_KEYS` from the env secret belongs
  to the same backup.
- **ClickHouse** (events, replay metadata): `BACKUP DATABASE posthog TO S3(...)` from a CronJob or
  clickhouse-backup; restores need the same schema version.
- **Object storage** (replay blobs, exports): bucket versioning/replication on your S3.
- **Redpanda**: transient, no backup.

## Repository layout

```
charts/posthog/           the chart (templates are generic, data is generated)
  upstream.yaml           GENERATED service definitions
  upstream.lock           GENERATED upstream commit + input hashes
  files/upstream/         GENERATED config files (ClickHouse XML, Kafka topics, Temporal config)
  ci/kind-values.yaml     values for the CI end-to-end run
  tests/                  helm-unittest suites
tools/                    generator + rules + tests
e2e/smoke.py              end-to-end test used in CI
```

## Contributing and support

Issues and PRs welcome, especially: verified production setups, external ClickHouse/Kafka, the
auth-proxy overlay, HA ClickHouse. There is no commercial support and no SLA; the maintainers run
this chart themselves and fix what breaks for them first. Security reports: see
[SECURITY.md](SECURITY.md).

## License

Apache-2.0 for everything in this repository. PostHog is MIT-licensed except the `ee/` directory,
which has its own license; the chart uses PostHog's public images unmodified and does not enable
enterprise features. "PostHog" is a trademark of PostHog Inc.
