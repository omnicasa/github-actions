#!/usr/bin/env bash
set -euo pipefail

# Bump together: the version and the checksum of its linux_x64 tarball, taken from the
# release's gitleaks_<version>_checksums.txt.
VERSION=8.30.1
SHA256=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb

bin_dir="${RUNNER_TEMP:-/tmp}/gitleaks-bin"
mkdir -p "$bin_dir" "$REPORT_DIR"
tarball="$bin_dir/gitleaks.tar.gz"

curl -fsSL --retry 3 -o "$tarball" \
  "https://github.com/gitleaks/gitleaks/releases/download/v${VERSION}/gitleaks_${VERSION}_linux_x64.tar.gz"
echo "$SHA256  $tarball" | sha256sum -c - >/dev/null \
  || { echo "::error title=gitleaks checksum::downloaded gitleaks ${VERSION} does not match the pinned SHA-256"; exit 1; }
tar -xzf "$tarball" -C "$bin_dir" gitleaks

config="${CONFIG:-}"
[ -z "$config" ] && [ -f .gitleaks.toml ] && config=.gitleaks.toml

# --redact: the secret never reaches a log or a report. Decoding catches base64/hex
# wrapped values at the cost of a little time.
args=(git --redact --exit-code 0 --no-banner --max-decode-depth 3)
[ -n "$config" ] && args+=(--config "$config")

"$bin_dir/gitleaks" "${args[@]}" --report-format json --report-path "$REPORT_DIR/gitleaks.json" .
"$bin_dir/gitleaks" "${args[@]}" --report-format sarif --report-path "$REPORT_DIR/gitleaks.sarif" . >/dev/null

count=$(jq 'length' "$REPORT_DIR/gitleaks.json")
echo "findings=$count" >> "$GITHUB_OUTPUT"

{
  echo "### Gitleaks (v${VERSION}, full history)"
  if [ "$count" -eq 0 ]; then
    echo "No secrets found."
  else
    echo "**${count} potential secret(s) found.** Values are redacted; rotate anything real, then add a \`.gitleaksignore\` entry for false positives."
    echo
    echo "| Rule | File | Line | Commit | Author |"
    echo "|---|---|---|---|---|"
    jq -r '.[:50][] | "| \(.RuleID) | \(.File) | \(.StartLine) | \(.Commit[0:8]) | \(.Author) |"' "$REPORT_DIR/gitleaks.json"
    [ "$count" -gt 50 ] && echo && echo "…and $((count - 50)) more in the artifact."
  fi
} >> "$GITHUB_STEP_SUMMARY"

if [ "$count" -gt 0 ]; then
  echo "::warning title=Gitleaks::${count} potential secret(s) in git history. See the job summary."
fi
