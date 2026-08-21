# The env-key contract

Fixed names, identical in every repo, set per GitHub Environment. Never invent a variant —
the reason this contract exists is that the original repos each named these differently and
nobody could move between them.

## The ten platform keys

| Key | Kind | Purpose |
|---|---|---|
| `OVH_REGISTRY` | var | Registry host for `docker login` and the image ref |
| `OVH_REGISTRY_USERNAME` | var | Registry user |
| `OVH_PROJECT_ID` | var | OVH public-cloud project ID (`/cloud/project/<id>`) |
| `OVH_KUBERNETES_ID` | var | Managed Kubernetes cluster ID |
| `IMAGE_PULL_SECRET_NAME` | var | Name of the pre-created pull secret in the namespace |
| `OVH_REGISTRY_PASSWORD` | secret | Registry password |
| `OVH_GA_APPLICATION_KEY` | secret | OVH API application key |
| `OVH_GA_APPLICATION_SECRET` | secret | OVH API application secret |
| `OVH_GA_CONSUMER_KEY` | secret | OVH API consumer key |
| `KUBECONFIG_BASE64` | secret | `base64 -w0` of the deploy kubeconfig |

`KUBECONFIG_BASE64` is named for what it holds. Inside the deploy job `KUBECONFIG` is
already the standard kubectl/helm variable holding a *file path* (`/tmp/kubeconfig`).
Reusing one name for both is how older repos ended up with a kubeconfig-shaped string
where a path was expected.

`OVH_PROJECT_ID` and the three `OVH_GA_*` keys are identical everywhere and are
candidates for **org-level** variables and secrets with a repo access list — rotating
the OVH API credentials is then one edit instead of seven.

The registry trio is **not**. stage-k8s and prod-k8s have separate OVH registries, so
`OVH_REGISTRY`, `OVH_REGISTRY_USERNAME` and `OVH_REGISTRY_PASSWORD` differ by cluster and
belong at environment scope like everything else that differs — see the rule below about
one scope per key.

`KUBECONFIG_BASE64` is per **environment**, not per cluster: it holds the per-namespace
ServiceAccount's kubeconfig, so `dev` and `staging` need different ones even though both
point at stage-k8s, and likewise `prodtest` and `production` on prod-k8s. Four
environments, four kubeconfigs.

`OVH_KUBERNETES_ID` is per **cluster**, so on the four-environment flow dev and staging
carry the same value and prodtest and production carry the other. It is still set on each
environment; the two just match. `OVH_REGISTRY`, `OVH_REGISTRY_USERNAME` and
`OVH_REGISTRY_PASSWORD` are per cluster the same way — each cluster has its own registry,
which is also why an image built for stage-k8s cannot simply be promoted to prod-k8s.

## var or secret?

The split is about what has to be readable to debug a failed deploy, not about tidiness.

**Variable** when the value grants no access by itself and seeing it in the log saves an
investigation: hostnames, ports, IDs, resource names, feature flags, public URLs.

**Secret** when the value is a credential, a token, a connection string containing one, or
personal data.

Two consequences:

- Secret masking is literal. The chart base64-encodes secrets into a `Secret` object, and
  GitHub does **not** mask the base64 form — which is exactly why `helm diff` runs with
  `--suppress-secrets`.
- A key listed under `env.secrets` in the manifest but stored as a `var` still works, and
  that is the failure mode to watch for: the value reaches the cluster as a Secret while
  being plainly readable in the log and the settings UI. `render-values.py` emits a
  warning naming those keys.

The reverse — a non-credential stored as a secret — is worse than it looks. A hostname
kept as a secret makes a failed deploy undiagnosable, because the log shows `***`
exactly where you need to read a value.

## Which scope: organization, repository or environment

GitHub resolves a name at three scopes and the **narrowest one wins outright**. There is
no merge and no partial override: environment beats repository beats organization, per
name.

| Scope | What belongs here |
|---|---|
| **Organization** | `OVH_PROJECT_ID`, `OVH_GA_*` — identical everywhere, and rotating them is then one edit instead of seven |
| **Repository** | almost nothing — see below |
| **Environment** | `KUBECONFIG_BASE64`, `OVH_KUBERNETES_ID`, `IMAGE_PULL_SECRET_NAME`, the `OVH_REGISTRY` trio, and every app variable and secret |

