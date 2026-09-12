# Changelog

All notable changes to the chart. The format follows [Keep a Changelog](https://keepachangelog.com/);
versions follow [SemVer](https://semver.org/). `appVersion` is the upstream PostHog commit the
chart data was generated from.

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
