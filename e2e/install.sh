#!/usr/bin/env bash
# Install the chart and print pod status every minute while helm waits, so a hung or slow
# start-up is visible in CI logs instead of a silent `helm --wait`.
set -euo pipefail
NAMESPACE="${NAMESPACE:-posthog}"
RELEASE="${RELEASE:-posthog}"
CHART="${CHART:-charts/posthog}"
VALUES="${VALUES:-$CHART/ci/kind-values.yaml}"

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

progress() {
  set +e
  while true; do
    sleep 60
    echo "=== $(date -u +%H:%M:%S) pods"
    kubectl -n "$NAMESPACE" get pods --no-headers 2>/dev/null | awk '{printf "  %-55s %-10s %-20s %s\n", $1, $2, $3, $4}'
    kubectl -n "$NAMESPACE" get pods --no-headers 2>/dev/null | awk '$3 !~ /Running|Completed/ {print $1}' | head -5 | while read -r p; do
      echo "  --- $p"; kubectl -n "$NAMESPACE" describe pod "$p" 2>/dev/null | grep -A8 "^Events:" | tail -6 | sed 's/^/      /'
      kubectl -n "$NAMESPACE" logs "$p" --all-containers --tail=8 2>/dev/null | sed 's/^/      | /'
    done
  done
}
progress &
PROGRESS_PID=$!
trap 'kill $PROGRESS_PID 2>/dev/null || true' EXIT

helm upgrade --install "$RELEASE" "$CHART" -n "$NAMESPACE" -f "$VALUES" --wait --timeout "${HELM_TIMEOUT:-15m}"
echo "=== install done"
kubectl -n "$NAMESPACE" get pods
