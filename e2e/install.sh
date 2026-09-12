#!/usr/bin/env bash
# Install the chart and print pod status every minute while helm waits, so a hung or slow
# start-up is visible in CI logs instead of a silent `helm --wait`.
set -euo pipefail
NAMESPACE="${NAMESPACE:-posthog}"
RELEASE="${RELEASE:-posthog}"
CHART="${CHART:-charts/posthog}"
VALUES="${VALUES:-$CHART/ci/kind-values.yaml}"

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

# Fail fast: a pod that sits in CrashLoopBackOff with many restarts for several consecutive
# checks is not going to recover; stop waiting for the full helm timeout.
FAILFAST_RESTARTS="${FAILFAST_RESTARTS:-8}"
FAILFAST_STRIKES="${FAILFAST_STRIKES:-5}"
declare -A strikes
progress() {
  set +e
  while true; do
    sleep 60
    echo "=== $(date -u +%H:%M:%S) pods"
    while read -r name ready status restarts _; do
      if [ "$status" = "CrashLoopBackOff" ] && [ "${restarts:-0}" -ge "$FAILFAST_RESTARTS" ]; then
        strikes[$name]=$(( ${strikes[$name]:-0} + 1 ))
        if [ "${strikes[$name]}" -ge "$FAILFAST_STRIKES" ]; then
          echo "!!! $name has been in CrashLoopBackOff with $restarts restarts for $FAILFAST_STRIKES checks; aborting install"
          kill "$HELM_PID" 2>/dev/null
          exit 1
        fi
      else
        strikes[$name]=0
      fi
    done < <(kubectl -n "$NAMESPACE" get pods --no-headers 2>/dev/null)
    kubectl -n "$NAMESPACE" get pods --no-headers 2>/dev/null | awk '{printf "  %-55s %-10s %-20s %s\n", $1, $2, $3, $4}'
    kubectl -n "$NAMESPACE" get pods --no-headers 2>/dev/null | awk '$3 !~ /Running|Completed/ {print $1}' | head -5 | while read -r p; do
      echo "  --- $p"; kubectl -n "$NAMESPACE" describe pod "$p" 2>/dev/null | grep -A8 "^Events:" | tail -6 | sed 's/^/      /'
      kubectl -n "$NAMESPACE" logs "$p" --all-containers --tail=8 2>/dev/null | sed 's/^/      | /'
    done
  done
}
# A fresh install applies thousands of Django and ClickHouse migrations; on a 4 vCPU runner
# that takes 20-25 minutes. Real failures are caught earlier by the fail-fast check above.
helm upgrade --install "$RELEASE" "$CHART" -n "$NAMESPACE" -f "$VALUES" --wait --timeout "${HELM_TIMEOUT:-25m}" &
HELM_PID=$!
progress &
PROGRESS_PID=$!
trap 'kill $PROGRESS_PID 2>/dev/null || true' EXIT
wait $HELM_PID || { echo "=== helm failed or was aborted"; exit 1; }
echo "=== install done"
kubectl -n "$NAMESPACE" get pods
