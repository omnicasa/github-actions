# Changelog

Consumers pin an exact tag, so every entry here is something a bump can change under you.
Read it before moving a pin.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: semver, where
the "API" is the workflow inputs, the action inputs, and the chart values.

## [Unreleased]

### Added

- Reusable `deploy.yml` (`workflow_call`): test → build → deploy, reproducing the
  `Omnicasa-Oauth2-Consent-App` pipeline step for step.
- Reusable `rollback.yml`, sharing the deploy concurrency group so the two can never
  overlap on one release.
- Composite actions: `setup-cluster`, `ovh-ip-allowlist`, `render-values`, `helm-deploy`,
  `helm-rollback`, `k8s-diagnostics`.
- Unified chart `omnicasa-app`, vendored at `actions/helm-deploy/chart`.
- `scripts/check-workflow.sh` and `scripts/validate-manifest.py`.
- `docs/estate-audit.md` and `docs/migration-backlog.md` — the seven-repo audit that
  motivated this repo, kept so it need not be re-derived.

### Fixed (relative to the per-repo pipelines this replaces)

- `ovh-ip-restriction.sh` now checks the HTTP status. Previously a failed allowlist `add`
  exited 0 and surfaced ten minutes later as an opaque `kubectl` timeout.
- ConfigMap and Secret templates quote values properly. `{{ $key }}: "{{ $val }}"` produced
  invalid YAML for any value containing `"`, `:` or a newline.
- `PodDisruptionBudget` is opt-in. It used to render unconditionally at `maxUnavailable: 50%`,
  which blocks node drains on a single-replica Deployment.
- `concurrency.group` includes the repository, so repos do not serialise against each other.
- `chart.name` defaults to `.Release.Name`, not `.Chart.Name`. With a shared chart the old
  default would rewrite `app.kubernetes.io/name` and hit the immutable
  `Deployment.spec.selector`, failing every migration outright.
