# Changelog

Consumers pin an exact tag, so every entry here is something a bump can change under you.
Read it before moving a pin.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: semver, where
the "API" is the workflow inputs, the action inputs, and the chart values.

## [Unreleased]

### Added

- **`branch-guard.yml`** — a reusable workflow enforcing the gitflow branch naming on
  pull requests. Defaults: `main` accepts `develop` and `hotfix/*`; `develop` accepts
  `feat/*`, `fix/*`, `chore/*`, `refactor/*`, `hotfix/*`. Both lists are inputs.
  Unknown base branches are skipped rather than blocked, so adding a `release/*` flow
  later does not require touching this first.

- **`templates/caller-deploy-gitflow.yml`** — PRs into `develop` deploy to staging for
  QA; `develop` itself deploys nothing (what QA signed off on is already the commit
  that was deployed); pushes to `main` deploy production.

- **`templates/caller-branch-guard.yml`** — three-line caller for the guard.

### Fixed

- `deploy.yml` is now pull-request-aware. Three things were wrong for PR triggers:

  - The image was tagged `github.sha`, which on a `pull_request` is the **merge**
    commit — a synthetic commit on no branch that changes whenever the base moves,
    so the tag could not be traced to anything. Now tags `pull_request.head.sha`.
  - `actions/checkout` took the merge ref while the tag named the head commit, so the
    image contents and its tag described different code. Both now use the head commit.
  - The environment fell back to `github.head_ref`, which on a PR is a branch name
    like `feat/login`, not an environment. That fallback is gone, and a new preflight
    step fails fast if the resolved environment looks like a branch ref (`42/merge`,
    `feat/login`) instead of resolving no secrets and failing obscurely later.

## [v1.2.0] — 2026-08-14

### Added

- `helm-deploy` now captures unhealthy pods **during** the upgrade, not after it.

  A background loop snapshots pod phase, container state and reason, current and
  `--previous` logs, and warning events every 5s while `helm upgrade` runs, and prints
  the last snapshot to the log and the job summary if the upgrade fails.

  This closes a real hole: `helm upgrade --atomic` completes its rollback *before*
  returning non-zero, deleting the failed ReplicaSet, its pods and their logs. Anything
  that inspects the cluster after the failure — including the existing `k8s-diagnostics`
  step — finds it already gone. The only moment the evidence exists is during the
  upgrade.

- `kubectl rollout status` failures now print the same pod state and logs. This covers
  the case `--atomic` does not: a release helm considers successful whose pods never
  settle. Nothing has been deleted at that point, so the failing pods are still readable.

## [v1.1.0] — 2026-08-14

### Removed — breaking for any manifest using `migration:`

- `migration` is no longer a `deploy-manifest.yml` key. The chart reads `migration.*`
  from `deploy/values.yaml` and always did; the manifest copy was only ever used to emit
  an action output that nothing consumed. So `migration.enabled: true` in a manifest
  looked like the switch and did nothing.

  Both `render-values.py` and `validate-manifest.py` now **hard-fail** if the key is
  present, naming the fix, rather than ignoring it silently. Move `migration.*` into
  `deploy/values.yaml`.

  Also drops the unused `migration-enabled` output from the `render-values` action.

## [v1.0.0] — 2026-08-14

First release. `omnicasa-tools` is the only consumer.

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
