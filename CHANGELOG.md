# Changelog

Consumers pin an exact tag, so every entry here is something a bump can change under you.
Read it before moving a pin.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: semver, where
the "API" is the workflow inputs, the action inputs, and the chart values.

## [v1.9.0] — 2026-09-05

### Added

- **Production deploys and rollbacks can now notify Microsoft Teams.** `deploy.yml` and
  `rollback.yml` each gained a `notify` job, scoped to `production` only, via the new
  `actions/teams-notify`. Two channels: `MSTEAMS_WEBHOOK_URL_DEPLOYS` posts every
  production deploy or rollback, success or failure; `MSTEAMS_WEBHOOK_URL_ALERTS` posts
  failures only. Both are optional **organization**-scoped secrets — a repo without
  either just gets a blank webhook-url and the notify step no-ops.

  They must stay org-scoped rather than per-environment: the notify job declares no
  `environment:` on purpose, so a failure alert is never stuck waiting behind
  production's required-reviewer gate. See
  [multi-environment.md](docs/multi-environment.md#production-alerts-to-ms-teams).

  The webhook is a Power Automate "Workflows" incoming webhook, not the legacy Office
  365 Connector — Microsoft retired the latter, and it silently drops the Adaptive Card
  payload this sends.

  **Migration.** No repo has either secret today, so nothing changes for anyone on this
  bump until an org secret is created and granted.

### Changed

- **`prodtest` now builds its own image instead of promoting staging's.** Its default
  registry prefix moves from `staging` to `prodtest`, so the ref goes from
  `<registry>/staging/<app>:<sha>` to `<registry>/prodtest/<app>:<sha>`.
  `REPOSITORY_PREFIX` in `actions/resolve-target/target.py` is now empty; every
  environment reads its own name.

  Promotion was the better design and is still available — it just cannot be the
  *default* on an estate where stage-k8s and prod-k8s have separate OVH registries. A
  prodtest pod reaching for `staging/<app>` needs stage-registry credentials in a
  prod-k8s namespace, which no namespace here has, so the old default produced
  `ImagePullBackOff` on first use.

  **Migration.** No repo has `prodtest` enabled today, so nothing in the estate changes
  on this bump. A repo that DOES promote — any caller with a `build-only: true` job
  feeding prodtest, `templates/caller-deploy-4env.yml` included — must add:

  ```yaml
  environments:
    prodtest:
      repositoryPrefix: staging
  ```

  Without it the build pushes `staging/<app>` and the deploy pulls `prodtest/<app>`.
  `check-workflow.sh` now **fails** on that combination instead of letting it reach a
  cluster; the old warning, which fired when a repo did *not* promote, is gone because
  not promoting is now correct.

  The prodtest namespace is unaffected — still `prodtest-<app>`, from the separate
  `NAMESPACE_PREFIX` table.

## [v1.8.0] — 2026-09-04

### Changed

- **The chart's default `cert-manager.io/cluster-issuer` is now `letsencrypt-dns01`,
  was `letsencrypt`.** DNS-01 does not need the host reachable on :80, so it serves
  environments that are not publicly exposed and is the only solver that can issue a
  wildcard.

  This changes every release that does not set the annotation itself, on its next
  deploy — cert-manager sees a new issuer on the Ingress and re-issues. **Before moving
  a pin, confirm `letsencrypt-dns01` exists on the target cluster and its solver holds
  credentials for that app's zone.** If it does not, issuance fails while the existing
  certificate stays in the Secret, so the release goes green and nothing breaks until
  that certificate reaches renewal, weeks later.

  Both stage-k8s and prod-k8s were given the issuer before this release, so no Omnicasa
  app needs to act. The warning above is for a cluster added later.

  Apps that need HTTP-01 pin it back per environment:

  ```yaml
  # deploy/values.<environment>.yaml
  ingress:
    annotations:
      cert-manager.io/cluster-issuer: letsencrypt
  ```

  The golden render does not cover this: `tests/fixtures/omnicasa-tools` sets the
  annotation explicitly, so `tests/golden/omnicasa-tools.yaml` is unchanged and CI
  stays green either way.

## [v1.7.1] — 2026-08-21

### Fixed

- `render-build-args` emitted `secret-files` in the buildx **CLI** form
  `id=NAME,src=PATH`. `docker/build-push-action` splits each line on the first `=` and
  treats the remainder as the filename, so it looked for a file called
  `NAME,src=/path`, logged `##[warning]secret file ... not found`, and **carried on**.
  Every declared build secret then reached the Dockerfile as an empty string, and the
  build failed much later somewhere unrelated — for a Next.js app, as "Failed to
  collect page data" naming a route with nothing to do with the missing value.

  The correct form is `NAME=PATH`. CI now asserts it and rejects the CLI form, because
  the failure mode is a warning rather than an error and is otherwise invisible.

  Anyone on v1.7.0 using `buildArgs.secrets` must move to v1.7.1; `buildArgs.variables`
  was unaffected.

## [v1.7.0] — 2026-08-21

### Added

- **Manifest-driven docker build args.** A new `buildArgs:` block in
  `.github/deploy-manifest.yml` resolves build inputs from the target GitHub
  Environment, rendered by the new `actions/render-build-args` action inside the
  deploy workflow's build job:
  - `buildArgs.variables` → `--build-arg NAME=value`, read from `vars.*`
  - `buildArgs.secrets` → buildx `--secret id=NAME`, read from `secrets.*` and mounted
    as a file, so a credential the build genuinely needs is never written into the
    image metadata or the GHA cache. Each name needs a matching
    `RUN --mount=type=secret,id=<NAME>` in the app's Dockerfile.

  This exists because a caller **cannot** do it: `jobs.<id>.environment` is not allowed
  on a job that uses `uses:`, so every expression in that job's `with:` resolves at
  repository and organization scope and an environment variable reads as empty. The
  build job inside the reusable workflow does declare `environment:`, which is the only
  place an environment-scoped build input can be resolved. Resolves the open question
  in `docs/migration-backlog.md` item 2.

  Existing repos are unaffected: a manifest with no `buildArgs` block renders nothing,
  and the `build-args` workflow input keeps working — it is now merged with the
  manifest's rather than replaced by it. On a name set in both the manifest wins, with
  a warning, because only the manifest is resolved at the target environment's scope.

### Changed

- The `build-args` workflow input's description now states that it is evaluated in the
  caller, so it is only useful for a value identical in every environment.
- `scripts/validate-manifest.py` validates the new block: a name in both lists is an
  error, and so is a name in `env.secrets` passed as a build arg *variable*. It also
  reads the app's Dockerfile and **errors** when a declared `buildArgs.secrets` entry
  has no matching `--mount=type=secret,id=<NAME>` — without the mount the value is
  rendered, mounted and silently ignored, and the build succeeds without it. It warns
  when the Dockerfile has no `# syntax=` directive, which secret mounts require.

### Documentation

- `docs/env-contract.md` gains a **Build-time vs deploy-time** section.
- `docs/multi-environment.md` corrected: the OVH registry is **not** shared between
  stage-k8s and prod-k8s on this estate. Each cluster has its own, which means
  build-once-promote to `prodtest` needs a cross-registry mirror that does not exist
  yet. The previous text claimed a single shared registry and only the path prefix
  separating environments.

## [v1.6.1] — 2026-08-21

### Documentation

- `docs/onboarding.md` named the required status check **Branch guard**, which is the
  workflow's `name:` and not a check that exists. A reusable workflow reports as
  `<caller job> / <inner job name>`, so the string to require is `guard / Branch naming`.
  Added a table of the real check names for every caller, and a note that renaming a
  caller job renames its checks — which silently blocks every PR if that check was
  required.

### Changed

- Third-party action pins moved by Dependabot. All three are **major** bumps, so read
  this one before moving a pin:
  - `actions/checkout` 4 → 7, in `ci.yml`, `deploy.yml`, `release.yml` and `rollback.yml`
  - `docker/login-action` 3 → 4, in the deploy workflow's build job
  - `docker/setup-buildx-action` 3 → 4, in the same job

  No input to any reusable workflow, composite action or chart value changed, which is
  why this is a patch release.

  What CI does **not** cover: the deploy path itself. `ci.yml` lints, renders the chart
  and exercises the renderer, but never checks out on a `pull_request` deploy, never logs
  in to a registry and never builds an image — so all three of these actions are used in
  places the release gate does not reach. Two behaviours worth confirming against a real
  run before a production deploy relies on them:
  - `deploy.yml` passes `ref: ${{ github.event.pull_request.head.sha }}` to checkout,
    which is **empty on push events** and depends on empty meaning "the default ref".
  - `docker/login-action` still takes `registry` / `username` / `password` unchanged;
    that is the OVH registry auth in the build job.

  Roll back with the Release workflow — dispatch it with `tag: v1.6.0` — if a deploy
  fails at checkout, at `docker login` or at buildx setup.

## [v1.6.0] — 2026-08-21

### Added

- **Four-environment flow.** `dev`, `staging`, `prodtest` and `production` across two
  long-lived branches: a PR into `staging` deploys dev, the merge deploys staging and
  prodtest in parallel from one build, and a merge into `main` deploys production. One
  new template, `caller-deploy-4env.yml` — the branch guard and rollback callers are
  flow-agnostic and were widened in place rather than forked.
  See [docs/multi-environment.md](docs/multi-environment.md).
- **Namespace and registry-prefix conventions.** `dev` → `dev-<app>`, `prodtest` →
  `prodtest-<app>`; `staging` and `production` stay `<app>`. `prodtest` reads its image
  from `staging/<app>` because it promotes the artifact staging built rather than
  rebuilding the commit. An explicit `environments.<env>.namespace` is still taken
  verbatim and no prefix is added on top.
- **`enabled` on/off switch.** `environments.<env>.enabled: false` in the deploy manifest
  skips that environment's build and deploy with a green tick and a log line, instead of
  failing on credentials that were never created. A top-level `enabled: false` pauses
  every environment at once.
- `actions/resolve-target` — one implementation of "which app, which namespace, which
  registry repository, is it on", shared by the build job, the deploy job and the rollback
  workflow. It replaces three hand-rolled copies of that logic.
- `build-only` input on `deploy.yml`, plus an `image-tag` workflow output. One build job
  can now feed several deploy jobs, which is what lets staging and prodtest deploy the
  same bytes concurrently.
- `environment-branches` input on `deploy.yml`, defaulting to `production=main`. A run
  whose ref is not that branch cannot deploy that environment and fails in the
  credential-free `resolve` job with a message saying which branch to use. Closes the
  `workflow_dispatch` gap where the branch and the environment are two unconnected
  dropdowns. Only production is pinned by default: `staging` and `dev` deploy from PR
  head branches on one flow or the other, and `prodtest` is left open so a feature branch
  can be rehearsed on prod-k8s before it is merged. This is a second lock beside the
  GitHub deployment-branch policy, not a replacement: the policy is settings state that a
  new repo can silently lack.
- `allowed-into-staging` input on `branch-guard.yml`, and `staging` recognised as a PR
  base. Previously a PR into `staging` matched no policy and the guard skipped.
- `templates/caller-branch-guard.yml` now triggers on PRs into `staging` as well as
  `main`, and `templates/caller-rollback.yml` lists all four environments. Both are
  inert additions on the two-environment flow — no `staging` branch means no PR targets
  it — so one template serves both flows. Re-copy them to pick the change up; an
  existing copy keeps working unchanged.

### Documentation

- `docs/env-contract.md` gains a "which scope" section: what belongs at organization,
  repository and environment scope, why a key that differs anywhere must be set at
  environment scope everywhere, and why an app key at repository scope is the most likely
  route to prodtest writing to the production database.

### Fixed

- `release.yml` on `workflow_dispatch` checked out the branch selected in the Actions UI
  rather than the tag named in the `tag` input, so re-pointing `v1` at an older release —
  the rollback path — moved `v1` to that branch's HEAD instead, while the log announced
  the older tag. It now checks out the requested tag and names the target commit
  explicitly. A tag push was always correct and is unaffected.

### Changed

- The "resolved to an environment name" check moved from the deploy job into the new
  credential-free `resolve` job, so a caller that forgets `environment:` on a
  `pull_request` trigger fails in seconds rather than after a full image build.
- The deploy job is now handed the registry repository the build job pushed to, rather
  than recomputing it. Same resolver either way; passing it removes the question.
- `environments:` may override a fifth key, `enabled`. The other four are unchanged.

Nothing here changes behaviour for a repo on the two-environment flow: `staging` and
`production` resolve to the same namespaces and the same registry prefixes as before, and
`caller-deploy.yml`, `caller-deploy-no-staging.yml`, `caller-branch-guard.yml` and
`caller-rollback.yml` are untouched.

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
