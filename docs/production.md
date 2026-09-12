# Production setup

A walkthrough of the setup the maintainers run: external PostgreSQL via CloudNativePG, S3 for
object storage, local PersistentVolumes for the single-instance datastores, the UI behind
Teleport, SDK traffic through a public hostname, secrets from a secret manager via External
Secrets, deployed by Argo CD.

## 1. Namespace and Pod Security

The ClickHouse, Redpanda and Temporal images are not `restricted`-clean; use `baseline` and let
`warn`/`audit` tell you what would be missing:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: posthog
  labels:
    pod-security.kubernetes.io/enforce: baseline
    pod-security.kubernetes.io/warn: restricted
    pod-security.kubernetes.io/audit: restricted
```

## 2. PostgreSQL (CloudNativePG)

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata: {name: posthog-pg, namespace: posthog}
spec:
  instances: 3
  imageName: ghcr.io/cloudnative-pg/postgresql:17.6
  postgresql:
    synchronous: {method: any, number: 1, dataDurability: required}
    parameters:
      max_connections: "400"      # web + workers + node ingestion pools + temporal
      shared_buffers: 1GB
  bootstrap:
    initdb: {database: posthog, owner: app}
  storage: {size: 30Gi, storageClass: your-class}
  backup:
    retentionPolicy: 30d
    barmanObjectStore: {...}      # S3 PITR, see CNPG docs
---
# Temporal needs two databases; declaring them keeps the app user without CREATEDB.
apiVersion: postgresql.cnpg.io/v1
kind: Database
metadata: {name: posthog-pg-temporal, namespace: posthog}
spec: {name: temporal, owner: app, cluster: {name: posthog-pg}}
---
apiVersion: postgresql.cnpg.io/v1
kind: Database
metadata: {name: posthog-pg-temporal-visibility, namespace: posthog}
spec: {name: temporal_visibility, owner: app, cluster: {name: posthog-pg}}
```

CNPG generates the secret `posthog-pg-app` with keys `username`, `password`, `uri`. The chart
consumes it directly:

```yaml
postgresql:
  bundled: false
  external:
    host: posthog-pg-rw
    username: app
    database: posthog
    existingSecret: posthog-pg-app
    urlKey: uri
    passwordKey: password
temporal:
  skipDbCreate: true
```

PostgreSQL 15–17 work; the compose file ships 15, the maintainers run 17.

## 3. Object storage (S3)

One bucket, one IAM user restricted to it. Replays, exports, symbol sets and AI blobs go there.
Budget for roughly 1 GB per 200 recordings.

```yaml
objectStorage:
  endpoint: https://s3.eu-central-1.amazonaws.com
  bucket: my-posthog
  region: eu-central-1
  forcePathStyle: false          # true for MinIO/SeaweedFS/Ceph
  existingSecret: posthog-s3     # keys accessKeyId, secretAccessKey
  bundled: {enabled: false}
```

Minimal IAM policy:

```json
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["s3:ListBucket","s3:GetBucketLocation","s3:ListBucketMultipartUploads"],"Resource":"arn:aws:s3:::my-posthog"},
 {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject","s3:AbortMultipartUpload","s3:ListMultipartUploadParts"],"Resource":"arn:aws:s3:::my-posthog/*"}]}
```

Session replays can use a separate bucket/credentials via `sessionReplay.storage.*`.

## 4. Local volumes for ClickHouse, Redpanda, Zookeeper, Redis

These are single-instance; put them on fast local disks and pin the claims to pre-provisioned
PersistentVolumes with a label selector:

```yaml
services:
  clickhouse:
    persistence:
      size: 200Gi
      storageClass: local-retain
      selector: {matchLabels: {storage.example/purpose: posthog-clickhouse}}
  kafka:
    persistence:
      size: 50Gi
      storageClass: local-retain
      selector: {matchLabels: {storage.example/purpose: posthog-kafka}}
  zookeeper:
    persistence:
      size: 5Gi
      storageClass: local-retain
      selector: {matchLabels: {storage.example/purpose: posthog-zookeeper-data}}
      extraOverrides:
        datalog: {selector: {matchLabels: {storage.example/purpose: posthog-zookeeper-datalog}}}
  redis7:
    persistence:
      size: 2Gi
      storageClass: local-retain
      selector: {matchLabels: {storage.example/purpose: posthog-redis}}
```

