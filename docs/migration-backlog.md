# Migration backlog

`omnicasa-tools` is migrated. The other six are **not in scope** until someone decides
otherwise — this file exists so that decision does not require re-deriving the analysis.

Ordered easiest first. The order is deliberate: consent-app second because its diff
should be nearly empty, which tests the abstraction rather than the app.

## 1. `Omnicasa-Oauth2-Consent-App`

The reference implementation, so migrating it mostly means deleting code. Its `helm diff`
should be close to empty — which is exactly why it goes first: a large diff here means
the shared pipeline has drifted from the thing it was extracted from.

- Carries `hostAliases` with nine hard-coded internal IPs (`sql-stage.omnicasadev.local`,
  `graylog.*`, `systemsql.omnicasa.local`, `hosting7/9/10/18/19.omnicasa.local`). Move
  to `deploy/values.yaml` verbatim.
- `chart/docker-registry.secret.yaml` renders nothing and is marked safe to delete.
- Container port is 8888, not 3000. Probes currently hit `/`.
- Build flavour is **gulp**, not Dockerfile: `gulp clean && gulp pushDocker --tag <sha>`,
  needs `NODE_OPTIONS=--no-experimental-strip-types` and `npm install --force` (no
  committed lockfile). The shared `deploy.yml` currently only has a Dockerfile build
  job — **this repo needs a `build: gulp` path added first.**

## 2. `omnicasa-email-editor`

- Retire the old key names; move the pull secret from in-workflow creation to pre-created.
- Needs build-time env baked into the image (`NEXT_PUBLIC_SUPABASE_URL`,
  `NEXT_PUBLIC_*`, `SENTRY_AUTH_TOKEN`, `POSTHOG_*`) *and* the same values again at
  deploy time. The workflow's `build-args` input covers the non-secret half.
  **Open question:** `SENTRY_AUTH_TOKEN` is a credential and build args are readable by
  anyone who can pull the image. Decide whether to use a buildx secret mount instead.
- Domain is `emaileditor.${{ vars.BASE_DOMAIN }}` — the shared pipeline expects one
  `domainVar` holding the full hostname, so a new `APP_DOMAIN` var is needed per environment.

## 3. `omnicasa-webhook`

- Same key retirement and pull-secret change.
- Sets `hostAliases` as roughly twenty `--set hostAliases[N].ip=...` flags. All of it
  collapses into `deploy/values.yaml`.
- `secrets.FULL_DOMAIN` is a hostname stored as a secret, which makes a failed deploy
  undiagnosable (the log shows `***` where the host should be). Move it to a var as
  `APP_DOMAIN`.

## 4. `omnicasa-api-access`

- `KUBE_CONFIG` → `KUBECONFIG_BASE64`.
- Chart moves from `k8s/helm/` to the unified chart. Check its selector labels the same
  way the tools migration did — that is the one thing that fails irreversibly.
- Uses `vars.APIV3_CUSTOMER_DB_HOST_MAP` alongside a secret of nearly the same name;
  confirm which the app actually reads before splitting them.

## 5. `omnicasa-payload`

Collapse `ci.yaml` and `ci_dev.yaml` into one caller plus an `environments:` block.

**Two real bugs to fix while migrating, not after:**

- `ci_dev.yaml`'s deploy job declares `environment: production` while its concurrency
  group is `staging`. The development app deploys using production-environment secrets.
- Both workflows inject `OVH_*` and `KUBECONFIG` into `config.env.secrets`, i.e. the
  cluster credentials are written into the application's own Secret and mounted into
  its pods.

Also: `ci.yaml` hardcodes the TLS secret as `omnicasa-payload-ssl-secret` while
`ci_dev.yaml` uses `${APP_NAME}-tls-certificates`. Set `tlsSecretName` per environment
or cert-manager will reissue.

## 6. `omnicasa-peppol`

Last, and the largest.

- Raw manifests under `k8s/manifest/`, not a chart at all.
- Needs `ingress.managed: false` (multiple hosts) and `cronJobs[]` (scheduled tasks).
- **Docs contradict reality.** `GITHUB_ENVIRONMENTS_SETUP.md` and `DUAL_CLUSTER_SETUP.md`
  describe `develop` → staging and `main` → production across two separate clusters,
  with namespaces `omnicasa-peppol-staging` and `omnicasa-peppol-prod`. The actual
  workflow sends `main`, `develop` **and** `develop-ci` to production in a single
  namespace `omnicasa-peppol`, and `deploy-stg.yaml.old` is entirely commented out.
  **Decide which is true before migrating** — this is a question for a person, not a
  thing to infer from the files.
- Its inline allowlist code is the only copy that checks the HTTP status. That check is
  now the shared behaviour.

## Cross-cutting

### RBAC drift — do this before any estate-wide rollout

`terraform/infra-helm/github-actions-rbac-per-ns/values/prod-k8s/github-actions-rbac-per-ns.yaml`
lists **two** namespaces (`omnicasa-peppol`, `omnicasa-prod`) while seven apps deploy.
Most repos are therefore using a broader kubeconfig than the per-namespace design intends.

Centralising CI without fixing this centralises a privilege problem. Fix:

1. Add every deploying namespace to the prod and stage values files.
2. `helmfile apply` to mint the per-namespace ServiceAccounts.
3. Mint a token per namespace, build a kubeconfig, `base64 -w0`, set as that repo's
   environment `KUBECONFIG_BASE64`.
4. Verify with `kubectl auth can-i --list -n <ns>` using the new kubeconfig.

The unified pipeline makes step 3 a one-secret change per repo.

### Branch-name alignment

Deferred by decision. Today: `main` → production, `develop` → staging, expressed in the
caller's one mapping expression. Repos already on literal `staging`/`production` branches
simplify to `environment: ${{ github.ref_name }}`. Peppol's `develop-ci` disappears.

### Bootstrap automation

`sync-registry-to-k8s.sh` and the `github-actions-rbac-per-ns` helmfile already exist but
are run by hand, which is why the namespace list drifted. An `onboard-app.sh` wrapping
both — plus ServiceAccount token minting and `gh variable set` / `gh secret set` — would
make onboarding one PR and one command.

### A gulp build path in the shared workflow

Required before consent-app can migrate. See item 1.
