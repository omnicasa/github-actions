#!/usr/bin/env bash
# Lint an app repo that has been migrated onto omnicasa/github-actions.
#
#   bash /path/to/github-actions/scripts/check-workflow.sh [app-repo-root]
#
# Every check below corresponds to something that actually broke a deploy in one of
# the seven repos. FAIL means it will break; warn means it will look fine and behave
# wrongly. Exits non-zero on any FAIL.
set -uo pipefail

# Resolved BEFORE the cd below, or $0 is interpreted relative to the app repo.
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

ROOT="${1:-.}"
cd "$ROOT" || { echo "cannot cd to $ROOT"; exit 2; }

FAILS=0
WARNS=0

fail() { echo "FAIL: $*"; FAILS=$((FAILS + 1)); }
warn() { echo "warn: $*"; WARNS=$((WARNS + 1)); }
ok()   { echo "ok  : $*"; }

have() { command -v "$1" >/dev/null 2>&1; }

echo "== checking $(pwd) =="

# ---------------------------------------------------------------- required files
MANIFEST=.github/deploy-manifest.yml
[ -f "$MANIFEST" ] || fail "$MANIFEST is missing — the deploy workflow cannot run without it"
[ -d .github/workflows ] || fail ".github/workflows is missing"
[ -f Dockerfile ] || warn "no Dockerfile at the repo root (fine if build-context differs)"
[ -f .dockerignore ] || warn "no .dockerignore — 'COPY . .' will pull in local node_modules and .next"

WORKFLOWS=$(find .github/workflows -maxdepth 1 -name '*.yml' -o -maxdepth 1 -name '*.yaml' 2>/dev/null)
[ -n "$WORKFLOWS" ] || fail "no workflow files found"

# ------------------------------------------------------------- leftover per-repo
# These are what the migration is supposed to delete. Left behind, they keep
# deploying the old way from a branch nobody remembers.
for stale in k8s/helm k8s/manifest .github/scripts; do
  [ -e "$stale" ] && warn "$stale still exists — the unified chart and shared actions replace it"
done
for f in $WORKFLOWS; do
  if grep -q 'helm upgrade' "$f" 2>/dev/null; then
    fail "$f runs helm directly; callers should only 'uses:' the reusable workflow"
  fi
  if grep -qE 'ovh-ip-restriction|ipRestrictions' "$f" 2>/dev/null; then
    fail "$f has its own OVH allowlist logic; the shared action owns that now"
  fi
done

# ------------------------------------------------------------------ retired keys
# Each of these had a different name in a different repo, which is why nobody could
# move between them. See docs/env-contract.md.
for old in OVH_DOCKER_REGISTRY_URL OVH_SERVICE_NAME OVH_APPLICATION_KEY \
           OVH_APPLICATION_SECRET OVH_CONSUMER_KEY KUBE_CONFIG \
           KUBERNETES_DOCKER_REGISTRY_CREDENTIALS; do
  if grep -rq "$old" .github/ 2>/dev/null; then
    fail "retired key '$old' is still referenced under .github/ — see docs/env-contract.md"
  fi
done
# `secrets.KUBECONFIG` is retired, but `KUBECONFIG` as a plain env var is not.
if grep -rqE 'secrets\.KUBECONFIG([^_]|$)' .github/ 2>/dev/null; then
  fail "secrets.KUBECONFIG is retired — the contract key is secrets.KUBECONFIG_BASE64"
fi

