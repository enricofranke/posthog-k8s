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

### Added
- `capture-ai` (AI events on `/i/v0/ai` and `/i/v1/ai/events`, now routed on both listeners) and
  `property-defs-ai` services.
- `temporal-search-attributes`: a post-install/post-upgrade hook Job that registers PostHog's custom
  Temporal search attributes with upstream's `manage.py register_temporal_search_attributes`. It adds
  only missing attributes, so existing ones are left alone. `temporal.searchAttributes.{enabled,namespace}`.
- Secrets `INTERNAL_REQUEST_TOKEN` (Django -> feature-flags), `RECORDING_API_JWT_SECRET`
  (Django -> recording-api) and `CLICKHOUSE_SIGNALS_PASSWORD` (new read-only ClickHouse user
  `signals`). Values: `posthog.secrets.internalRequestToken`, `posthog.secrets.recordingApiJwtSecret`,
  `clickhouse.users.signals`.

### Fixed
- Temporal workflows on any task queue other than the default never ran (Replay Vision, session
  replay and video exports, PostHog AI, batch exports): web, worker and temporal-django-worker now
  set `TEMPORAL_USE_SINGLE_TASK_QUEUE=true` (upstream PostHog/posthog#123069), so the one
  temporal-django-worker polls every workflow.
- Starting a workflow failed with `Namespace default has no mapping defined for search attribute
  PostHogTeamId`: Temporal with SQL visibility had none of PostHog's custom search attributes.

### Upgrade notes
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
