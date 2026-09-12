<h1 align="center">posthog-k8s</h1>

<p align="center">
  <b>Self-host <a href="https://posthog.com">PostHog</a> on Kubernetes with a Helm chart that is generated from PostHog's own compose file and proven end-to-end on every commit.</b><br>
  Product analytics · Web analytics · Session replay · Error tracking · Logs · Feature flags · Surveys
</p>

<p align="center">
  <a href="https://github.com/enricofranke/posthog-k8s/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/enricofranke/posthog-k8s/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/enricofranke/posthog-k8s/releases"><img alt="Release" src="https://img.shields.io/github/v/release/enricofranke/posthog-k8s?label=chart"></a>
  <img alt="Upstream" src="https://img.shields.io/badge/PostHog-18b824b7-1d4aff">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
</p>

```bash
helm install posthog oci://ghcr.io/enricofranke/charts/posthog \
  --namespace posthog --create-namespace \
  --set posthog.siteUrl=https://posthog.example.com
```

> **Status: alpha (0.1.x).** Installs from scratch and passes an end-to-end test (signup, event,
> exception → issue, OTLP log, feature flag) on every commit. Runs in production for the
> maintainers with CloudNativePG and S3. Values may still change before 1.0; in-place chart
> upgrades are not yet covered by CI.
>
> Not affiliated with or endorsed by PostHog Inc. PostHog [stopped supporting Kubernetes in 2023](https://posthog.com/blog/sunsetting-helm-support-posthog)
> and archived its chart in 2026. This project exists for the people who still want PostHog next to
> their other workloads, with GitOps, their own Postgres and their own S3.

---

## Contents

- [Why this chart](#why-this-chart)
- [How it works](#how-it-works)
- [Quick start (evaluation)](#quick-start-evaluation)
- [Production setup](#production-setup)
- [Behind an identity-aware proxy](#behind-an-identity-aware-proxy)
- [Fast first start (schema seed)](#fast-first-start-schema-seed)
- [Sizing](#sizing)
- [Operations](#operations)
- [What is and is not included](#what-is-and-is-not-included)
- [Repository layout](#repository-layout)
- [Contributing, support, license](#contributing-support-license)

## Why this chart

PostHog's only supported self-hosting path is a ~35-container docker-compose file on a single
VM. That file is excellent and kept current by PostHog. Hand-written Kubernetes manifests drift
from it within weeks. So this chart does not hand-write anything: a generator reads the compose
files at a pinned upstream commit and turns them into chart data; generic templates render the
Kubernetes objects. Upgrading PostHog is "bump the commit, read the diff, run CI".

What you get on top of a straight translation:

| | |
|---|---|
| **Two front doors** | A Caddy router with an `app` listener (full UI and API, put it behind your SSO proxy) and an `ingest` listener that forwards **only** SDK paths and strips identity headers. Your login page never has to be on the internet. |
| **Your datastores** | External PostgreSQL (a CloudNativePG `uri` secret works out of the box) and S3-compatible object storage for replays and exports. Bundled single-node PostgreSQL and SeaweedFS for evaluation. |
| **Secrets by reference** | Every secret can live in a Secret you own. When you let the chart generate them, they are created once and kept. Nothing secret in the pod spec. |
| **Pinned and signed** | Every image pinned by digest at sync time; chart releases pushed to GHCR and signed with cosign (keyless). `appVersion` is the upstream commit. |
| **Secure defaults** | Non-root everywhere, all capabilities dropped, no service-account tokens, optional NetworkPolicies, PodDisruptionBudgets, ClickHouse application users with real passwords. |
| **Fast first start** | A schema seed skips the 20-minute migration marathon of a fresh database. |
| **Tested for real** | Generator unit tests, `helm lint`, `helm unittest`, `kubeconform`, Caddy config validation, and a full install on kind that signs up, sends an event, an exception, an OTLP log line and evaluates a flag. |

## How it works

```
PostHog/posthog @ <pinned commit>
  docker-compose.base.yml  ─┐
  docker-compose.hobby.yml ─┼─►  tools/sync_upstream.py  ─►  charts/posthog/upstream.yaml   (services, images, env, routes)
  .env.services            ─┤        + tools/rules.yaml       charts/posthog/files/upstream/ (ClickHouse XML, Kafka topics, ...)
  docker/clickhouse/*.xml  ─┘                                 charts/posthog/upstream.lock   (commit + sha256 of every input)
                                                                          │
                                                       generic templates + your values.yaml
                                                                          ▼
                                   Deployments · StatefulSets · Jobs · Services · ConfigMaps · Secret
                                   Router (Caddy) · NetworkPolicies · PDBs · ServiceMonitors
```

Everything the generator does not understand is a hard error, not a guess: a new service, a new
variable or a new hostname in the compose file fails the sync until `tools/rules.yaml` says what
to do with it.

Inside the cluster it looks like this:

```
 Browsers / apps                Your SSO proxy / private ingress          Your other workloads
   https://ph.example.com         https://posthog.example.com               <release>-router:8081
        │ (public)                     │ (X-Forwarded-User, ...)                  │
        ▼                              ▼                                          │
  router :8081 "ingest" ◄────── router :8080 "app" ◄──────────────────────────────┘
  /e /i/v0 /i/v1/logs /s /decide /flags /array /static      everything: UI + API
        └──────────────────┬───────────────────┘
                           ▼
  web · worker · temporal-django-worker · plugins · ingestion-{general,sessionreplay,error-tracking,logs,traces}
  recording-api · capture · replay-capture · capture-logs · feature-flags · property-defs · hypercache
  livestream · personhog · cymbal · browserless
  ClickHouse · Zookeeper · Redpanda · Redis · Valkey · Temporal · PostgreSQL* · SeaweedFS*      (* or external)
```

## Quick start (evaluation)

Needs a cluster with a default StorageClass and about 18 GB of allocatable memory. Works on kind,
k3s, k0s, Docker Desktop with enough RAM.

```bash
helm install posthog oci://ghcr.io/enricofranke/charts/posthog \
  --namespace posthog --create-namespace \
  --set posthog.siteUrl=http://localhost:8080 \
  --set posthog.secureCookies=false

kubectl -n posthog get pods -w        # first start: 15–25 minutes (see "Fast first start")
kubectl -n posthog port-forward svc/posthog-router 8080:8080
open http://localhost:8080            # create the first user
```

Small machine? Use the CI values as a starting point, they fit a 16 GB box:

```bash
helm install posthog oci://ghcr.io/enricofranke/charts/posthog -n posthog --create-namespace \
  -f https://raw.githubusercontent.com/enricofranke/posthog-k8s/main/charts/posthog/ci/kind-values.yaml
```

Send something to it:

```bash
TOKEN=phc_...   # Settings → Project → Project API key
curl -X POST http://localhost:8081/e/ -H 'Content-Type: application/json' \
  -d "{\"api_key\":\"$TOKEN\",\"event\":\"hello\",\"distinct_id\":\"me\"}"
```

## Production setup

The full walkthrough with CloudNativePG, S3, local volumes, ingress and NetworkPolicies is in
[docs/production.md](docs/production.md). The short version:

```yaml
posthog:
  siteUrl: https://posthog.example.com     # the UI, behind your SSO proxy or private ingress
  publicUrl: https://ph.example.com        # what browsers and mobile apps send events to
  existingSecret: posthog-app              # SECRET_KEY, ENCRYPTION_SALT_KEYS, BROWSERLESS_TOKEN, INTERNAL_API_SECRET
  multiOrg: true                           # one organization per product/team

postgresql:
  bundled: false
  external:
    host: posthog-pg-rw                    # CloudNativePG service
    username: app
    database: posthog
    existingSecret: posthog-pg-app         # the CNPG app secret
    urlKey: uri                            #   -> DATABASE_URL
    passwordKey: password                  #   -> PGPASSWORD
temporal:
  skipDbCreate: true                       # create `temporal` and `temporal_visibility` yourself (CNPG Database CRs)

objectStorage:
  endpoint: https://s3.eu-central-1.amazonaws.com
  bucket: my-posthog
  region: eu-central-1
  forcePathStyle: false
  existingSecret: posthog-s3               # keys accessKeyId / secretAccessKey
  bundled: {enabled: false}

secrets:
  keys:                                    # route any key to a Secret you manage
    CLICKHOUSE_API_PASSWORD: {secret: posthog-clickhouse}
    CLICKHOUSE_APP_PASSWORD: {secret: posthog-clickhouse}
    CLICKHOUSE_BILLING_PASSWORD: {secret: posthog-clickhouse}
    CLICKHOUSE_DICT_READER_PASSWORD: {secret: posthog-clickhouse}

services:
  clickhouse:
    persistence: {size: 500Gi, storageClass: fast-ssd}
  web:
    replicas: 2

router:
  ingress:
    enabled: true
    className: nginx
    hosts:
      - {host: ph.example.com, listener: ingest}   # public
      # the app listener is reached through Teleport / oauth2-proxy / a private ingress instead

networkPolicy:
  enabled: true
  ingestIngressFrom:
    - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: ingress-nginx}}
  appIngressFrom:
    - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: teleport}}

metrics:
  serviceMonitor: {enabled: true}
```

**Argo CD:** Helm hooks map to sync hooks; set every secret explicitly or via `existingSecret`
(Argo renders without `lookup`, generated secrets would change on each sync); use
`ServerSideApply=true`. Example Application in [docs/argocd.md](docs/argocd.md).

## Behind an identity-aware proxy

Point your proxy (Teleport Application Access, oauth2-proxy, Pomerium, Authelia, Cloudflare
Access) at `<release>-router:8080` and expose `<release>-router:8081` publicly for SDK traffic.
The ingest listener answers 404 for anything that is not an SDK path. Header-based automatic
login and group → organization mapping is the next milestone; see [docs/auth-proxy.md](docs/auth-proxy.md).
A complete Teleport example lives in [docs/production.md](docs/production.md#teleport-example).

## Fast first start (schema seed)

A fresh PostHog database needs ~1,500 PostgreSQL and ~300 ClickHouse migrations. On a small
cluster that is 10–25 minutes during which `web` is not ready. A **schema seed** (a `pg_dump` plus
a native ClickHouse `BACKUP` of an already-migrated, empty instance) is restored by the bundled
datastores on their first start, so the `migrate` job only has to confirm.

```yaml
seed:
  hostPath: /var/lib/posthog-seed      # posthog.pgdump + clickhouse/seed/ on the node
  # or: existingClaim: posthog-seed    # a PVC with the same layout
```

Every release ships `posthog-schema-seed-<version>.tar.gz`; `e2e/export-seed.sh` produces one from
a running release. CI uses the same mechanism: a cold end-to-end run installs in ~27 minutes on a
4 vCPU runner, a seeded one in ~10.

## Sizing

Defaults request about 18 GB of memory across ~34 pods and 160 GB of volumes (ClickHouse 100,
Redpanda 50, PostgreSQL 20, SeaweedFS 50 when bundled). Measured on a 3-project instance with
2 M events and 6,500 recordings per month: ~20 GB RSS in total, ClickHouse and web being the
largest. Concurrency knobs: `GRANIAN_WORKERS` (web, default 2) and `WEB_CONCURRENCY` (celery,
default 2) via `services.<name>.env`.

## Operations

| | |
|---|---|
| **Upgrade PostHog** | `make sync UPSTREAM=<commit>` regenerates `upstream.yaml`; read the diff; CI installs it. Data migrations run in the `migrate` Job on every install and upgrade. Back up first; `helm rollback` rolls pods back, not the schema. |
| **Backups** | PostgreSQL holds everything you cannot lose (users, dashboards, flags, issues) plus `ENCRYPTION_SALT_KEYS` from the env secret. ClickHouse: `BACKUP DATABASE posthog TO File('/var/lib/clickhouse/backups/<name>')` (the chart allows that path). Object storage: bucket versioning. |
| **Monitoring** | `metrics.serviceMonitor.enabled` renders ServiceMonitors for services that expose Prometheus metrics. |
| **Troubleshooting** | [docs/troubleshooting.md](docs/troubleshooting.md): why a pod waits, where the migrate job logs, what the compat aliases are for. |

## What is and is not included

**Included:** every service from `docker-compose.hobby.yml` except the ones replaced by
cluster-native pieces: `db` → bundled PostgreSQL template or external; `seaweedfs`/`objectstorage`
→ S3 or bundled SeaweedFS; `proxy` → the router; `elasticsearch` → Temporal on PostgreSQL
visibility; `temporal-ui`/`temporal-admin-tools` → not workloads.

**Not (yet):** high availability for ClickHouse and Redpanda (single instance on a
PersistentVolume, same as the compose deployment; PostHog's replicated ClickHouse layout is a
separate project); external ClickHouse/Kafka; PostHog's paid features (Cloud-only; the `ee/`
code has its own license and this chart does not enable it); automatic upstream bumps without a
human reading the diff.

## Repository layout

```
charts/posthog/           the chart (templates are generic, data is generated)
  upstream.yaml           GENERATED service definitions
  upstream.lock           GENERATED upstream commit + input hashes
  files/upstream/         GENERATED config files (ClickHouse XML, Kafka topics, Temporal config)
  files/chart/            chart-owned files (web start script, zoo.cfg, seed hooks)
  ci/kind-values.yaml     values for the CI end-to-end run
  tests/                  helm-unittest suites
tools/                    generator + rules + tests
e2e/                      install/smoke/seed scripts used by CI
docs/                     production, Argo CD, troubleshooting, auth proxy roadmap
```

## Contributing, support, license

Issues and PRs welcome, in particular: verified production setups, external ClickHouse/Kafka,
the auth-proxy overlay, HA ClickHouse. See [CONTRIBUTING.md](CONTRIBUTING.md). There is no
commercial support and no SLA; the maintainers run this chart themselves and fix what breaks for
them first. Security reports: [SECURITY.md](SECURITY.md).

Apache-2.0 for everything in this repository. PostHog is MIT-licensed except the `ee/`
directory, which has its own license; the chart uses PostHog's public images unmodified and
does not enable enterprise features. "PostHog" is a trademark of PostHog Inc.
