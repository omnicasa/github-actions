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

These are candidates for **org-level** variables and secrets with a repo access list.
Set that way, onboarding a repo needs only `KUBECONFIG_BASE64` (per environment, since
staging and production are separate clusters), `IMAGE_PULL_SECRET_NAME` and the app's own
keys — and rotating the OVH credentials is one edit instead of seven.

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

## Per-environment values

The environment name doubles as the registry repository prefix:
`<OVH_REGISTRY>/<environment>/<app>:<commit-sha>`. So staging and production images never
collide, and the environment an image came from is visible in its ref. Override with
`repositoryPrefix` in an `environments:` block when a deviating environment must push
somewhere else.

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
