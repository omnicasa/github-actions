# Changelog

Consumers pin an exact tag, so every entry here is something a bump can change under you.
Read it before moving a pin.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: semver, where
the "API" is the workflow inputs, the action inputs, and the chart values.

## [v1.5.0] — 2026-08-16

### Changed

- `branch-guard.yml` accepts two more branch kinds by default: `perf/` (speed or resource
  work with no behaviour change) and `ci/` (pipeline and workflow changes, previously
  forced into `chore/`). Both `allowed-into-main` and `allowed-into-develop` now default to
  `feat/,fix/,perf/,refactor/,chore/,ci/,hotfix/`.

  Widening only — no branch name that passed before fails now, so no consumer action is
  needed. A repo that pinned a narrower list via `allowed-into-main` keeps its own list.

## [v1.4.0] — 2026-08-14

Supersedes v1.3.0, which was tagged but never promoted to `v1` — its release run failed
the "unrecorded change" guard, so `v1` stayed on v1.2.0. Everything below is therefore
new to any consumer tracking `v1`, including the parts first tagged in v1.3.0.

### Added

- **`branch-guard.yml`** — a reusable workflow enforcing the house branch naming on
  pull requests. Default: `main` accepts `feat/`, `fix/`, `chore/`, `refactor/` and
  `hotfix/`. Both allow-lists are inputs. Unknown base branches are skipped rather
  than blocked, so adding a `release/*` flow later does not require touching this first.

- **`templates/caller-deploy.yml`** — the house flow. A PR into `main` deploys its head
  commit to staging; the merge to `main` deploys production. There is no `develop`
  branch: an integration branch means code lands somewhere shared before anyone has
  run it. `hotfix/*` takes the same route as everything else — it is a label, not a
  second pipeline, and gets no separate domain.

- **`templates/caller-deploy-no-staging.yml`** — the same, minus the staging lane, for
  a repo with no staging cluster entry. Switching later is a two-line change.

- **`templates/caller-branch-guard.yml`** — three-line caller for the guard.

### Deprecated

- `templates/caller-deploy-gitflow.yml`, `caller-deploy-1branch.yml` and
  `caller-deploy-2branch.yml` are superseded by the two templates above and carry a
  header saying so. They target a `develop` branch or the older naming. Nothing
  references them; they are kept only while repos migrate.

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

## [v1.3.0] — 2026-08-14 — do not use

Tagged, but `v1` was never moved to it: the release run failed because CHANGELOG.md had
no `v1.3.0` entry, so the tag exists while `v1` stayed on v1.2.0. Its branch flow —
`feat/* → develop → main`, with staging deployed from the PR into `develop` — was
replaced before it shipped. Pin v1.4.0 or `v1` instead.

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