### One scope per key, all four environments or none

If a key differs in *any* environment, set it at the environment scope in **all** of them
— including the ones where the value is the same.

The alternative looks tidier and debugs terribly: a repo-level default with an
environment-level override on the one environment that deviates. Now three environments
read the value from a place the fourth does not, and nothing in the log says which scope
a value came from. `render-values.py` prints key *names* only, never values and never
provenance, because it is handed the whole secrets context and any print of a value would
leak every secret at once. So "why did prodtest get the staging database?" has no answer
in the run log — you are reduced to reading four settings pages.

Four copies of an identical value is the cheaper problem. It is visible, greppable in the
settings UI, and wrong in only one place at a time.

### The repository scope is the trap

Anything at repository scope reaches **every** environment that does not define its own.
For an app key that means production's value silently becomes prodtest's default. This is
the single most likely way for prodtest to end up writing to the production database, and
it fails silently — the deploy is green, the pods are healthy, the data goes to the wrong
place.

Keep app variables and secrets out of the repository scope entirely. The rule that scales:
**if it names a host, database, bucket, queue, tenant or hostname, it is environment
scope.**

### Neutralising a value you cannot delete

If a repository-level key already exists and one environment must not receive it, set that
name to an **empty value** on the environment. `render-values.py` treats an empty string
as "declared but not set": the key is dropped from the release entirely and the app falls
back to its own default, rather than being handed `""`. The run log warns
`not set, omitted from the release`.

Check that GitHub accepts an empty value for the kind you are setting before relying on
this — and prefer removing the repository-level key outright, which is the real fix.

### Names never change per environment

The same key name in all four environments. Never `PRODTEST_DATABASE_URL`: the manifest
allowlists by name, so a prefixed name would have to be listed there, and would then be
handed to every environment that happens to define it. One name, four values, four
environments.

## App keys

Declared in `.github/deploy-manifest.yml` and matched **by name** against the GitHub
Environment. Nothing translates or prefixes: the name in the manifest, the name in the
environment, and the name the app reads from its own environment are the same string.

Two rules the renderer enforces:

- A name in both `env.variables` and `env.secrets` is a hard failure — the ConfigMap
  would carry the secret value in plain text.
- A name in `required:` that is unset for the target environment is a hard failure
  *before* helm runs. Use `required:` for values whose absence yields a
  running-but-broken release: the ingress host, a database connection string, a
  session-signing secret. Everything else, when unset, is omitted and the app falls back
  to its own default — which is only correct if the app really has one, so check.

Anything in the `secrets` context but absent from the manifest allowlist is ignored
outright, `github_token` included.

## Build-time vs deploy-time

Everything above describes values handed to a *running* pod. Some apps also need a value
while the image is being built — most often a frontend bundler that inlines a public env
prefix, or a credential a build step authenticates with. That is a different mechanism
with a different failure mode, and it is declared separately, in the manifest's
`buildArgs:` block.

### Why a caller cannot pass one

`jobs.<id>.environment` is not allowed on a job that uses `uses:`. So every expression in
a caller's `with:` — including `build-args` — resolves at **repository and organization
scope only**, and an environment-scoped variable read there is the empty string. Not an
error, not a warning: empty.

The build job inside the shared workflow *does* declare `environment:`, which is what
makes `vars` and `secrets` there the target environment's. `actions/render-build-args`
runs in that job and reads the manifest, which is why the allowlist lives in the manifest
rather than the caller:

```yaml
buildArgs:
  variables:            # -> --build-arg NAME=value, from vars.*
    - PUBLIC_API_URL
  secrets:              # -> buildx --secret id=NAME, from secrets.*
    - NPM_TOKEN
```

The workflow's `build-args` input still works and is appended to, not replaced. Use it
only for a value that is identical in every environment.

### variables or secrets?

Not the same question as the var/secret split above, and the answer is stricter.

