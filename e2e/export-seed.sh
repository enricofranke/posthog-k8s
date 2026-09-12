#!/usr/bin/env bash
# Export a schema seed from a freshly migrated release: a pg_dump of the PostgreSQL database and
# a native ClickHouse BACKUP of the posthog database. Run right after install, before any data
# is written. Output directory layout matches what the chart's seed hooks expect:
#   <out>/posthog.pgdump
#   <out>/clickhouse/seed/...
set -euo pipefail
OUT="${1:?output dir}"
NAMESPACE="${NAMESPACE:-posthog}"
RELEASE="${RELEASE:-posthog}"
mkdir -p "$OUT/clickhouse"
echo "== pg_dump"
kubectl -n "$NAMESPACE" exec "sts/${RELEASE}-postgresql" -- sh -c 'pg_dump --username="$POSTGRES_USER" --format=custom --no-owner --no-privileges "$POSTGRES_DB"' > "$OUT/posthog.pgdump"
echo "== clickhouse backup"
kubectl -n "$NAMESPACE" exec "${RELEASE}-clickhouse-0" -- sh -c "rm -rf /var/lib/clickhouse/backups/seed && clickhouse-client --query \"BACKUP DATABASE posthog TO File('/var/lib/clickhouse/backups/seed')\""
kubectl -n "$NAMESPACE" exec "${RELEASE}-clickhouse-0" -- tar -C /var/lib/clickhouse/backups -cf - seed | tar -C "$OUT/clickhouse" -xf -
kubectl -n "$NAMESPACE" exec "${RELEASE}-clickhouse-0" -- rm -rf /var/lib/clickhouse/backups/seed
echo "== seed exported"
du -sh "$OUT"/* "$OUT"/clickhouse/seed
