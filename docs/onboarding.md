# Onboarding an app repo

Four files in the app repo, five prerequisites outside it. Budget an hour for the first
one, fifteen minutes after that.

## What the app repo ends up with

```
.github/workflows/deploy.yml        # 20-60 lines, from a templates/caller-deploy*.yml
.github/workflows/rollback.yml      # ~22 lines,   from templates/caller-rollback*.yml
.github/workflows/branch-guard.yml  # ~3 lines,    from templates/caller-branch-guard*.yml
.github/deploy-manifest.yml         # what the app needs      (templates/deploy-manifest.yml)
deploy/values.yaml                  # how the chart is shaped (templates/values.yaml)
deploy/values.<env>.yaml            # optional, per environment
Dockerfile
.dockerignore
```

And these are **deleted**: `k8s/helm/`, `k8s/manifest/`, `chart/`, `.github/scripts/`, and
any duplicate per-environment workflow.

## Pick a flow first

| | four-environment | two-environment |
|---|---|---|
| Branches | `staging`, `main` | `main` |
| Environments | dev, staging, prodtest, production | staging, production |
| Namespaces | `dev-<app>`, `<app>`, `prodtest-<app>`, `<app>` | `<app>` on both clusters |

Everything below is written for four; a two-environment repo does the same work for two.
[multi-environment.md](multi-environment.md) has the comparison.

## Prerequisites outside the repo

CI deliberately cannot do these — it holds no namespace-write RBAC, which is what keeps a
compromised workflow from creating resources anywhere in the cluster. The deploy fails
fast with the exact remediation command if either cluster prerequisite is missing.

Steps 1–3 are **per namespace**, so on the four-environment flow they are done four
times: `dev-<app>` and `<app>` on stage-k8s, `prodtest-<app>` and `<app>` on prod-k8s.
The namespace names come from the convention, not from anything you write in the repo.

1. **Namespace exists** on the target cluster.
2. **Namespace is registered for RBAC** in
   `terraform/infra-helm/github-actions-rbac-per-ns/values/{prod-k8s,stage-k8s}/github-actions-rbac-per-ns.yaml`,
   then `helmfile apply`. This mints the per-namespace ServiceAccount, Role and RoleBinding.
   Note this list has drifted before — see [migration-backlog.md](migration-backlog.md).
3. **Registry pull secret synced** into the namespace:
   ```
   terraform/infra-terraform/scripts/sync-registry-to-k8s.sh \
     --env production --user all --namespace <namespace>
   ```
   The secret in `prodtest-<app>` must be able to read the **`staging/`** registry path:
   prodtest promotes the image staging built rather than rebuilding it.
4. **GitHub Environment created** — one per environment — with the ten platform keys plus
   the app's own keys. See [env-contract.md](env-contract.md).
5. **`KUBECONFIG_BASE64` is the per-namespace ServiceAccount kubeconfig**, one per
   *environment*, not per cluster. dev and staging sit on the same cluster in different
   namespaces, so they still need different kubeconfigs; likewise prodtest and production.

Not ready for all four? Ship with the rest turned off — `environments.<env>.enabled: false`
in the manifest — and turn each on as its namespace lands. The jobs skip green until then.

## Steps

### 1. Write the manifest first

This is the design step; everything else follows from it. Copy
`templates/deploy-manifest.yml` and fill it in.

The decision that matters is the variable/secret split. Get it right in both directions:
a credential as a variable leaks it into the log, and a hostname as a secret makes a
failed deploy undiagnosable because the log shows `***` where you need to read a value.

