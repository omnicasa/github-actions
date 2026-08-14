# Onboarding an app repo

Four files in the app repo, five prerequisites outside it. Budget an hour for the first
one, fifteen minutes after that.

## What the app repo ends up with

```
.github/workflows/deploy.yml      # ~19 lines, from templates/caller-deploy-*.yml
.github/workflows/rollback.yml    # ~22 lines, from templates/caller-rollback.yml
.github/deploy-manifest.yml       # what the app needs      (templates/deploy-manifest.yml)
deploy/values.yaml                # how the chart is shaped (templates/values.yaml)
deploy/values.<env>.yaml          # optional, per environment
Dockerfile
.dockerignore
```

And these are **deleted**: `k8s/helm/`, `k8s/manifest/`, `chart/`, `.github/scripts/`, and
any duplicate per-environment workflow.

## Prerequisites outside the repo

CI deliberately cannot do these — it holds no namespace-write RBAC, which is what keeps a
compromised workflow from creating resources anywhere in the cluster. The deploy fails
fast with the exact remediation command if either cluster prerequisite is missing.

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
4. **GitHub Environment created** with the ten platform keys plus the app's own keys.
   See [env-contract.md](env-contract.md).
5. **`KUBECONFIG_BASE64` is the per-namespace ServiceAccount kubeconfig**, one per
   environment — staging and production are separate clusters, so they cannot share one.

## Steps

### 1. Write the manifest first

This is the design step; everything else follows from it. Copy
`templates/deploy-manifest.yml` and fill it in.

The decision that matters is the variable/secret split. Get it right in both directions:
a credential as a variable leaks it into the log, and a hostname as a secret makes a
failed deploy undiagnosable because the log shows `***` where you need to read a value.

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

From `templates/caller-deploy-1branch.yml` (or `-2branch`) and
`templates/caller-rollback.yml`. Pin an exact `@vX.Y.Z`.

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
- **`environments:` overrides only four keys** — `app`, `namespace`, `repositoryPrefix`,
  `tlsSecretName`. Deliberately narrow, so a dev environment cannot quietly diverge in
  its env-var allowlist.
- **`tlsSecretName`** must match the app's existing certificate Secret, or cert-manager
  issues a new one during the cutover.
- **The image tag is always the commit SHA.** If anything still pulls `:latest`, it will
  silently keep running the old image. Grep for it before cutting over.
