#!/usr/bin/env bash
set -euo pipefail

# Bump together: the version and the checksum of its linux_x64 tarball, taken from the
# release's gitleaks_<version>_checksums.txt.
VERSION=8.30.1
SHA256=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb

# Only the linux x86-64 build is pinned and checksummed here.
if [ "$(uname -m)" != x86_64 ]; then
  echo "::warning title=Gitleaks::only x86-64 Linux runners are supported (this one is $(uname -m)); scan skipped"
  exit 0
fi

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

scope="full history"
log_opts="${LOG_OPTS:-}"
if [ -n "$log_opts" ]; then
  start="${log_opts%%..*}"
  # A force push or a shallow clone can leave the range start missing; scanning
  # everything is slower but never silently scans nothing.
  if git cat-file -e "${start}^{commit}" 2>/dev/null; then
    args+=(--log-opts="$log_opts")
    end="${log_opts##*..}"
    scope="new commits ${start:0:8}..${end:0:8}"
  else
    echo "::notice title=Gitleaks::range start ${start:0:8} is not in the clone; scanning the full history"
  fi
fi

"$bin_dir/gitleaks" "${args[@]}" --report-format json --report-path "$REPORT_DIR/gitleaks.json" .
"$bin_dir/gitleaks" "${args[@]}" --report-format sarif --report-path "$REPORT_DIR/gitleaks.sarif" . >/dev/null

count=$(jq 'length' "$REPORT_DIR/gitleaks.json")
echo "findings=$count" >> "$GITHUB_OUTPUT"

{
  echo "### Gitleaks (v${VERSION}, ${scope})"
  if [ "$count" -eq 0 ]; then
    echo "No secrets found in ${scope}."
  else
    echo "**${count} potential secret(s) found.** Values are redacted; rotate anything real, then add a \`.gitleaksignore\` entry for false positives."
    echo
    echo "| Rule | File | Line | Commit | Author |"
    echo "|---|---|---|---|---|"
    # md: a file name or commit author can hold a "|" or a newline; keep it in its cell.
    jq -r 'def md: tostring | gsub("[\r\n]+"; " ") | gsub("\\|"; "\\|") | .[0:120];
           .[:50][] | "| \(.RuleID | md) | \(.File | md) | \(.StartLine | md) | \(.Commit[0:8] | md) | \(.Author | md) |"' "$REPORT_DIR/gitleaks.json"
    [ "$count" -gt 50 ] && echo && echo "…and $((count - 50)) more in the artifact."
  fi
} >> "$GITHUB_STEP_SUMMARY"

if [ "$count" -gt 0 ]; then
  echo "::warning title=Gitleaks::${count} potential secret(s) in ${scope}. See the job summary."
fi
