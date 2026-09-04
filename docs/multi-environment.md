# Branches, environments and namespaces

## The resolution chain

```
branch (or workflow_dispatch input)
  -> caller maps it to an environment name
     -> GitHub Environment supplies vars + secrets (and its protection rules apply)
        -> environment name picks the namespace and registry prefix by convention
           -> manifest's `environments:` block overrides either, or switches it off
```

The environment name is the hinge. It is simultaneously the GitHub Environment, the
registry path segment, and the key into the manifest's `environments:` block.

`actions/resolve-target/target.py` is the one implementation of that chain. The build
job, the deploy job and the rollback workflow all call it, because a build that pushes
to `staging/app` while the deploy pulls `prodtest/app` fails in the cluster, minutes
later, as `ImagePullBackOff`.

## Two supported flows

Both are current. A repo picks one and copies the matching templates; nothing about the
reusable workflows differs between them.

| | four-environment | two-environment |
|---|---|---|
| Long-lived branches | `staging`, `main` | `main` |
| Environments | dev, staging, prodtest, production | staging, production |
| Deploy template | `caller-deploy-4env.yml` | `caller-deploy.yml` |
| Guard template | `caller-branch-guard.yml` | same file |
| Rollback template | `caller-rollback.yml` | same file |

The deploy caller is the only one that forks, because it is the only one where the two
flows disagree: a PR into `main` deploys staging on one and nothing on the other. The
guard triggers on both bases and the rollback lists all four environments — additions
that are inert wherever the branch or the environment does not exist.

A repo with no staging cluster entry at all uses `templates/caller-deploy-no-staging.yml`:
push to `main` deploys production, PRs deploy nothing, and the branch guard still runs.