A `--build-arg` is recorded in the image metadata and the build cache. Anyone who can
pull the image can read it. So `buildArgs.variables` is for values that are already
public once the image ships — a hostname, a public API URL, a publishable client key.
A name that is in `env.secrets` and in `buildArgs.variables` is a hard failure, and so
is a name stored as a `secret` but declared as a build-arg variable: masked in the log
while written into the image is the worst of both.

`buildArgs.secrets` is a buildx secret mount: a file present for the life of one `RUN`,
recorded in no layer, no metadata and no cache entry. It needs a matching line in the
app's Dockerfile, which is the one part the manifest cannot do for you:

```dockerfile
# syntax=docker/dockerfile:1.7
RUN --mount=type=secret,id=NPM_TOKEN \
    NPM_TOKEN="$(cat /run/secrets/NPM_TOKEN)" npm ci
```

Read the mounted file with `cat` and no trimming — the renderer writes the value with no
trailing newline on purpose, because a newline inside a token is invisible in a log and
breaks whatever the token authenticates.

### What a build arg costs, every time

A baked value pins the image to one environment. Two consequences follow, and neither is
recoverable later without changing the app:

- staging and production **build the same commit twice**, and two builds of one commit
  are only presumed identical.
- **build-once-promote is off the table** for that app. `prodtest` exists to run the
  exact bytes staging tested; it cannot, if those bytes carry staging's configuration.

So the order of preference is: read it at runtime from the ConfigMap or Secret; if the
build truly cannot defer it, `buildArgs`; and if it is a credential, `buildArgs.secrets`.
A value read at runtime also changes with a `helm upgrade` instead of a rebuild.

## Per-environment values

The environment name doubles as the registry repository prefix:
`<OVH_REGISTRY>/<environment>/<app>:<commit-sha>`. So one environment's images never
collide with another's, and the environment an image came from is visible in its ref.
Override with `repositoryPrefix` in an `environments:` block when a deviating environment
must push somewhere else.

`prodtest` is the built-in exception. It deploys the artifact `staging` already built and
tested rather than rebuilding the same commit, so it reads `<registry>/staging/<app>`.
That is what makes prod-k8s run the exact bytes stage-k8s ran; the cost is that the pull
secret in `prodtest-<app>` needs read access to the `staging/` path.

The namespace follows the environment the same way. `dev` and `prodtest` share a cluster
with another environment of the same app, so they take a prefixed namespace —
`dev-<app>` and `prodtest-<app>` — while `staging` and `production` use `<app>` bare. No
app repo writes this down; the manifest's one `namespace:` is the base and the prefix is
applied per environment. See [multi-environment.md](multi-environment.md).

The image tag is always the commit SHA. No `latest`, no `<branch>-<sha>` — one immutable
tag means "what is running?" has one answer, and a rollback targets a real revision rather
than a moving pointer. It is also why `image.pullPolicy` can safely be `IfNotPresent`.

## Retired names

`scripts/check-workflow.sh` fails on each of these.

| Old | Now |
|---|---|
| `vars.OVH_DOCKER_REGISTRY_URL` | `vars.OVH_REGISTRY` |
| `secrets.OVH_DOCKER_REGISTRY_USERNAME` | `vars.OVH_REGISTRY_USERNAME` |
| `secrets.OVH_DOCKER_REGISTRY_PASSWORD` | `secrets.OVH_REGISTRY_PASSWORD` |
| `vars.OVH_SERVICE_NAME` | `vars.OVH_PROJECT_ID` |
| `secrets.OVH_APPLICATION_KEY` | `secrets.OVH_GA_APPLICATION_KEY` |
| `secrets.OVH_APPLICATION_SECRET` | `secrets.OVH_GA_APPLICATION_SECRET` |
| `secrets.OVH_CONSUMER_KEY` | `secrets.OVH_GA_CONSUMER_KEY` |
| `secrets.KUBECONFIG`, `secrets.KUBE_CONFIG` | `secrets.KUBECONFIG_BASE64` |
| `secrets.KUBERNETES_DOCKER_REGISTRY_CREDENTIALS` | gone — the pull secret is pre-created; only its *name* is passed |

Delete the old keys from the Environment only **after** the first successful deploy on the
new pipeline, so a rollback to the previous workflow is still possible.
