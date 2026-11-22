#!/usr/bin/env bash
set -euo pipefail

# Bump together: the version and the checksum of its Linux-64bit tarball, taken from the
# release's trivy_<version>_checksums.txt.
VERSION=0.76.2
SHA256=bc0fc726e017b8927a62b2d3531d7a84ee01769daf5c99ed866dced54089fdbf

bin_dir="${RUNNER_TEMP:-/tmp}/trivy-bin"
mkdir -p "$bin_dir" "$REPORT_DIR"
tarball="$bin_dir/trivy.tar.gz"

curl -fsSL --retry 3 -o "$tarball" \
  "https://github.com/aquasecurity/trivy/releases/download/v${VERSION}/trivy_${VERSION}_Linux-64bit.tar.gz"
echo "$SHA256  $tarball" | sha256sum -c - >/dev/null \
  || { echo "::error title=trivy checksum::downloaded trivy ${VERSION} does not match the pinned SHA-256"; exit 1; }
tar -xzf "$tarball" -C "$bin_dir" trivy
trivy="$bin_dir/trivy"

# Two DB mirrors: ghcr.io rate-limits anonymous pulls from shared runner IPs.
common=(--no-progress --quiet
  --db-repository public.ecr.aws/aquasecurity/trivy-db:2,ghcr.io/aquasecurity/trivy-db:2
  --java-db-repository public.ecr.aws/aquasecurity/trivy-java-db:1,ghcr.io/aquasecurity/trivy-java-db:1)
[ -f .trivyignore ] && common+=(--ignorefile .trivyignore)

case "$MODE" in
  image)
    label="image ${TARGET}"
    # Everything Trivy can say about an image; --ignore-unfixed is deliberately off.
    scan=(image --scanners vuln,secret --image-src remote --platform "$PLATFORM" "$TARGET")
    "$trivy" "${common[@]}" image --format cyclonedx --image-src remote --platform "$PLATFORM" \
      --output "$REPORT_DIR/trivy-image.sbom.cdx.json" "$TARGET" || echo "::warning title=Trivy SBOM::SBOM generation failed"
    stem=trivy-image
    ;;
  fs)
    label="source tree"
    scan=(fs --scanners vuln,secret,misconfig,license "$TARGET")
    stem=trivy-fs
    ;;
  *)
    echo "::error::mode must be image or fs, got '$MODE'"; exit 1 ;;
esac

"$trivy" "${common[@]}" "${scan[@]}" --format json --exit-code 0 --output "$REPORT_DIR/$stem.json"
"$trivy" convert --format sarif --output "$REPORT_DIR/$stem.sarif" "$REPORT_DIR/$stem.json"

# One flat list of findings across the four scanners. Secret values are never read: the
# summary names the rule and the file, nothing else.
flat='[ .Results[]? | .Target as $t
  | ( (.Vulnerabilities[]?     | {kind:"vuln",    sev:.Severity, id:.VulnerabilityID, what:(.PkgName + " " + .InstalledVersion), fix:(.FixedVersion // ""), target:$t}),
      (.Secrets[]?             | {kind:"secret",  sev:.Severity, id:.RuleID,          what:.Title,                               fix:"",                      target:$t}),
      (.Misconfigurations[]?   | {kind:"misconf", sev:.Severity, id:.ID,              what:.Title,                               fix:"",                      target:$t}),
      (.Licenses[]?            | {kind:"license", sev:.Severity, id:.Name,            what:.PkgName,                             fix:"",                      target:$t}) ) ]'
jq "$flat" "$REPORT_DIR/$stem.json" > "$REPORT_DIR/$stem.findings.json"

count_of() { jq --arg s "$1" '[.[] | select(.sev == $s)] | length' "$REPORT_DIR/$stem.findings.json"; }
critical=$(count_of CRITICAL); high=$(count_of HIGH); medium=$(count_of MEDIUM); low=$(count_of LOW)
secrets=$(jq '[.[] | select(.kind == "secret")] | length' "$REPORT_DIR/$stem.findings.json")
{
  echo "critical=$critical"
  echo "high=$high"
} >> "$GITHUB_OUTPUT"

{
  echo "### Trivy v${VERSION}: ${label}"
  echo
  echo "| Critical | High | Medium | Low | Secrets |"
  echo "|---|---|---|---|---|"
  echo "| $critical | $high | $medium | $low | $secrets |"
  if [ $((critical + high)) -gt 0 ]; then
    echo
    echo "Top critical and high findings (full list in the artifact):"
    echo
    echo "| Severity | Type | ID | Detail | Fixed in | Where |"
    echo "|---|---|---|---|---|---|"
    jq -r '[.[] | select(.sev == "CRITICAL" or .sev == "HIGH")]
           | sort_by(.sev != "CRITICAL")[:25][]
           | "| \(.sev) | \(.kind) | \(.id) | \(.what) | \(.fix) | \(.target) |"' "$REPORT_DIR/$stem.findings.json"
  fi
} >> "$GITHUB_STEP_SUMMARY"

if [ $((critical + secrets)) -gt 0 ] || [ "$high" -gt 0 ]; then
  echo "::warning title=Trivy ${MODE}::${critical} critical, ${high} high, ${secrets} secret finding(s) in ${label}. See the job summary."
fi
