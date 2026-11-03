# Troubleshooting

Everything below was hit while bringing the chart up; the fixes are built in, the symptoms are
listed so you recognise them.

## `web` / `worker` stay in `Init:0/1` for a long time

They wait for the `migrate` Job (`kubectl -n posthog logs job/<release>-migrate-<hash> -f`). A fresh
database takes 10–25 minutes; a [schema seed](../README.md#fast-first-start-schema-seed) brings
that down to seconds. If the Job itself is not running, check that ClickHouse and PostgreSQL are
ready: it needs both.

## `wait-for-migrations` init container OOMKilled

Since upstream's move to Django 6.1 (2026-10), `manage.py migrate --check` loads a migration graph
that no longer fits in 1 Gi. Chart 0.2.0 gives the init container a 2 Gi limit; raise it further
with `migrations.waitResources` if a later upstream needs more.

## Replay Vision, session summaries or video exports never run

Before 0.2.0 the chart ran one `temporal-django-worker` that polled one task queue, while PostHog
schedules these workflows on per-product queues (`replay-vision-task-queue`,
`session-replay-task-queue`, `video-export-task-queue`, …). 0.2.0 sets
`TEMPORAL_USE_SINGLE_TASK_QUEUE=true` on web, worker and temporal-django-worker and registers
PostHog's custom search attributes (`temporal-search-attributes` hook Job). It also creates PostHog's
Temporal schedules (`temporal-schedules` hook Job): upstream's `bin/migrate` skips that step on hobby
deploys, so before 0.2.0 no schedule existed. Without the Replay Vision reconciler, enabled scanners
never got a sweep schedule, and insight alerts, subscriptions and digests did not run either.

- Check the schedules with `temporal schedule list` (about 130 on a self-hosted instance, among
  them `replay-vision-scanner-reconciler-schedule`). Missing: look at the `temporal-schedules` Job's
  logs, or run `python manage.py schedule_temporal_workflows` once in a pod with
  temporal-django-worker's env. Not inside the running worker, though: a second Django process does
  not fit its memory limit.
- Scans stay "Running" and their child workflow `rasterize-recording` waits on
  `rasterization-task-queue`: the recording rasterizer is missing or crash-looping. See
  [rasterizer.md](rasterizer.md); `No usable sandbox!` in its log means the seccomp profile is
  missing.
- The first sweep after enabling a scanner can start a hundred scans at once.
  `temporal-django-worker` runs at most 10 activities in parallel (`MAX_CONCURRENT_ACTIVITIES`).
  Raise it only together with the worker's memory limit (`services.temporal-django-worker.{env,resources}`).
- Workflows started before the upgrade stay on their old queue and never run. Terminate them with
  the Temporal CLI, for example
  `temporal workflow terminate --query 'TaskQueue="replay-vision-task-queue"' --reason "single queue since chart 0.2.0"`
  (run it where the CLI reaches `<release>-temporal:7233`).
- `Namespace default has no mapping defined for search attribute PostHogTeamId`: the hook Job did
  not run. Check `temporal.searchAttributes.enabled` and the Job's logs.
- Scans fail with `GatewayNotConfiguredError: AI_GATEWAY_URL and AI_GATEWAY_API_KEY must be
  configured`: Replay Vision reaches Gemini only through PostHog's AI gateway now, and
  `REPLAY_VISION_GEMINI_API_KEY` is no longer read. Enable `geminiGateway` with your Gemini key
  ([gemini-gateway.md](gemini-gateway.md)).
- Scans fail with `ExportedAsset ... produced empty video bytes`: temporal-django-worker reads the
  rendered video without object storage. 0.2.0 sets `OBJECT_STORAGE_ENABLED=true` there; check that
  no `services.temporal-django-worker.env` override turns it off.
- Self-hosted organizations are capped by `REPLAY_VISION_MONTHLY_CREDIT_QUOTA`, which defaults to the
  free plan's allocation (2,500 credits per month at the time of writing).

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
kubectl -n posthog logs $(kubectl -n posthog get jobs -l app.kubernetes.io/component=migrate --sort-by=.metadata.creationTimestamp -o name | tail -1) --tail=50
kubectl -n posthog exec posthog-clickhouse-0 -c clickhouse -- clickhouse-client -q "SELECT event, count() FROM posthog.events GROUP BY event"
```

`e2e/diagnose.sh <namespace>` prints container exit reasons, previous logs of crash-looping
containers and events in one go (it is what CI runs on failure).
