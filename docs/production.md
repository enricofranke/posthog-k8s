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

### Without an access key (workload identity)

With `objectStorage.auth: serviceAccount` the chart passes no access key: the key variables render
empty and every S3 client (Python, Node and Rust) falls back to its SDK's default credential chain.
The identity then comes from the pod's ServiceAccount, whichever way your platform provides it.
It needs an external endpoint; the bundled SeaweedFS only accepts its own keys.

```yaml
objectStorage:
  endpoint: https://s3.eu-central-1.amazonaws.com
  bucket: my-posthog
  region: eu-central-1
  auth: serviceAccount
  bundled: {enabled: false}
  # Only the services that read or write object storage run as this one; the rest keep
  # serviceAccount (or the namespace default), so they never get the identity.
  serviceAccount:
    create: true
    annotations: {}   # whatever your platform's workload identity binds on, if anything
```

Two ways to get the identity to the pods:

- **A platform webhook.** If your cluster injects credentials for annotated ServiceAccounts, put
  the annotation in `objectStorage.serviceAccount.annotations` and you are done.
- **The chart's projected token.** `objectStorage.webIdentity` mounts a token of the pod's
  ServiceAccount (`audience`, `expirationSeconds`) into the object storage services and sets
  `AWS_ROLE_ARN`, `AWS_WEB_IDENTITY_TOKEN_FILE` and, with `stsEndpoint`, `AWS_ENDPOINT_URL_STS`.
  The SDKs exchange it through `AssumeRoleWithWebIdentity`, so it works with any S3 store whose STS
  trusts your cluster's service-account issuer (AWS, MinIO, Ceph RGW, ...), no webhook needed.

```yaml
objectStorage:
  webIdentity:
    enabled: true
    roleArn: <role to assume, in the provider's format>
    audience: sts.amazonaws.com   # what the provider's STS expects
    stsEndpoint: ""               # e.g. https://minio.example.com for MinIO's STS
```

cymbal (error-tracking symbol sets) only recognises web-identity credentials
(`AWS_ROLE_ARN` + `AWS_WEB_IDENTITY_TOKEN_FILE`); other credential sources leave it without object
storage. `services.<name>.serviceAccountName` overrides the ServiceAccount of a single service.

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
| `posthog-app` | `SECRET_KEY`, `ENCRYPTION_SALT_KEYS`, `BROWSERLESS_TOKEN`, `INTERNAL_API_SECRET`, `INTERNAL_REQUEST_TOKEN`, `RECORDING_API_JWT_SECRET` | `ENCRYPTION_SALT_KEYS` must never change once data exists (32 hex chars); the last two are random strings (`openssl rand -hex 32`) |
| `posthog-clickhouse` | `CLICKHOUSE_API_PASSWORD`, `CLICKHOUSE_APP_PASSWORD`, `CLICKHOUSE_BILLING_PASSWORD`, `CLICKHOUSE_DICT_READER_PASSWORD`, `CLICKHOUSE_SIGNALS_PASSWORD` | any strong strings |
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
    CLICKHOUSE_SIGNALS_PASSWORD: {secret: posthog-clickhouse}
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

The app listener accepts `appIngressFrom` and nobody else: the release's own pods reach the ingest
listener, not the app listener. Leaving `appIngressFrom` empty closes it.

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
        - "X-Forwarded-Email: {{external.email}}"
        - "X-Forwarded-Groups: {{internal.kubernetes_groups}}"
```

Give roles `app_labels: {app: posthog}`. `external.email` is the email your SSO connector passes
on; PostHog accounts are matched by it, so only send an address the identity provider has
verified. `X-Forwarded-User` is only the display name of a new account, but set all three: a header
the proxy leaves out is one the client can send. Without `authProxy.enabled`, users log in to PostHog with their own account after
passing Teleport.

### oauth2-proxy example

[oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) as the reverse proxy in front of the
app listener, against any OIDC provider:

```yaml
# values of the oauth2-proxy chart; client ID, client secret and cookie secret come from a Secret
extraArgs:
  provider: oidc
  oidc-issuer-url: https://idp.example.com/realms/main
  redirect-url: https://posthog.example.com/oauth2/callback
  email-domain: "*"
  scope: openid email profile groups
  oidc-groups-claim: groups
  upstream: http://posthog-router.posthog.svc.cluster.local:8080
  reverse-proxy: true
```

Point the Ingress for `posthog.example.com` at oauth2-proxy, not at the router. oauth2-proxy
passes `X-Forwarded-User`, `X-Forwarded-Email` and `X-Forwarded-Groups` to its upstream
(`pass-user-headers`, on by default), which are the chart's default header names, and sets them
itself on every request instead of forwarding what the client sent. Two of its defaults matter
here, leave them as they are: `insecure-oidc-allow-unverified-email=false` (an unverified address
is refused, and accounts are matched by email) and `skip-auth-strip-headers=true` (on routes you
exempt from authentication the client's copies of these headers are removed).

### Turning on the header login

With one of the proxies above in place, the chart side is ([auth-proxy.md](auth-proxy.md) has the
details and the trust model):

```yaml
authProxy:
  enabled: true
  groups:
    platform-admins: {organizations: ["*"], level: admin}
    acme-developers: {organizations: [acme], level: member}     # organization slug or ID
  # passwordLogin: false          # the proxy as the only way in; read "break-glass" first

networkPolicy:
  enabled: true
  appIngressFrom:                  # required: only the proxy may reach the app listener
    - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: oauth2-proxy}}
```

Add `AUTH_PROXY_SECRET` (a random string, `openssl rand -hex 32`) to the `posthog-app` Secret from
section 5; the router and `web` read it from there. Create the organizations first (the login does
not), and keep the account you signed up with: it owns its organization and is your way in if the
proxy is down. Whatever proxy you use, check that it replaces identity headers a client sends
instead of adding its own next to them; PostHog answers 400 to a request that carries one twice.

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
