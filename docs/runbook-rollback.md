# Runbook — a deploy went wrong

Read top to bottom. Most incidents stop at step 1.

## 0. What already happened automatically

`helm upgrade` runs with `--atomic`, so a failed upgrade **has already rolled itself back**
before the job finished. If the deploy job is red, the previous version is almost certainly
still serving. Confirm before doing anything:

```bash
kubectl get pods -n <namespace>
helm history <app> -n <namespace> --max 10
```

`--atomic` also deleted the failed pods, which is why the workflow's `if: failure()`
diagnostics step exists. **Read the job summary before rerunning anything** — a rerun
overwrites nothing, but the evidence from this failure is already gone from the cluster.

## 1. The deploy failed but the app is up

Normal case. Fix forward: the diagnostics dump in the job summary has the pod events,
the describe output and the container logs, including `--previous` for crash loops.

Common causes, in the order they actually occur:

| Symptom in the dump | Cause |
|---|---|
| `ImagePullBackOff` | pull secret missing in the namespace, or the image was pushed under a different environment prefix |
| `CreateContainerConfigError` | the pod references a ConfigMap/Secret key that no longer exists — usually mid-migration |
| `CrashLoopBackOff`, app logs show a missing env var | key not set for this environment; add it and check it is in the manifest allowlist |
| readiness never passes | probe path or port wrong, or a dependency the probe checks is down |
| `context deadline exceeded` on the helm step | the app takes longer to become ready than `helm-timeout` |

## 2. The app is down and you need the previous version now

Run the repo's **Rollback** workflow (`Actions → Rollback → Run workflow`).

- **environment**: the one that broke
- **revision**: leave blank for the previous revision, or pick one from `helm history`

It prints `helm history` before rolling back, rolls back with `--wait`, verifies the
rollout and dumps diagnostics if the rollback itself fails.

Prefer this over running helm by hand: it goes through the environment's protection rules,
holds the same concurrency lock as the deploy workflow (so a deploy cannot land
mid-rollback), and leaves an audit trail.

By hand, only if Actions itself is unavailable:

```bash
helm history <app> -n <namespace> --max 10
helm rollback <app> <revision> -n <namespace> --wait --timeout 10m
kubectl rollout status deployment/<app> -n <namespace>
```

## 3. "another operation is in progress"

The release is stuck in `pending-upgrade`, `pending-install` or `pending-rollback`, left
by a run that was cancelled mid-upgrade.

The deploy workflow detects and clears this automatically before deploying, so **just run
the deploy again**. By hand:

```bash
helm status <app> -n <namespace> -o json | jq -r '.info.status'
helm rollback <app> -n <namespace>
```

This is why the deploy job sets `cancel-in-progress: false`. Never cancel a running deploy
job — it leaves both this lock and a stale IP on the OVH allowlist.

## 4. A migration ran and the deploy still failed

**`--atomic` rolls back the release, not the database.** If the pre-upgrade migration Job
succeeded and the upgrade then failed, the schema is ahead of the code that is now running.

```bash
kubectl logs -n <namespace> job/<app>-migrate
```

The chart sets `backoffLimit: 0` so a failed migration stops immediately rather than
retrying against a half-applied schema. Options, in order of preference:

1. **Fix forward.** If migrations are additive and backward-compatible (the policy), the
   old code runs fine against the new schema. Deploy the corrected image.
2. Roll back the code and leave the schema ahead. Safe only if the migration was additive.
3. Roll back the schema by hand. Last resort, and needs a DBA, not a runbook.

This ordering is why the policy is expand-then-contract: it makes option 1 always available.

## 5. Deploys hang, or kubectl times out with no useful error

The runner's IP is not on the OVH API allowlist.

The allowlist action now checks the HTTP status and fails loudly, so this should surface
as a clear error in the "Authorise runner IP" step. If the job was cancelled at the runner
level between the add and the remove, a stale `/32` is left behind instead — harmless
individually, but they accumulate.

Check the current allowlist through the OVH API or console under
`/cloud/project/<project>/kube/<cluster>/ipRestrictions`.

## 6. Everything is fine but nothing changed

The image tag is the commit SHA, so redeploying the same commit is genuinely a no-op for
the Deployment. Config-only changes *do* roll the pods — the chart puts `checksum/config`
and `checksum/secret` annotations on the pod template for exactly this reason.

If a config change did not take effect, check that the key is in the manifest's
`env.variables` or `env.secrets` allowlist. Anything not listed is dropped silently by
design, though the renderer warns about declared-but-unset keys.
