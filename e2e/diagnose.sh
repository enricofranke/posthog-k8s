#!/usr/bin/env bash
# Collect what is needed to understand a failed install: pod table, container exit reasons,
# events, current AND previous logs of every container that is not ready.
set +e
NS="${1:-posthog}"
echo "################ pods"
kubectl -n "$NS" get pods -o wide
echo "################ container states (name / ready / restarts / last termination)"
kubectl -n "$NS" get pods -o json | python3 -c '
import json,sys
for p in json.load(sys.stdin)["items"]:
    for c in p["status"].get("initContainerStatuses",[])+p["status"].get("containerStatuses",[]):
        last=c.get("lastState",{}).get("terminated") or {}
        cur=c.get("state",{})
        state=next(iter(cur)) if cur else "?"
        detail=cur.get("waiting",{}).get("reason","") or cur.get("terminated",{}).get("reason","")
        print(f"{p[\"metadata\"][\"name\"]:55} {c[\"name\"]:26} ready={str(c[\"ready\"]):5} restarts={c[\"restartCount\"]:3} state={state}/{detail} last={last.get(\"reason\",\"-\")}/exit={last.get(\"exitCode\",\"-\")}")
'
echo "################ events"
kubectl -n "$NS" get events --sort-by=.lastTimestamp | grep -v "Normal" | tail -60
for p in $(kubectl -n "$NS" get pods -o json | python3 -c '
import json,sys
for p in json.load(sys.stdin)["items"]:
    ready=all(c.get("ready") for c in p["status"].get("containerStatuses",[])) and p["status"].get("phase")=="Running"
    if not ready and p["status"].get("phase")!="Succeeded": print(p["metadata"]["name"])'); do
  echo "################ $p (describe: conditions + events)"
  kubectl -n "$NS" describe pod "$p" | sed -n "/^Conditions:/,/^Volumes:/p" | head -30
  kubectl -n "$NS" describe pod "$p" | sed -n "/^Events:/,\$p" | tail -15
  for c in $(kubectl -n "$NS" get pod "$p" -o jsonpath="{.spec.initContainers[*].name} {.spec.containers[*].name}"); do
    echo "---------------- $p/$c current log"
    kubectl -n "$NS" logs "$p" -c "$c" --tail=60 2>&1 | cut -c1-400
    echo "---------------- $p/$c PREVIOUS log"
    kubectl -n "$NS" logs "$p" -c "$c" --previous --tail=60 2>&1 | cut -c1-400
  done
done
echo "################ healthy pods: last 15 lines each"
for p in $(kubectl -n "$NS" get pods -o name); do echo "=== $p"; kubectl -n "$NS" logs "$p" --all-containers --tail=15 2>&1 | cut -c1-300; done
