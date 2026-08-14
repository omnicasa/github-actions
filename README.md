# omnicasa/github-actions

Shared CI/CD for Omnicasa app repos: one reusable deploy workflow, one unified Helm chart,
one set of composite actions. An app repo's deploy config is a ~19-line caller workflow, a
declarative manifest, and a values file.

Before this repo existed, seven app repos carried 2371 lines of workflow YAML with zero
shared code — four hand-drifted copies of the OVH IP-allowlist signing block, three naming
generations of the same platform keys, three chart locations, and a `--set` block duplicated
three times per file. Every bugfix had to be applied seven times.

## Quickstart for an app repo

```yaml
# .github/workflows/deploy.yml
name: Deploy
on:
  push:
    branches:
      - main
  workflow_dispatch:
    inputs:
      dry-run:
        description: "Render and diff only, do not upgrade"
        type: boolean
        default: false
permissions:
  contents: read
jobs:
  deploy:
    uses: omnicasa/github-actions/.github/workflows/deploy.yml@v1.0.0
    with:
      environment: production
      dry-run: ${{ inputs.dry-run == true }}
    secrets: inherit
```

Plus `.github/deploy-manifest.yml` and `deploy/values.yaml`. Start from `templates/`, and
follow [docs/onboarding.md](docs/onboarding.md).

## What is here

| Path | Purpose |
|---|---|
| `.github/workflows/deploy.yml` | The reusable `workflow_call` pipeline: test → build → deploy |
| `.github/workflows/rollback.yml` | Reusable rollback, same concurrency group as deploy |
| `actions/setup-cluster` | Pinned helm + kubectl + helm-diff, writes the kubeconfig |
| `actions/ovh-ip-allowlist` | Adds/removes the runner IP on the OVH cluster allowlist |
| `actions/render-values` | Turns the GitHub Environment into two Helm values files |
| `actions/helm-deploy` | Ensure-ns, unstick, diff, `helm upgrade --atomic`, rollout status — **and carries the chart** |
| `actions/helm-rollback` | `helm history` then `helm rollback` |
| `actions/k8s-diagnostics` | Failure evidence dump; `--atomic` destroys it otherwise |
| `templates/` | Copy-paste starting points for app repos |
| `scripts/` | `check-workflow.sh`, `validate-manifest.py` — run these before opening a PR |
| `docs/` | Onboarding, the env-key contract, every chart value, the rollback runbook |

## Why the chart lives inside a composite action

`actions/checkout` inside a reusable workflow checks out the **caller** repo, so files in
this repo are not on disk during a deploy. Composite actions do ship their own files and
expose `${{ github.action_path }}`. Vendoring the chart there means one `@vX.Y.Z` tag pins
the chart, the workflow, and the scripts together — you cannot get a new chart with an old
workflow. It also needs no extra registry auth.

## Versioning

Consumers track the floating major tag, `@v1`. It is moved only by `release.yml`, on an
annotated `vX.Y.Z` push, and every merge to `main` is gated by `ci.yml` first.

The trade-off is deliberate and worth stating: a consumer's next deploy picks up whatever
`v1` points at, without a PR to review. In exchange, there is exactly one ref in the whole
system — the callers, the reusable workflows and the composite actions they invoke all say
`@v1`, so they cannot disagree with each other. Mixed refs are the failure mode this
avoids: a workflow pinned at `v1.0.0` invoking actions at `v1` silently combines two
versions, because a called workflow's `uses:` is resolved fresh and does not inherit the
ref the workflow itself was loaded from.

A repo that wants stability instead can pin `@vX.Y.Z` — but then **every** internal action
ref must be pinned to the same version, or you get exactly the split above.

Third-party actions used *inside* this repo are pinned by tag and updated by Dependabot.

Every change is in [CHANGELOG.md](CHANGELOG.md). Read it before moving `v1`.

## Access

This repo is private. For another private `omnicasa` repo to consume it, Settings → Actions
→ General → **Access** must be "Accessible from repositories in the omnicasa organization".
No PAT is involved — GitHub passes a scoped read token to the runner.
# github-actions