# -------------------------------------------------------------- workflow content
for f in $WORKFLOWS; do
  grep -q '__[A-Z_]*__' "$f" && fail "$f still contains a __PLACEHOLDER__"

  # Only matters for callers of the shared deploy workflow.
  if grep -q 'omnicasa/github-actions/.github/workflows/deploy.yml' "$f"; then
    grep -q 'secrets: inherit' "$f" \
      || fail "$f calls the deploy workflow without 'secrets: inherit'; nothing will resolve"

    ref=$(grep -o 'deploy\.yml@[^ ]*' "$f" | head -1 | cut -d@ -f2)
    case "$ref" in
      v[0-9]*.[0-9]*.[0-9]*) ok "$f pins $ref" ;;
      v[0-9]*)               warn "$f pins the floating tag $ref; an exact vX.Y.Z is reviewable" ;;
      main|master|"")        fail "$f tracks $ref — an unreviewed change to the shared repo deploys straight to production" ;;
      *)                     ok "$f pins $ref" ;;
    esac
  fi

  # `--set` for app config is the comma bug: helm treats , as a list separator, so a
  # rotated password containing one corrupts the release with no code change.
  grep -qE '\-\-set[a-z-]* [^ ]*\$\{\{ *secrets\.' "$f" \
    && fail "$f passes a secret via --set; use a values file"
  grep -q '\-\-create-namespace' "$f" \
    && fail "$f uses --create-namespace; the namespace is pre-created and CI has no RBAC to patch it"
done

# ------------------------------------------------------------- rollback workflow
if ! grep -rql 'rollback\.yml@' .github/workflows/ 2>/dev/null; then
  warn "no rollback workflow — during an incident the fallback is running helm by hand"
else
  # The two must share a concurrency group, which they do by both deriving it from
  # github.repository + environment inside the shared workflows. Just check the pair exists.
  ok "rollback workflow present"
fi

# -------------------------------------------------------------------- manifest
if [ -f "$MANIFEST" ]; then
  if have python3; then
    python3 "$SCRIPT_DIR/validate-manifest.py" "$MANIFEST" || FAILS=$((FAILS + 1))
  else
    warn "python3 not available; manifest not validated"
  fi

  app=$(grep -E '^app:' "$MANIFEST" | head -1 | sed 's/^app:[[:space:]]*//' | tr -d '"'"'")
  values_file=deploy/values.yaml
  if [ -n "$app" ] && [ ! -f "$values_file" ]; then
    warn "$values_file is missing; the chart defaults apply (containerPort 3000, probes on /)"
  fi

  # A chart left in the repo alongside the manifest is ambiguous: which one deploys?
  if [ -d chart ]; then
    warn "chart/ still exists in this repo — the unified chart ships with the shared action"
  fi
fi

# ------------------------------------------------------------------- app values
if [ -f deploy/values.yaml ] && have python3; then
  python3 - <<'PY' || FAILS=$((FAILS + 1))
import sys, yaml
try:
    v = yaml.safe_load(open("deploy/values.yaml")) or {}
except Exception as exc:
    print(f"FAIL: deploy/values.yaml is not valid YAML: {exc}")
    sys.exit(1)
rc = 0
port = (v.get("service") or {}).get("containerPort")
if port is None:
    print("warn: deploy/values.yaml does not set service.containerPort; the chart default is 3000")
for probe in ("livenessProbe", "readinessProbe"):
    p = v.get(probe) or {}
    path = ((p.get("httpGet") or {}).get("path"))
    if path == "/":
        print(f"warn: {probe} targets '/'; --atomic only means something if the probe "
              "fails when a dependency is down")
# Setting these on an existing release rewrites the immutable Deployment selector.
for key in ("nameOverride", "fullnameOverride"):
    if v.get(key):
        print(f"FAIL: deploy/values.yaml sets {key}; this changes the immutable "
              "Deployment selector and every future upgrade will fail")
        rc = 1
sys.exit(rc)
PY
fi

# ------------------------------------------------------------------- actionlint
if have actionlint; then
  if actionlint; then
    ok "actionlint clean"
  else
    fail "actionlint reported issues"
  fi
else
  warn "actionlint not installed: go install github.com/rhysd/actionlint/cmd/actionlint@latest"
fi

echo
echo "$FAILS FAIL, $WARNS warn"
[ "$FAILS" -eq 0 ]