Directory ownership on the node: ClickHouse and Redpanda run as uid 101, Zookeeper 1000, Redis
999, the posthog images 10001 (`systemd-tmpfiles` or an init step on the node).

## 5. Secrets

Put these in your secret manager and sync them (External Secrets, Sealed Secrets, SOPS):

| Secret | Keys | Note |
|---|---|---|
| `posthog-app` | `SECRET_KEY`, `ENCRYPTION_SALT_KEYS`, `BROWSERLESS_TOKEN`, `INTERNAL_API_SECRET` | `ENCRYPTION_SALT_KEYS` must never change once data exists (32 hex chars) |
| `posthog-clickhouse` | `CLICKHOUSE_API_PASSWORD`, `CLICKHOUSE_APP_PASSWORD`, `CLICKHOUSE_BILLING_PASSWORD`, `CLICKHOUSE_DICT_READER_PASSWORD` | any strong strings |
| `posthog-s3` | `accessKeyId`, `secretAccessKey` | the IAM user above |
| `posthog-pg-app` | `uri`, `password` | created by CNPG |

```yaml
posthog:
  existingSecret: posthog-app
secrets:
  keys:
    CLICKHOUSE_API_PASSWORD: {secret: posthog-clickhouse}
    CLICKHOUSE_APP_PASSWORD: {secret: posthog-clickhouse}
    CLICKHOUSE_BILLING_PASSWORD: {secret: posthog-clickhouse}
    CLICKHOUSE_DICT_READER_PASSWORD: {secret: posthog-clickhouse}
objectStorage:
  existingSecret: posthog-s3
```

With every secret external, the chart's own Secret only carries derived, deterministic values,
which is what you want under Argo CD.

## 6. Traffic

Two hostnames:

- `posthog.example.com` → router **:8080** (UI + API). Only reachable through your identity-aware
  proxy or a private ingress.
- `ph.example.com` → router **:8081** (SDK ingest). Public. Only SDK paths are forwarded.

Either use the chart's Ingress (`router.ingress.*`) or your own edge (Cloudflare Tunnel, Gateway
API, a proxy). NetworkPolicies lock the listeners down to the peers you name:

```yaml
networkPolicy:
  enabled: true
  ingestIngressFrom:
    - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: ingress-nginx}}
    - namespaceSelector: {matchExpressions: [{key: my.org/product, operator: Exists}]}   # backends send server-side
  appIngressFrom:
    - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: teleport}}
      podSelector: {matchLabels: {app: teleport-agent}}
```

Backends in the same cluster should send to `http://<release>-router.<ns>.svc:8081` directly.

### Teleport example

Teleport Application Access (teleport-kube-agent) fronting the app listener:

```yaml
apps:
  - name: posthog
    uri: http://posthog-router.posthog.svc.cluster.local:8080
    public_addr: posthog.example.com
    labels: {app: posthog}
    rewrite:
      headers:
        - "X-Forwarded-User: {{internal.logins}}"
        - "X-Forwarded-Groups: {{internal.kubernetes_groups}}"
```

Give roles `app_labels: {app: posthog}`. Until the header login ships, users log in to PostHog
with their own account after passing Teleport.

## 7. Multiple organizations

Self-hosted PostHog has no project-level access control (that is a paid, Cloud-only feature).
The isolation boundary is the **organization**: members see every project of their organization
and nothing else. Set `posthog.multiOrg: true`, create one organization per product or team,
invite people as Members (Admin/Owner for the operators).

## 8. Checklist before going live

- [ ] `posthog.siteUrl` is the exact URL users open (cookies and CSRF depend on it), `publicUrl` the ingest one
- [ ] Every secret external; `ENCRYPTION_SALT_KEYS` backed up
- [ ] PostgreSQL PITR and a restore drill
- [ ] ClickHouse backup CronJob (`BACKUP DATABASE posthog TO File('/var/lib/clickhouse/backups/<date>')` + sync to S3)
- [ ] Bucket versioning or replication
- [ ] Alerts on Kafka consumer lag, ClickHouse disk, router 5xx, pod restarts
- [ ] First user created, signup closed (automatic), 2FA for admins