A repo that wants two environments but a branch trigger for each — rather than deploying
staging from a pull request — has no template; see [branch-triggered
staging](#branch-triggered-staging) below.

> `templates/caller-deploy-gitflow.yml`, `-1branch.yml` and `-2branch.yml` are
> superseded and kept only for reference. They target a `develop` branch or an older
> naming; do not start a new repo from them.

## The four-environment flow

```
feat/*      ──PR──>  staging  ───merge───>  ──PR──>  main  ───merge───>
fix/*                                       (same branch)
perf/*        deploys DEV      deploys       deploys        deploys
refactor/*                     STAGING       nothing        PRODUCTION
chore/*                        + PRODTEST
ci/*
hotfix/*
```

| Event | Deploys to | Why |
|---|---|---|
| PR `<kind>/*` → `staging` | **dev** | the change runs somewhere before it is anywhere shared |
| push `staging` | **staging** and **prodtest** | integration on stage-k8s, and the same artifact rehearsed on prod-k8s |
| PR `<kind>/*` → `main` | nothing | this commit has already run in three environments |
| push `main` | **production** | the merge *is* the release |

A feature branch is merged twice, into `staging` and then into `main`. The promotion PR
deploys nothing on purpose: re-deploying it to dev would clobber whatever the next
feature is testing there, and prodtest already rehearsed it.

The branch kind is a label, not a pipeline. `hotfix/*` takes exactly the same route as
`feat/*` — same dev deploy, same staging and prodtest, same review, same production
release. It only says why the change is urgent, which is what you want in the log six
months later. Nothing is cherry-picked anywhere.

### Where each environment lives

| Environment | Cluster | Namespace | Registry repository |
|---|---|---|---|
| dev | stage-k8s | `dev-<app>` | `<registry>/dev/<app>:<sha>` |
| staging | stage-k8s | `<app>` | `<registry>/staging/<app>:<sha>` |
| prodtest | prod-k8s | `prodtest-<app>` | `<registry>/staging/<app>:<sha>` |
| production | prod-k8s | `<app>` | `<registry>/production/<app>:<sha>` |

`dev` and `prodtest` are second environments on a cluster that already hosts the app, so
they get a prefixed namespace. No app repo writes any of this down: the manifest carries
one `namespace: <app>` and the prefix is applied per environment. An explicit
`environments.<env>.namespace` is taken **verbatim** — the prefix is never stacked on a
name someone chose, because that name is what the per-namespace RBAC and the pull secret
were created against.

All four namespaces still have to exist, be RBAC-registered and have the pull secret
synced, per [onboarding.md](onboarding.md). The convention removes the *configuration*,
not the prerequisite.

### prodtest promotes, it does not rebuild

`prodtest` is the one environment whose registry prefix is not its own name. Push to
`staging` runs **one** build, pushed to `staging/<app>:<sha>`; both the staging and
prodtest deploys then promote that exact image, in parallel.

Two things follow, and both are the point:

- **prod-k8s runs the bytes stage-k8s was tested on.** Two builds of one commit are only
  presumed identical; one build is identical.
- **They deploy concurrently**, because neither waits on the other's rollout. The caller
  expresses this with a `build-only: true` job whose `image-tag` output both deploy jobs
  consume.

The cost: the pull secret in `prodtest-<app>` must be able to read the `staging/` path —
and that path is in the **stage-k8s registry**, which is a different registry from the
one prod-k8s pulls from. So promotion needs either a pull secret in `prodtest-<app>`
holding stage-registry credentials, or the image mirrored across registries after the
build. **Neither exists on this estate today**, which is why no repo has prodtest
enabled yet.

A second thing can rule promotion out independently of the registry: an image built with
`buildArgs` carries one environment's configuration baked in, so promoting it puts
staging's configuration on prod-k8s. An app in that position should keep
`environments.prodtest.enabled: false` until the value moves to runtime. See
[env-contract.md](env-contract.md#build-time-vs-deploy-time).

`production` does **not** promote. The commit on `main` is a different commit from the
one on `staging` — different merge history, different SHA — so there is no staging
artifact that corresponds to it. It builds under its own prefix.

### Keeping the two branches from drifting

Each feature branch is merged into both `staging` and `main`, so the two converge on
content. They will never converge on SHA, and nothing in this pipeline needs them to.
What does matter: a change merged to `main` but never to `staging` is invisible to every
future integration test. Merge into `staging` first, always.

### Turning an environment off

```yaml
# .github/deploy-manifest.yml
environments:
  prodtest:
    enabled: false
```

The environment's jobs skip with a green tick and a log line saying why, rather than
failing on credentials that were never created. Use it for an app that has no prodtest
namespace yet, or one that should never reach dev. Leave the key out and the environment
is on.

A top-level `enabled: false` pauses every environment at once — a one-line way to stop a
repo deploying during an incident without deleting the workflow.

The switch is in the manifest and not the caller workflow so that every repo's caller is
the same file. "Which environments does this app have?" is an app fact, and app facts
live in the manifest where they are reviewable in the app's own PR.

## The two-environment flow

```
feat/*      ──PR──>  main        PR deploys to STAGING, merge deploys PRODUCTION
fix/*       ──PR──>  main
...
```

| Event | Deploys to | Why |
|---|---|---|
| PR `<kind>/*` → `main` | **staging** | the change is tested on stage-k8s before it is anywhere shared |
| push `main` | **production** | the merge *is* the release |

`main` is the only long-lived branch, and it is production. Namespaces are `<app>` on
both clusters, and the registry prefix is the environment name — the conventions above
leave `staging` and `production` exactly as they were, which is what lets a repo stay on
this flow with no change at all.

### Branch-triggered staging

`support-ai-dashboard` runs a variant: two environments, but each deploys from its own
long-lived branch, and a pull request deploys nothing.

| Event | Deploys to |
|---|---|
| PR `<kind>/*` → `staging` | nothing |
| push `staging` | **staging** |
| PR `staging` → `main` | nothing |
| push `main` | **production** |

Pick it when staging should change on a merge rather than on every push to an open PR.
The cost is that a change reaches staging only once it is merged there, so the PR itself
runs nowhere.

There is no template. Copy `caller-deploy-4env.yml` and delete the `dev` and `prodtest`
jobs; the `build-only` job goes with them, since it exists solely to hand one artifact to
staging and prodtest at once. Two things then have to change, and neither is optional:

- **Pin both environments**, because the default pins only production and this flow's
  staging *does* have a fixed branch: `environment-branches: production=main,staging=staging`.
- **Let `staging` open a PR into `main`**, or every promotion PR fails the guard with
  `'staging' cannot target 'main'`. In the branch-guard caller:
  `allowed-into-main: "staging"`. PRs into `staging` keep the default prefixes.

Set the staging Environment's deployment branches to `staging`, not All branches — the
row in [Protection rules](#protection-rules-worth-setting) below assumes the PR-triggered
two-environment flow.

## Shared releases

dev and staging are **one release each**. Every open PR deploys over the same dev release
at the same hostname; two PRs in flight means the last push wins, so check the run's
commit before testing. This was a deliberate trade — per-PR preview environments need
wildcard DNS, a wildcard certificate, and a teardown job on PR close.

## Pull-request specifics that bite

- **`environment` must be passed explicitly** for PR triggers. On a `pull_request`,
  `github.ref_name` is `42/merge` and `github.head_ref` is `feat/login` — neither is an
  environment name. The workflow fails fast, in the credential-free `resolve` job before
  any image is built, rather than running with no environment and no secrets.
- **The image is tagged with the PR head commit**, not `github.sha`, which on a PR is
  the merge commit — synthetic, on no branch, and it changes whenever the base moves.
  Checkout uses the same head commit, so the image and its tag describe the same code.
- **Fork PRs get no secrets.** GitHub withholds them, so the deploy fails closed at the
  `required:` check. Fine for an org where all work happens on branches; worth knowing
  if you ever accept outside contributions.
- **The dev environment's deployment-branch policy must allow the PR head branches.**
  Set dev to "All branches" — it is the low-stakes environment, and the branch guard
  already restricts what can open a PR. Keep `production` restricted to `main`.

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

Three things matter here:

- **The repository is part of the group.** A bare `group: production` — which is what the
  per-repo workflows used — would serialise every Omnicasa repo's production deploy
  against every other one now that they share a workflow. That looks like a hang.
- **The environment is part of the group**, which is what lets staging and prodtest
  deploy at the same time off one push. They are different releases on different
  clusters; serialising them would buy nothing.
- **The groups are identical between deploy and rollback**, so the two can never touch one
  release at the same time. If you edit one, edit the other.

`cancel-in-progress: false` is not a preference. A cancelled run leaves the Helm release in
`pending-upgrade` *and* leaves the runner's IP on the OVH allowlist.

## Separate clusters

stage-k8s and prod-k8s are separate clusters, and each hosts two environments.
Consequences:

- `KUBECONFIG_BASE64` is an **environment** secret, never a repo-level one. It is the
  per-namespace ServiceAccount's kubeconfig, so `dev` and `staging` need different ones
  even though they point at the same cluster — and so do `prodtest` and `production`.
  Four environments means four kubeconfigs.
- `OVH_KUBERNETES_ID` is per **cluster**, so dev and staging share a value, and prodtest
  and production share the other. It is still set per environment; the two just match.
- The registry is **not** shared. stage-k8s and prod-k8s each have their own OVH
  registry, so `OVH_REGISTRY` — and the username and password with it — is an
  **environment**-scoped value like `KUBECONFIG_BASE64`, not an org- or repo-level one.
  dev and staging carry one registry's values, prodtest and production the other.
  Within a registry, environments are still separated only by the path prefix.

## Environments that deviate

When one environment needs a different app name, namespace or registry prefix, use the
manifest's `environments:` block rather than a second workflow file:

```yaml
environments:
  dev:
    namespace: my-app-sandbox   # verbatim: no dev- prefix is added on top
  prodtest:
    enabled: false
```

Only `app`, `namespace`, `repositoryPrefix`, `tlsSecretName` and `enabled` may be
overridden. The narrowness is the point: an environment that could redefine its own
env-var allowlist is exactly how `omnicasa-payload`'s duplicated dev workflow ended up
deploying with the wrong environment's secrets.

`resolve-target` and `render-values.py` each emit a `::notice::` naming the effective app,
namespace and repository on every run, so the log always states what it actually did.

## Who can deploy production, and from where

`workflow_dispatch` asks for a branch and an environment as two independent choices.
Nothing in that form connects them, so picking `production` while standing on a feature
branch is one wrong dropdown away. Three layers stop it, and they answer different
questions.

### From where — the `environment-branches` input

The reusable deploy workflow refuses, in the credential-free `resolve` job before
anything is built, to deploy an environment from a ref that is not its branch:

```
environment-branches: production=main     # the default
```

An environment absent from the list is unrestricted. Only `production` is pinned by
default:

- `staging` deploys from the `staging` branch on the four-environment flow but from
  whatever branch opened the PR on the two-environment one — no single default fits both.
- `dev` deploys from PR head branches, which have no fixed name.
- `prodtest` is left open on purpose. It is the rehearsal environment, and being able to
  dispatch a feature branch onto prod-k8s *before* merging it is what makes the rehearsal
  worth having. It runs on the production cluster but serves no users.

Override the input to pin any of them — `production=main,staging=staging` for a
four-environment repo that wants staging locked down too — or set it to an empty string
to turn the check off entirely.

This exists because the GitHub-side rule below is settings state — invisible in review,
and silently absent the day someone creates a new repo and forgets. A default in the
shared workflow holds everywhere at once. It is a second lock, not a replacement.

### From where — the deployment branch policy

On the `production` environment, set **deployment branches** to `main`. GitHub then
refuses to start any job declaring `environment: production` from another ref, including
the build.

Leave `prodtest` on **All branches** so a feature branch can be dispatched onto it. That
is a deliberate trade: prodtest sits on prod-k8s, so unreviewed code can reach the
production cluster on a manual dispatch by anyone with repo write. What limits the blast
radius is that prodtest is a separate namespace with its own credentials, its own
hostname and no users — and that `production` remains pinned to `main` behind required
reviewers.

Keep this even with the input above. Only this one cannot be edited by someone with
write access to the workflow file, and only this one also governs approval.

### Who — required reviewers

`workflow_dispatch` needs repository **write** access, and that cannot be narrowed per
workflow. There is no "only these people may dispatch" setting.

The control that does exist is **required reviewers** on the `production` environment.
Every production deploy — dispatched or merged — then pauses for approval by a named
person or team, and the job holds with no credentials issued until someone approves. Set
the reviewer to the team allowed to release, and tick **prevent self-review** so the
person who started the run is not the person who waves it through.

That is also the answer to the original worry: a wrong-branch dispatch that somehow got
past both branch checks still stops dead in front of a human who can see the ref.

## Protection rules worth setting

| Environment | Deployment branches | Reviewers |
|---|---|---|
| production | `main` | required, prevent self-review |
| prodtest | All branches — dispatched on demand | none |
| staging | `staging` (four-env, branch-triggered) / All branches (PR-triggered two-env) | none |
| dev | All branches — PR head names vary | none |

`dev` and PR-triggered `staging` are "All branches" because PR head branch names vary
and the branch guard already restricts what can open a PR. A staging that deploys from a
branch is pinned to it instead. `prodtest` is "All branches"
for a different reason: so a branch can be rehearsed on prod-k8s before it is merged.

One coupling to know about: the build job that feeds a dispatched prodtest deploy
declares `environment: staging`, because that is the environment the image is pushed
under. So dispatching prodtest from a feature branch also needs `staging` on All
branches. Pinning `staging` to its own branch and still dispatching prodtest freely means
changing the caller's build job to `environment: prodtest` — the image path is identical
either way, since the convention maps prodtest to the `staging` prefix.

These are GitHub Environment settings, not workflow configuration — which is another
reason the deploy job declares `environment:` rather than passing credentials in as inputs.