If the app needs anything while the image is *built* — a bundler that inlines a public
env prefix, a credential a build step authenticates with — that goes in the same file
under `buildArgs:`, not in the caller. A caller cannot resolve an environment-scoped
value at all; see [env-contract.md](env-contract.md#build-time-vs-deploy-time). Prefer
runtime configuration where the app allows it: a baked value pins the image to one
environment and rules out build-once-promote.

Validate before going further:

```bash
python3 scripts/validate-manifest.py .github/deploy-manifest.yml
```

### 2. Write deploy/values.yaml

Copy `templates/values.yaml`. If the repo already has a chart, copy its values across
**verbatim** — the same numbers, so the first diff shows only the intended changes.

Set the real `service.containerPort` and real probe paths. `helm upgrade --atomic --wait`
judges success by pod readiness, so a probe that only proves the process is up makes
`--atomic` useless, and a probe against the wrong port makes every deploy fail.

### 3. Add the caller workflows

Three files, all from `templates/`, all tracking `@v1`:

| Template | Copy to | When |
|---|---|---|
| `caller-deploy-4env.yml` | `.github/workflows/deploy.yml` | **four-environment** flow — `staging` and `main` branches |
| `caller-deploy.yml` | `.github/workflows/deploy.yml` | **two-environment** flow — PRs into `main` deploy staging |
| `caller-deploy-no-staging.yml` | `.github/workflows/deploy.yml` | **no staging** — only `main` deploys, PRs deploy nothing |
| `caller-branch-guard.yml` | `.github/workflows/branch-guard.yml` | always, either flow |
| `caller-rollback.yml` | `.github/workflows/rollback.yml` | always, either flow |

Only the deploy caller differs by flow, because only it disagrees about what a PR into
`main` means. Pick one of the three, not several. The guard and the rollback caller are
the same file for everyone — the guard's `staging` trigger is inert in a repo with no
`staging` branch, and the rollback's environment list is a choice menu to trim.

The branch guard goes in either way: the branch naming is enforced whether or not PRs
deploy anything.

Then, in the repo's settings — four-environment flow:

- Make `guard / Branch naming` a required status check on `staging` **and** on `main`.
- Require pull requests on both. The guard only runs on `pull_request`, so a direct push
  bypasses it entirely — and a direct push to `staging` deploys prod-k8s.
- Back both with a GitHub ruleset. A status check explains the rule; a ruleset enforces
  it against anyone who can bypass checks.
- Deployment branches: `dev` → **All branches** (PR head branch names vary, and the guard
  already restricts what can open one); `staging` and `prodtest` → `staging`;
  `production` → `main`, with required reviewers.

Two-environment flow: the same, with `main` as the only guarded base, `staging` set to
All branches, and `production` restricted to `main`.

### What the checks are actually called

A reusable workflow does not report under its own `name:`. Each of its jobs reports as
`<caller job> / <job name inside the reusable workflow>`, and it is the **left** half you
control — it is the job id in your caller, or that job's `name:` if it has one. So the
strings to type into the required-checks box are:

| Caller | Appears in checks as |
|---|---|
| `caller-branch-guard.yml`, job `guard` | `guard / Branch naming` |
| `caller-deploy.yml`, job `deploy` | `deploy / Resolve target`, `deploy / Build & push`, `deploy / Deploy` |
| `caller-deploy-4env.yml`, job `dev` | `dev / Resolve target`, `dev / Build & push`, `dev / Deploy` |
| ...and the same three under `build`, `staging`, `prodtest`, `production` |

Two consequences worth knowing before you configure branch protection:

- **Renaming a caller job renames its checks.** If that check was required, it stops
  reporting and every PR blocks until someone updates the setting. This is why the
  template job ids are left alone even where a prettier name was available — an estate
  where half the repos say `guard` and half say `Branch guard` is the confusion this
  naming is meant to prevent.
- **The `build` job in the four-environment caller shows a skipped `build / Deploy`.** It
  calls the same reusable workflow with `build-only: true`, so the deploy job is
  evaluated and skipped. That is correct, not a failure.

### 4. Prove the migration before it can do damage

If the repo already has a running release, this is not optional.

**Offline, no cluster.** Render the old chart and the new one and compare:

```bash
helm template <app> <old-chart> -n <ns> ... > /tmp/old.yaml
helm template <app> actions/helm-deploy/chart -n <ns> \
  -f actions/helm-deploy/chart/values.yaml \
  -f deploy/values.yaml \
  -f <a stand-in for the rendered release values> > /tmp/new.yaml
```

**Every resource `metadata.name`, every `spec.selector.matchLabels`, and the Service
`spec.selector` must be identical.** `Deployment.spec.selector` is immutable: if it
changes, `helm upgrade` fails outright and no amount of retrying fixes it. A changed
Service or Ingress *name* is worse — helm creates the new one and deletes the old after
the upgrade, so external-dns and cert-manager both react and the TLS secret is orphaned.

New objects are fine. A ConfigMap appearing is expected if the old chart had none.

**Against the live cluster, non-mutating.** Push to a branch whose caller passes
`dry-run: true` and read the `helm diff` in the job summary. Any name or selector change
there is a stop-the-line defect.

### 5. Cut over

Merge. Delete the old chart and old workflow in the same commit — leaving them means two
pipelines can deploy the same app.

If it goes wrong: `--atomic` reverts automatically. If the release is left `pending-*`,
run the Rollback workflow; the next deploy also unsticks it. See
[runbook-rollback.md](runbook-rollback.md).

### 6. Lint

```bash
bash /path/to/github-actions/scripts/check-workflow.sh /path/to/app-repo
```

Fix every FAIL. Read the warnings — several describe failures that only appear after a
successful-looking deploy.

## Gotchas worth knowing before you hit them

- **Probe and annotation maps merge, they do not replace.** If the chart default sets a
  field you care about (`failureThreshold`), state it explicitly in your values file or
  you inherit the default.
- **Never set `nameOverride` or `fullnameOverride`** on an existing release. Both change
  the immutable Deployment selector.
- **`environments:` overrides only five keys** — `app`, `namespace`, `repositoryPrefix`,
  `tlsSecretName`, `enabled`. Deliberately narrow, so a dev environment cannot quietly
  diverge in its env-var allowlist.
- **An explicit `environments.<env>.namespace` is verbatim.** The `dev-`/`prodtest-`
  prefix is not stacked on top of it. That is what you want — but it also means a typo
  there silently deploys a second release into a namespace nobody is watching.
- **`tlsSecretName`** must match the app's existing certificate Secret, or cert-manager
  issues a new one during the cutover.
- **The image tag is always the commit SHA.** If anything still pulls `:latest`, it will
  silently keep running the old image. Grep for it before cutting over.
