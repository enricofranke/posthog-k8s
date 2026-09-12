# Troubleshooting

Everything below was hit while bringing the chart up; the fixes are built in, the symptoms are
listed so you recognise them.

## `web` / `worker` stay in `Init:0/1` for a long time

They wait for the `migrate` Job (`kubectl -n posthog logs job/<release>-migrate-<hash> -f`). A fresh
database takes 10–25 minutes; a [schema seed](../README.md#fast-first-start-schema-seed) brings
that down to seconds. If the Job itself is not running, check that ClickHouse and PostgreSQL are
ready: it needs both.

## `web` OOMKilled

Each granian worker is a full Django process (~700 MB). Defaults: `GRANIAN_WORKERS=2` with a 4 Gi
limit. Scale both together via `services.web.env` and `services.web.resources`. The celery worker
forks `WEB_CONCURRENCY` processes (default 2, 3 Gi limit).

## Something connects to `kafka:9092`, `db`, `clickhouse` although env says otherwise

PostHog's code carries the compose hostnames as defaults in places that env does not override.
The chart renders ExternalName Services with those names (`compatAliases.enabled`, default on),
which requires one release per namespace. If you disabled it, that is why.

## Ingest listener returns 404 for a path

By design: only SDK paths are forwarded on :8081. The list comes from upstream's Caddyfile plus
`router.ingest.webPaths`. If an SDK needs a new path after an upstream change, add it there and
open an issue.

## Pod fails to start with `not a directory` or `Invalid value ... must not contain dots`

Both were chart bugs (nested subPath mounts into ConfigMap directories; volume names derived from
upstream paths). Fixed since 0.1.0; upgrade.

## Zookeeper: `/conf/zoo.cfg: Permission denied`

The zookeeper image writes its config at start, which needs root. The chart ships `zoo.cfg` from a
ConfigMap; if you overrode the mounts, keep that file.

## Caddy: `exec /usr/bin/caddy: operation not permitted`

The caddy binary carries `cap_net_bind_service` as a file capability with the effective bit;
dropping all capabilities makes exec fail. The router keeps `NET_BIND_SERVICE`.

## `setpriv: setresuid failed`

Upstream's `bin/docker-server` drops privileges with `setpriv`; in a pod that already runs
non-root that fails. The chart starts web through `files/chart/web/start.sh` (same script minus
that step). A generator test keeps it in sync with upstream.

## Error tracking: exceptions arrive but no issue appears

`cymbal` needs object storage for symbol sets and crashes without the bucket. Check
`cymbal-resolution` logs for `NoSuchBucket`; the bundled SeaweedFS waits for the bucket before
reporting ready, external S3 must have it created.

## Where to look

```bash
kubectl -n posthog get pods
kubectl -n posthog logs deploy/posthog-web -c web --tail=100
kubectl -n posthog logs job/$(kubectl -n posthog get jobs -o name | grep migrate | cut -d/ -f2) --tail=50
kubectl -n posthog exec posthog-clickhouse-0 -c clickhouse -- clickhouse-client -q "SELECT event, count() FROM posthog.events GROUP BY event"
```

`e2e/diagnose.sh <namespace>` prints container exit reasons, previous logs of crash-looping
containers and events in one go (it is what CI runs on failure).
