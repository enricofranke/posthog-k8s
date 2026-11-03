# Changelog

All notable changes to the chart. The format follows [Keep a Changelog](https://keepachangelog.com/);
versions follow [SemVer](https://semver.org/). `appVersion` is the upstream PostHog commit the
chart data was generated from.

## [0.2.0] - 2026-11-03

### Changed
- Upstream PostHog `4ba4b09d` (2026-11-03), was `18b824b7` (2026-09-11).
- ClickHouse runs PostHog's build, as upstream does: `ghcr.io/posthog/clickhouse-posthog:26.9.5.2-posthog-aca5bdabb92f`
  (was `clickhouse/clickhouse-server:26.6.2.158`). users.xml gains JSON-type settings in the default
  profile, user_defined_function.xml two UDFs (`JSONCleanPostHogEvent`, `JSONDropKeysPool`).
- capture, replay-capture: topics come from the `CAPTURE_OUTPUT_*` variables; the
  `CAPTURE_V1_SINK_*` variables are gone. capture no longer reads `CAPTURE_ANALYTICS_AI_EVENTS_TOPIC`;
  it sends `$ai_*` events to `CAPTURE_OUTPUT_AI_MAIN_TOPIC` (default `events_plugin_ingestion_ai`).
- feature-flags and hypercache-server read the HyperCache S3 tier from the configured object storage
  (credentials by reference).
- temporal-django-worker gets recording-api URL and credentials (recording deletion) and personhog.
- New Kafka topics `clickhouse_email_status` and `realtime_only_events_json`.
- The web start script mirrors upstream's optional `POSTHOG_GRANIAN_SHARED_SOCKET` mode.
- The wait-for-migrations init container of web and the workers gets a 2Gi memory limit (was 1Gi),
  configurable as `migrations.waitResources`: with upstream on Django 6.1, `manage.py migrate --check`
  was OOM-killed at 1Gi.
- The `asyncmigrationscheck` hook gets 2Gi (was 1Gi) for the same reason: `run_async_migrations
  --check` was OOM-killed, and the Argo CD sync waited on the hook.

### Added
- `capture-ai` (AI events on `/i/v0/ai` and `/i/v1/ai/events`, now routed on both listeners) and
  `property-defs-ai` services.
- `recording-rasterizer` (`rasterizer.*`): upstream's Node.js worker that renders recordings to
  video in chrome-headless-shell, for Replay Vision scans and replay video exports. They wait on
  `rasterization-task-queue`, which the single-queue setting does not collapse, and upstream's
  hobby compose has no such worker. Chromium's sandbox needs a Localhost seccomp profile
  (`docs/seccomp/chrome-sandbox.json`, `rasterizer.seccompProfile`); with `networkPolicy.enabled`
  an egress policy keeps the browser off private addresses. See docs/rasterizer.md.
- `gemini-gateway` (`geminiGateway.*`, off by default): a Gemini-only stand-in for PostHog's AI
  gateway. Upstream's Replay Vision builds its Gemini client only through that gateway
  (`AI_GATEWAY_URL`), which PostHog does not publish, so scans failed with `GatewayNotConfiguredError`.
  A Caddy proxy checks an internal token, swaps in the Gemini key, drops PostHog's label headers and
  keeps resumable uploads on itself; web, worker and temporal-django-worker get `AI_GATEWAY_URL` and
  `AI_GATEWAY_API_KEY`. Secrets `GEMINI_API_KEY` and `AI_GATEWAY_API_KEY`. See docs/gemini-gateway.md.
- Generator: `extra_images` in rules.yaml for images of chart-native services, pinned like the
  compose images.
- `temporal-schedules`: a post-install/post-upgrade hook Job that creates PostHog's Temporal
  schedules (see Fixed). `temporal.schedules.enabled`.
- `temporal-search-attributes`: a post-install/post-upgrade hook Job that registers PostHog's custom
  Temporal search attributes with upstream's `manage.py register_temporal_search_attributes`. It adds
  only missing attributes, so existing ones are left alone. `temporal.searchAttributes.{enabled,namespace}`.
- Secrets `INTERNAL_REQUEST_TOKEN` (Django -> feature-flags), `RECORDING_API_JWT_SECRET`
  (Django -> recording-api) and `CLICKHOUSE_SIGNALS_PASSWORD` (new read-only ClickHouse user
  `signals`). Values: `posthog.secrets.internalRequestToken`, `posthog.secrets.recordingApiJwtSecret`,
  `clickhouse.users.signals`.
- `migrations.sessionsV3Reset` (default on): before `bin/migrate`, the migrate Job rebuilds an empty
  `raw_sessions_v3` that still has the old shape. It drops the empty v3 tables and views and forgets
  the v3 migration records, so the migrations recreate them with today's definitions. Forgetting the
  records alone is not enough, because the replayed 0149 keeps the old tables and fails on its
  `uniqMerge` view. A v3 table that holds rows stops the Job with the manual steps. Dry run:
  `SESSIONS_V3_RESET_DRY_RUN=1`.

### Fixed
- Temporal workflows on any task queue other than the default never ran (Replay Vision, session
  replay and video exports, PostHog AI, batch exports): web, worker and temporal-django-worker now
  set `TEMPORAL_USE_SINGLE_TASK_QUEUE=true` (upstream PostHog/posthog#123069), so the one
  temporal-django-worker polls every workflow.
- Starting a workflow failed with `Namespace default has no mapping defined for search attribute
  PostHogTeamId`: Temporal with SQL visibility had none of PostHog's custom search attributes.
- None of PostHog's Temporal schedules existed, so the periodic work never ran: the Replay Vision
  reconciler (enabled scanners got no sweep schedule and never scanned), insight alerts,
  subscriptions, error tracking digests. Upstream's `bin/migrate` skips `schedule_temporal_workflows`
  when `DEPLOYMENT=hobby`, which the hobby env sets. A new `temporal-schedules` hook Job
  (post-install/post-upgrade, after the search attributes) runs it; `temporal.schedules.enabled`.
  Cloud-only schedules (usage reports, CRM sync) stay off because `CLOUD_DEPLOYMENT` is unset.
- Replay Vision failed every scan with `ExportedAsset ... produced empty video bytes`:
  temporal-django-worker had no `OBJECT_STORAGE_ENABLED` (hobby sets it on web and worker only,
  PostHog defaults to false), so it read the rendered videos through the disabled-storage stub.
- temporal-django-worker ran out of memory on the first Replay Vision sweep (the SDK default allows
  100 parallel activities). It now sets `MAX_CONCURRENT_ACTIVITIES=10`, overridable by name.

- Argo CD: every sync after the first failed on the migrate Job (`Job.batch ... is invalid:
  spec.selector: Required value`): `Replace=true` cannot replace an existing Job, because the API
  server defaults the selector only on create. The Job name now carries a hash of its rendered spec
  and the annotation is gone, so a sync re-applies the finished Job unchanged.
- Argo CD ran the migrations again every day: the TTL controller deleted the finished migrate Job
  after 24 hours and self-heal recreated it. The migrate Job has no `ttlSecondsAfterFinished` any
  more; superseded ones carry `argocd.argoproj.io/compare-options: IgnoreExtraneous`.

### Upgrade notes
- Replay Vision no longer reads `REPLAY_VISION_GEMINI_API_KEY` or `GEMINI_API_KEY`: enable
  `geminiGateway` and map your Gemini key with `secrets.keys.GEMINI_API_KEY` (docs/gemini-gateway.md).
  Setting `AI_GATEWAY_URL` also points PostHog's other gateway features at it; they get 501 for
  OpenAI/Anthropic models, as they failed without a gateway before.
- Replay Vision and replay video exports need the new `recording-rasterizer`, and it needs a
  seccomp profile on the nodes: install `docs/seccomp/chrome-sandbox.json` and set
  `rasterizer.seccompProfile` (docs/rasterizer.md). Until then the pod logs
  `No usable sandbox!` and restarts; `rasterizer.enabled: false` turns it off.
- Argo CD: after the upgrade, delete the old `posthog-migrate-*` Job (it predates
  `IgnoreExtraneous` and shows as requiring pruning). If a sync is stuck retrying the old Job's
  `Replace`, terminate the operation first.
- PostgreSQL 15 or newer is required: upstream moved to Django 6.1, and `posthog/settings/service_requirements.py`
  now asks for `>=15.0.0`. The bundled image is 15.12; check `SELECT version()` on an external server
  before upgrading.
- ClickHouse migration `0364_sessions_v3_more_ad_ids` creates the `raw_sessions_v3` materialized views
  again with the new table shape (`uniq` states instead of `uniqExact`), and its `IF NOT EXISTS` keeps an
  existing table. Every install from before 2026-10-28 has that table in the old shape (empty, because
  migration 0178 had dropped its views), and views that cannot write to it would make every insert into
  `sharded_events` fail. The migrate Job handles this on its own (`migrations.sessionsV3Reset`, see
  Added). Its log line starting `sessions-v3 reset:` says what it did. If your v3 table holds rows,
  the Job stops and prints the manual steps instead. Check beforehand with
  `SELECT count() FROM sharded_raw_sessions_v3` and `SHOW CREATE TABLE sharded_raw_sessions_v3`.
- `posthog.existingSecret` / `secrets.keys` (Argo CD): add `INTERNAL_REQUEST_TOKEN` and
  `RECORDING_API_JWT_SECRET` to the app Secret. Both references are optional, so pods start without
  them, but flag test evaluation in the UI fails and recording-api keeps accepting the shared
  `INTERNAL_API_SECRET`. Add `CLICKHOUSE_SIGNALS_PASSWORD` to your ClickHouse Secret and map it in
  `secrets.keys`; unmapped, it lands in the chart's own Secret, which Argo CD regenerates on every render.
- ClickHouse restarts on a newer release (26.6 -> 26.9, PostHog's build). Back it up first; going
  back to 26.6 after 26.9 has written data is not supported.
- Workflows that were already started on the per-product queues stay there and never run; terminate
  them. Per-object Temporal schedules (batch exports, warehouse syncs) created before this version
  keep their old queue until they are saved again.
- If you overrode `CAPTURE_ANALYTICS_AI_EVENTS_TOPIC` on capture, move the override to
  `CAPTURE_OUTPUT_AI_MAIN_TOPIC`, and make sure a consumer reads that topic (ingestion-general in
  combined mode reads `events_plugin_ingestion_ai` only in current posthog-node images).
- The node services use `posthog/posthog-node:latest`, as upstream's compose does. On Docker Hub that
  tag was last published on 2026-03-31; current builds are on `ghcr.io/posthog/posthog-node`. To run
  them at the same code level as Django, override the image, e.g.
  `images.overrides.posthog-node: {repository: ghcr.io/posthog/posthog-node, digest: <digest of :latest>}`.

## [0.1.1] - 2026-09-12

### Added
- `services.<name>.persistence.selector` and `extraOverrides` to bind StatefulSet claims to
  pre-provisioned PersistentVolumes (local volumes by label).
- `services.<name>.env` overrides upstream variables by name instead of duplicating them.
- Schema seed attached to releases; docs: production walkthrough, Argo CD, troubleshooting.

### Fixed
- Release workflow: cosign signature push (docker login).

## [0.1.0] - 2026-09-12

First alpha release.

### Added
- First public version of the chart, generated from PostHog `18b824b7` (2026-09-11).
- All hobby services rendered as Deployments/StatefulSets: web, worker, temporal-django-worker,
  plugins, ingestion-{general,sessionreplay,error-tracking,logs,traces}, recording-api, capture,
  replay-capture, capture-logs, feature-flags, property-defs, hypercache, livestream, personhog,
  cymbal, browserless, ClickHouse, Zookeeper, Redpanda, Redis, Valkey, Temporal.
- Router (Caddy) with separate `app` and `ingest` listeners; the ingest listener only forwards SDK
  paths and strips identity headers.
- Bundled PostgreSQL and SeaweedFS for evaluation; external PostgreSQL and S3 for production.
- Secrets by reference (`existingSecret`, `secrets.keys`), generated once when absent.
- Images pinned by digest at sync time.
- `migrate`, `kafka-init` and `asyncmigrationscheck` as Helm/Argo hooks.
- CI: generator tests, helm lint/unittest, kubeconform, Caddyfile validation, end-to-end install
  and smoke test on kind.
- Schema seed (`seed.hostPath` / `seed.existingClaim`): restore a pre-migrated PostgreSQL dump and
  ClickHouse backup on first start instead of running every migration; CI caches one per upstream
  commit.
- Compose-name aliases (`compatAliases.enabled`): ExternalName Services for `db`, `kafka`,
  `clickhouse`, `temporal`, ... because PostHog's code uses those names as defaults.
