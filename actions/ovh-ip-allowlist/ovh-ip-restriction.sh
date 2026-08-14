#!/bin/sh
# Add or remove the runner's public IP on the OVH managed-Kubernetes API allowlist.
#
# Usage: ovh-ip-restriction.sh add|remove [ip]
#
# Required environment:
#   OVH_PROJECT_ID OVH_KUBERNETES_ID
#   OVH_GA_APPLICATION_KEY OVH_GA_APPLICATION_SECRET OVH_GA_CONSUMER_KEY
# Optional:
#   OVH_API_URL   default https://api.ovh.com/1.0
#   RETRIES       default 3
#   FAIL_ON_ERROR default true for add, callers pass false for remove
#
# On success prints the IP it acted on to stdout. Everything else goes to stderr,
# so the caller can capture the IP with a command substitution.
set -eu

action="${1:-}"
forced_ip="${2:-}"

API_URL="${OVH_API_URL:-https://api.ovh.com/1.0}"
RETRIES="${RETRIES:-3}"
FAIL_ON_ERROR="${FAIL_ON_ERROR:-true}"

log() { echo "$@" >&2; }

die() {
  if [ "$FAIL_ON_ERROR" = "true" ]; then
    log "::error::$*"
    exit 1
  fi
  # Removal runs under `if: always()`. Failing hard there would replace the real
  # deploy error in the job summary with this one, which is strictly less useful.
  log "::warning::$* (continuing: FAIL_ON_ERROR=false)"
  exit 0
}

case "$action" in
  add|remove) ;;
  *) log "usage: $0 add|remove [ip]"; exit 1 ;;
esac

for v in OVH_PROJECT_ID OVH_KUBERNETES_ID OVH_GA_APPLICATION_KEY \
         OVH_GA_APPLICATION_SECRET OVH_GA_CONSUMER_KEY; do
  eval "val=\${$v:-}"
  [ -n "$val" ] || die "$v is not set"
done

# The IP is passed in on removal so we delete the same entry we added, even if the
# runner's egress address rotated during the job.
if [ -n "$forced_ip" ]; then
  agent_ip="$forced_ip"
else
  agent_ip=$(curl -fsS --max-time 10 https://api.ipify.org/ || true)
  [ -n "$agent_ip" ] || die "could not determine the runner's public IP"
fi

if [ "$action" = "add" ]; then
  method="POST"
  url="/cloud/project/${OVH_PROJECT_ID}/kube/${OVH_KUBERNETES_ID}/ipRestrictions"
  post_data="{\"ips\": [\"${agent_ip}/32\"]}"
else
  method="DELETE"
  # The /32 must be URL-encoded in the path, or OVH 404s.
  url="/cloud/project/${OVH_PROJECT_ID}/kube/${OVH_KUBERNETES_ID}/ipRestrictions/${agent_ip}%2F32"
  post_data=""
fi

attempt=1
while :; do
  # Timestamp and signature are recomputed every attempt. OVH validates the
  # timestamp against its own clock with a narrow tolerance, so a signature made
  # before a backoff sleep would be rejected as replay on the retry.
  timestamp=$(curl -fsS --max-time 10 "$API_URL/auth/time" || true)
  [ -n "$timestamp" ] || die "could not fetch the OVH API timestamp"

  # OVH v1 signature: $1$ + sha1(secret+consumer+METHOD+fullurl+body+timestamp).
  # `printf '%s'` rather than `echo -n`: the inline copies in the old per-repo
  # workflows used echo, whose -n handling is not portable across shells.
  sig_data="${OVH_GA_APPLICATION_SECRET}+${OVH_GA_CONSUMER_KEY}+${method}+${API_URL}${url}+${post_data}+${timestamp}"
  sig="\$1\$$(printf '%s' "$sig_data" | openssl dgst -sha1 | sed -e 's/^.* //')"

  response=$(curl -sS -w '\n%{http_code}' -X "$method" \
    --max-time 30 \
    --header 'Content-Type:application/json;charset=utf-8' \
    --header "X-Ovh-Application:${OVH_GA_APPLICATION_KEY}" \
    --header "X-Ovh-Timestamp:${timestamp}" \
    --header "X-Ovh-Signature:${sig}" \
    --header "X-Ovh-Consumer:${OVH_GA_CONSUMER_KEY}" \
    ${post_data:+--data "$post_data"} \
    "${API_URL}${url}" 2>&1) || response="$response
000"

  http_code=$(printf '%s' "$response" | tail -n1)
  body=$(printf '%s' "$response" | sed '$d')

  case "$http_code" in
    2*)
      log "ovh-ip-allowlist: ${action} ${agent_ip}/32 -> HTTP ${http_code}"
      printf '%s\n' "$agent_ip"
      exit 0
      ;;
    404)
      if [ "$action" = "remove" ]; then
        # Already gone. A previous run's cleanup, or a manual prune.
        log "ovh-ip-allowlist: ${agent_ip}/32 was not on the allowlist, nothing to remove"
        printf '%s\n' "$agent_ip"
        exit 0
      fi
      ;;
  esac

  # This status check is the whole point of the rewrite. The previous version
  # ignored it, so a failed `add` exited 0 and the failure only surfaced ten
  # minutes later as an opaque kubectl connection timeout.
  log "ovh-ip-allowlist: attempt ${attempt}/${RETRIES} failed, HTTP ${http_code}: ${body}"
  if [ "$attempt" -ge "$RETRIES" ]; then
    die "failed to ${action} ${agent_ip}/32 after ${RETRIES} attempts (HTTP ${http_code})"
  fi
  attempt=$((attempt + 1))
  sleep $((attempt * 2))
done
