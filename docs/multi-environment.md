# Branches, environments and namespaces

## The resolution chain

```
branch (or workflow_dispatch input)
  -> caller maps it to an environment name
     -> GitHub Environment supplies vars + secrets (and its protection rules apply)
        -> environment name is the registry repository prefix
           -> manifest maps it to app + namespace
```

The environment name is the hinge. It is simultaneously the GitHub Environment, the
registry path segment, and the key into the manifest's `environments:` block.

## The gitflow

```
feat/*  ──PR──>  develop  ──PR──>  main
fix/*   ──PR──>  develop
hotfix/* ─────────PR─────────────> main   (then cherry-pick to develop)
```

| Event | Deploys to | Why |
|---|---|---|
| PR `feat/*` → `develop` | **staging** | QA tests the branch before it merges, so only tested code reaches `develop` |
| push `develop` | nothing | `develop` is an integration branch; what QA approved is the commit already deployed from the PR |
| PR → `main` | nothing | branch guard only |
| push `main` | **production** | |

`main` accepts only `develop` (the normal path) and `hotfix/*` (urgent). A hotfix goes
straight to `main`, deploys production on merge, and is cherry-picked back to `develop`
afterwards — it gets no separate domain and no separate flow.

Use `templates/caller-deploy-gitflow.yml` plus `templates/caller-branch-guard.yml`.

A repo with no staging cluster entry uses `templates/caller-deploy-1branch.yml` instead:
push to `main` deploys production, PRs deploy nothing, and the branch guard still runs.
That is `omnicasa-tools` today.

### Staging is one shared release

Every open PR deploys over the same release at the same hostname. Two PRs in flight
means the last push wins, so check the run's commit before testing. This was a
deliberate trade — per-PR preview environments need wildcard DNS, a wildcard
certificate, and a teardown job on PR close.

### Pull-request specifics that bite

- **`environment` must be passed explicitly** for PR triggers. On a `pull_request`,
  `github.ref_name` is `42/merge` and `github.head_ref` is `feat/login` — neither is an
  environment name. The workflow fails fast with a clear message rather than running
  with no environment and no secrets.
- **The image is tagged with the PR head commit**, not `github.sha`, which on a PR is
  the merge commit — synthetic, on no branch, and it changes whenever the base moves.
  Checkout uses the same head commit, so the image and its tag describe the same code.
- **Fork PRs get no secrets.** GitHub withholds them, so the deploy fails closed at the
  `required:` check. Fine for an org where all work happens on branches; worth knowing
  if you ever accept outside contributions.
- **The staging environment's deployment-branch policy must allow the PR head
  branches.** Set staging to "All branches" — it is the low-stakes environment, and the
  branch guard already restricts what can open a PR. Keep `production` restricted to
  `main`.

## Why the expression is repeated inline

`jobs.<id>.environment` and `jobs.<id>.concurrency` can read `inputs`, `vars` and `github`
— but **not** `env`. Factoring the expression into a workflow-level `env` variable and
referencing it there does not error; it silently evaluates to nothing, and the job runs
with no environment at all, so no environment secrets resolve.

That is why the same expression appears verbatim in several places in
`.github/workflows/deploy.yml` rather than being defined once.

## Concurrency

Both the deploy and rollback workflows use:

```
group: ${{ github.repository }}-${{ environment }}
cancel-in-progress: false
```

Two things matter here:

- **The repository is part of the group.** A bare `group: production` — which is what the
  per-repo workflows used — would serialise every Omnicasa repo's production deploy
  against every other one now that they share a workflow. That looks like a hang.
- **The groups are identical between deploy and rollback**, so the two can never touch one
  release at the same time. If you edit one, edit the other.

`cancel-in-progress: false` is not a preference. A cancelled run leaves the Helm release in
`pending-upgrade` *and* leaves the runner's IP on the OVH allowlist.

## Separate clusters

Staging and production are separate clusters. Consequences:

- `KUBECONFIG_BASE64` is an **environment** secret, never a repo-level one. A repo-level
  kubeconfig would point both environments at the same cluster.
- `OVH_KUBERNETES_ID` is likewise per environment.
- The registry is **shared**; environments are separated only by the path prefix
  (`<registry>/staging/<app>` vs `<registry>/production/<app>`). So `OVH_REGISTRY`,
  `OVH_REGISTRY_USERNAME` and `OVH_REGISTRY_PASSWORD` can be org- or repo-level.

## Environments that deviate

When one environment needs a different app name, namespace or registry prefix, use the
manifest's `environments:` block rather than a second workflow file:

```yaml
environments:
  development:
    app: my-app-dev
    namespace: my-app-dev
    repositoryPrefix: staging
```

Only `app`, `namespace`, `repositoryPrefix` and `tlsSecretName` may be overridden. The
narrowness is the point: an environment that could redefine its own env-var allowlist is
exactly how `omnicasa-payload`'s duplicated dev workflow ended up deploying with the wrong
environment's secrets.

`render-values.py` emits a `::notice::` naming the effective app, namespace and repository
on every run, so the log always states what it actually did.

## Protection rules worth setting

On the `production` environment:

- required reviewers (at least one)
- deployment branches restricted to `main`

On `staging`: nothing, or a short wait timer.

These are GitHub Environment settings, not workflow configuration — which is another
reason the deploy job declares `environment:` rather than passing credentials in as inputs.
