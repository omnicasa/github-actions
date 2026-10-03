#!/usr/bin/env bash
# Move the floating major tag (v1) onto a released vX.Y.Z, with your own credentials.
#
#   bash scripts/move-major.sh v1.14.1 [-y]
#
# release.yml cannot: GITHUB_TOKEN may not point a ref at a tree containing
# .github/workflows changes, by git push or by the refs API, and nearly every release
# here changes a workflow.
set -euo pipefail

tag="${1:-}"
assume_yes=false
[ "${2:-}" = "-y" ] && assume_yes=true

if [ -z "$tag" ]; then
  echo "usage: bash scripts/move-major.sh vX.Y.Z [-y]" >&2
  exit 2
fi

case "$tag" in
  v[0-9]*.[0-9]*.[0-9]*) ;;
  *) echo "error: $tag is not vX.Y.Z" >&2; exit 2 ;;
esac

major="${tag%%.*}"

git fetch origin --tags --force --quiet

if ! git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
  echo "error: $tag does not exist — tag the release first" >&2
  exit 1
fi

target=$(git rev-list -n1 "$tag")

# v1.12.0 was tagged off-main, leaving v1 on a commit reachable from no branch.
if ! git merge-base --is-ancestor "$target" origin/main; then
  echo "error: $tag ($target) is not in origin/main's history" >&2
  exit 1
fi

current=$(git rev-list -n1 "$major" 2>/dev/null || echo "")

if [ "$current" = "$target" ]; then
  echo "$major already points at $tag ($target)"
  exit 0
fi

# Releasing an older tag after a newer one rolls every consumer back; nothing else checks.
if [ -n "$current" ] && git merge-base --is-ancestor "$target" "$current"; then
  echo "error: $tag ($target) is behind the current $major ($current)." >&2
  echo "       Moving it would roll every consumer back. Tag a newer version instead." >&2
  exit 1
fi

echo "$major: ${current:-none} -> $target ($tag)"

if [ "$assume_yes" = false ]; then
  printf 'Every consumer tracking %s picks this up on its next deploy. Continue? [y/N] ' "$major"
  read -r reply
  case "$reply" in
    y|Y) ;;
    *) echo "aborted"; exit 1 ;;
  esac
fi

git tag -f -a "$major" -m "Track $tag" "$target"
git push -f origin "$major"
echo "$major now points at $tag ($target)"
