#!/usr/bin/env bash
# Snapshot the logs of any unhealthy pod belonging to a release, on a loop.
#
# Runs in the BACKGROUND during `helm upgrade`. This exists because
# `helm upgrade --atomic` completes its rollback before it returns a non-zero exit
# code — so a diagnostics step that runs after the failure finds the failed
# ReplicaSet, its pods and their logs already deleted. The only place the evidence
# exists is during the upgrade, which is when this is running.
#
#   capture-failing-pods.sh <namespace> <release> <outfile> [interval] [tail]
#
# Each pass OVERWRITES <outfile> with the current picture rather than appending, so
# the file stays readable instead of growing into a 5000-line scroll of near-identical
# snapshots. What survives is the last state before the rollback, which is the one
# that explains the failure.
set -uo pipefail

NAMESPACE="${1:?namespace required}"
RELEASE="${2:?release required}"
OUT="${3:?output file required}"
INTERVAL="${4:-5}"
TAIL="${5:-80}"

SELECTOR="app.kubernetes.io/instance=${RELEASE}"

while :; do
  tmp="${OUT}.partial"
  {
    echo "# snapshot at $(date -u +%H:%M:%SZ)"

    # -o json once, then read it several ways: on a struggling cluster each API call
    # is a chance to add seconds to the loop.
    pods=$(kubectl get pods -n "$NAMESPACE" -l "$SELECTOR" -o json 2>/dev/null)
    [ -n "$pods" ] || { echo "(no pods yet)"; echo; }

    unhealthy=$(printf '%s' "$pods" | jq -r '
      .items[]
      | select(
          .status.phase != "Running"
          or ([.status.containerStatuses // [] | .[] | select(.ready == false)] | length) > 0
        )
      | .metadata.name' 2>/dev/null)

    if [ -z "$unhealthy" ]; then
      echo "(all pods ready)"
    else
      for pod in $unhealthy; do
        echo "===== pod/$pod ====="
        # The container is bound to $c rather than piped: `"..." + (if .state...)`
        # evaluates the if against the STRING just produced, not the container, and
        # jq dies with "Cannot index string with string".
        printf '%s' "$pods" | jq -r --arg p "$pod" '
          .items[] | select(.metadata.name == $p)
          | "phase: \(.status.phase)",
            ( (.status.containerStatuses // [])[] as $c
              | "container \($c.name): ready=\($c.ready) restarts=\($c.restartCount)"
                + " state=\($c.state | keys[0] // "unknown")"
                + (if $c.state.waiting.reason
                   then " reason=\($c.state.waiting.reason)" else "" end)
                + (if $c.state.terminated.reason
                   then " reason=\($c.state.terminated.reason) exit=\($c.state.terminated.exitCode)"
                   else "" end)
            )
        ' 2>/dev/null

        echo "--- logs (last ${TAIL}) ---"
        kubectl logs -n "$NAMESPACE" "$pod" --all-containers \
          --tail="$TAIL" --prefix --timestamps 2>&1 | tail -n "$TAIL"

        # A CrashLoopBackOff pod's current container has usually produced nothing;
        # the reason it died is in the previous one.
        if printf '%s' "$pods" | jq -e --arg p "$pod" \
             '.items[] | select(.metadata.name==$p)
              | (.status.containerStatuses // []) | map(.restartCount) | add > 0' >/dev/null 2>&1; then
          echo "--- previous container logs ---"
          kubectl logs -n "$NAMESPACE" "$pod" --all-containers \
            --tail="$TAIL" --prefix --previous 2>&1 | tail -n "$TAIL"
        fi
        echo
      done
    fi

    # Warning events name the causes pod logs cannot: failed scheduling, image pull
    # errors, probe failures, OOMKills.
    echo "===== warning events ====="
    kubectl get events -n "$NAMESPACE" --field-selector type=Warning \
      --sort-by=.lastTimestamp 2>/dev/null | tail -n 25
  } > "$tmp" 2>&1

  mv -f "$tmp" "$OUT" 2>/dev/null || true
  sleep "$INTERVAL"
done
