# Estate audit — what each app repo does today

Snapshot taken August 2026, before any migration. Recorded so a future migration starts
from fact rather than from re-reading seven pipelines.

Only `omnicasa-tools` has been migrated. Everything below describes the other six as
they still are.

## The numbers

- **2371 lines** of workflow YAML across seven repos
- **zero** shared code
- **four** hand-drifted copies of the OVH IP-allowlist request-signing block
- **three** naming generations for the same platform keys
- **three** different chart locations
- the same `--set` block repeated **three times per file** (template, diff, upgrade)

## Per repo

| Repo | Chart location | Kubeconfig key | Registry key | Pull secret | App values method |
|---|---|---|---|---|---|
| `Omnicasa-Oauth2-Consent-App` | `chart/` | `KUBECONFIG_BASE64` | `OVH_REGISTRY` | pre-created (name via var) | **values files** — the reference |
| `omnicasa-tools` | `k8s/helm/` | `KUBECONFIG_BASE64` | `OVH_REGISTRY` | pre-created | `--set` ×3 — **migrated** |
| `omnicasa-api-access` | `k8s/helm/` | `KUBE_CONFIG` | `OVH_REGISTRY` | pre-created | `--set` ×3 |
| `omnicasa-peppol` | `k8s/manifest/` | `KUBE_CONFIG` | `OVH_REGISTRY` | pre-created | `--set` ×3 |
| `omnicasa-payload` | `chart/` | `KUBECONFIG` | `OVH_DOCKER_REGISTRY_URL` | **created in-workflow** | `--set` |
| `omnicasa-email-editor` | `chart/` | `KUBECONFIG` | `OVH_DOCKER_REGISTRY_URL` | **created in-workflow** | `--set` |
| `omnicasa-webhook` | `chart/` | `KUBECONFIG` | `OVH_DOCKER_REGISTRY_URL` | **created in-workflow** | `--set` |

"Created in-workflow" means the chart builds the registry pull secret from
`KUBERNETES_DOCKER_REGISTRY_CREDENTIALS`, putting the registry password in two places.
The current model pre-creates the secret out of band and passes only its *name*.

## Retired key names

Still present in payload, email-editor and webhook. `scripts/check-workflow.sh` fails on
each of them. Full rationale in [env-contract.md](env-contract.md).

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
| `secrets.KUBERNETES_DOCKER_REGISTRY_CREDENTIALS` | gone — only the pull secret's *name* is passed |

## What only the consent app had

Everything in this list is now standard in the shared pipeline. Before it, exactly one
repo of seven had each:

- the OVH allowlist logic extracted to a script instead of inlined
- values files instead of `--set` (so a password containing a comma cannot corrupt a release)
- `--suppress-secrets` on `helm diff`
- helm pinned to v3.19.0 and kubectl to v1.31.0
- `--atomic --history-max 10`
- explicit namespace pre-create instead of `--create-namespace`
- `cancel-in-progress: false`
- failure diagnostics
- cleanup of the rendered secret files
- `permissions: contents: read`

## Branch and environment conventions

| Repo | Branches | Environment resolution |
|---|---|---|
| consent-app, email-editor, payload (`ci.yaml`) | `staging`, `production` | branch name **is** the environment name |
| payload (`ci_dev.yaml`) | `development` | hardcoded — and wrong, see the backlog |
| peppol | `main`, `develop`, `develop-ci` | all three hardcoded to `production` |
| tools | `main` | hardcoded to `production` |

Registry path convention is uniform: `<OVH_REGISTRY>/<environment>/<app>:<commit-sha>`.

## Infrastructure that already exists

Both are driven by hand today. See the backlog's bootstrap-automation item.

- `terraform/infra-terraform/scripts/sync-registry-to-k8s.sh` — distributes the OVH
  registry pull secret into namespaces.
- `terraform/infra-helm/github-actions-rbac-per-ns/` — a helmfile creating, per
  namespace in a list, a ServiceAccount + Role + RoleBinding with no cluster-level
  access. The namespace list lives in
  `values/{stage-k8s,prod-k8s}/github-actions-rbac-per-ns.yaml`.
- `terraform/infra-helm/github-actions-rbac/` — the older cluster-wide variant.
