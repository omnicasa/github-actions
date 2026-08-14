#!/usr/bin/env bash
# Dump everything needed to diagnose a failed deploy.
#
# This is the highest-value step in the pipeline. `helm upgrade --atomic` rolls the
# release back before the job ends, deleting the failed ReplicaSet, its pods and
# their logs — so without this dump the only evidence left is "the deploy failed".
#
# Every command is best-effort: the point is to gather what is still there, not to
# fail on what is already gone.
set -uo pipefail

NAMESPACE="${1:?namespace required}"
RELEASE="${2:?release required}"
EVENT_LINES="${3:-40}"
LOG_LINES="${4:-200}"

section() { echo; echo "===== $* ====="; }

section "pods"
kubectl get pods -n "$NAMESPACE" -o wide || true

section "deployment/$RELEASE"
kubectl describe "deployment/$RELEASE" -n "$NAMESPACE" || true

section "replicasets"
kubectl get replicasets -n "$NAMESPACE" \
  -l "app.kubernetes.io/instance=$RELEASE" || true

section "warning events (last $EVENT_LINES)"
kubectl get events -n "$NAMESPACE" \
  --field-selector type=Warning \
  --sort-by=.lastTimestamp 2>/dev/null | tail -n "$EVENT_LINES" || true

section "all events (last $EVENT_LINES)"
kubectl get events -n "$NAMESPACE" --sort-by=.lastTimestamp 2>/dev/null \
  | tail -n "$EVENT_LINES" || true

# The newest not-Ready pod is almost always the one that failed the rollout.
not_ready=$(kubectl get pods -n "$NAMESPACE" \
  -l "app.kubernetes.io/instance=$RELEASE" \
  --sort-by=.metadata.creationTimestamp \
  -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.phase}{"\n"}{end}' 2>/dev/null \
  | awk '$2 != "Running" && $2 != "Succeeded" {print $1}' | tail -1)

if [ -n "$not_ready" ]; then
  section "describe newest unhealthy pod: $not_ready"
  kubectl describe "pod/$not_ready" -n "$NAMESPACE" || true
fi

section "container logs (last $LOG_LINES lines per container)"
kubectl logs -n "$NAMESPACE" \
  --selector "app.kubernetes.io/instance=$RELEASE" \
  --all-containers --tail="$LOG_LINES" --prefix || true

# A crash-looping pod's useful output is in the PREVIOUS container, not the current one.
section "previous container logs (crash loops)"
kubectl logs -n "$NAMESPACE" \
  --selector "app.kubernetes.io/instance=$RELEASE" \
  --all-containers --tail="$LOG_LINES" --prefix --previous 2>/dev/null || \
  echo "(no previous containers)"

# A failed migration hook Job is deleted by the next deploy, not by --atomic, so
# it is usually still here and its logs explain the failure directly.
section "migration job"
kubectl describe "job/${RELEASE}-migrate" -n "$NAMESPACE" 2>/dev/null || echo "(no migration job)"
kubectl logs -n "$NAMESPACE" "job/${RELEASE}-migrate" --tail="$LOG_LINES" 2>/dev/null || true

section "helm history"
helm history "$RELEASE" -n "$NAMESPACE" --max 10 || true
