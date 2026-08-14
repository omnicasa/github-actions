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

## Current convention

Branch names have **not** been aligned across the estate yet. Until they are, the mapping
lives in one expression in each caller.

| Repo shape | Branches | Caller expression |
|---|---|---|
| Single branch | `main` | `environment: production` |
| Two branches | `main`, `develop` | `${{ github.ref_name == 'main' && 'production' \|\| 'staging' }}` |
| Already aligned | `staging`, `production` | `${{ github.ref_name }}` |

Use `templates/caller-deploy-1branch.yml` or `-2branch.yml`.

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
