# Chart values reference

Chart: `omnicasa-app`, vendored at `actions/helm-deploy/chart`.

`values.schema.json` sets `additionalProperties: false` at the top level, so a typo like
`replicasCount` fails `helm lint` in this repo's CI rather than silently deploying the
default.

## Layering

```
1. actions/helm-deploy/chart/values.yaml   chart defaults
2. deploy/values.yaml                      per-app, every environment   (app repo)
3. deploy/values.<environment>.yaml        per-app, this environment    (app repo)
4. /tmp/release-values.json                computed: image, ingress, pull secret
5. /tmp/app-env-values.json                computed: config.env.variables / .secrets
```

Files 2 and 3 are skipped silently if absent. `helm diff` receives the identical list in
the identical order — that equality is the only thing that makes the diff worth reading.

**Maps merge, they do not replace.** This catches people out with probes and ingress
annotations: setting `livenessProbe.httpGet` does not clear the chart's
`failureThreshold`. If you care about a field the chart also sets, state it explicitly.

## The values

### Identity — do not touch on an existing release

| Key | Default | Notes |
|---|---|---|
| `nameOverride` | `""` | Changes `app.kubernetes.io/name`, part of the **immutable** Deployment selector |
| `fullnameOverride` | `""` | Changes every object's name |

The chart's `chart.name` helper defaults to `.Release.Name`, not `.Chart.Name`. That is
deliberate and load-bearing: with one shared chart, a `.Chart.Name` default would rewrite
every existing release's selector to `omnicasa-app` and `helm upgrade` would fail with
"field is immutable". `check-workflow.sh` fails if an app sets either override.

### Workload

| Key | Default | Notes |
|---|---|---|
| `replicaCount` | `1` | Ignored when `autoscaling.enabled` |
| `revisionHistoryLimit` | `5` | Old ReplicaSets kept |
| `strategy` | RollingUpdate 25%/25% | |
| `image.registry` / `.repository` / `.tag` | set by the workflow | tag is always the commit SHA |
| `image.pullPolicy` | `IfNotPresent` | Safe only because tags are immutable |
| `resources` | 100m/256Mi → 500m/512Mi | |
| `priorityClassName`, `terminationGracePeriodSeconds` | unset | |
| `podAnnotations`, `podLabels`, `podSecurityContext`, `securityContext` | `{}` | |
| `nodeSelector`, `tolerations`, `affinity`, `topologySpreadConstraints` | empty | |
| `initContainers`, `extraVolumes`, `extraVolumeMounts` | `[]` | |
| `lifecycle` | `{}` | A `preStop` sleep helps zero-downtime behind haproxy |

### Configuration

| Key | Default | Notes |
|---|---|---|
| `config.env.variables` | `{}` | → ConfigMap, mounted `envFrom`. Set by the renderer |
| `config.env.secrets` | `{}` | → Secret, mounted `envFrom`. Set by the renderer |
| `extraEnv` | `[]` | Raw `EnvVar` list — for `fieldRef` and similar |
| `extraEnvFrom` | `[]` | Extra `configMapRef`/`secretRef` |
| `imagePullSecrets` | `[]` | Set by the renderer from `IMAGE_PULL_SECRET_NAME` |
| `hostAliases` | `[]` | Internal `.local` names. List only what the app talks to |

The Deployment carries `checksum/config` and `checksum/secret` pod annotations, so a
config-only change actually rolls the pods instead of being a no-op nobody notices.

### Networking

| Key | Default | Notes |
|---|---|---|
| `service.containerPort` | `3000` | **Set this.** Probes resolve through the named `http` port, so this is the only place the number appears |
| `service.port` | `80` | In-cluster only; the ingress fronts the app |
| `service.type`, `.annotations`, `.extraPorts`, `.extraServicePorts` | | |
| `ingress.enabled` | `true` | |
| `ingress.className` | `haproxy` | |
| `ingress.annotations` | `cert-manager.io/cluster-issuer: letsencrypt-dns01` only | Override the issuer per environment; see below |
| `ingress.hosts`, `.tls` | placeholders | Set by the renderer from `domainVar` |
| `ingress.extraRules` | `[]` | For shapes the single-host block cannot express |

Only the cluster-issuer is defaulted. Anything that changes how requests are *handled* —
`haproxy.org/ssl-redirect` in particular — is deliberately not, because annotations merge
and a default here would silently apply to every app. `ssl-redirect` behind a Cloudflare
zone set to Flexible SSL produces a redirect loop. Apps that want it declare it.

The default is `letsencrypt-dns01`. DNS-01 does not need the host reachable on :80, so it
works for an environment that is not publicly exposed and it is the only solver that can
issue a wildcard. `letsencrypt` (HTTP-01) remains available for anything whose zone the
DNS-01 solver does not hold credentials for.

The name is a default, not an allowlist: the template emits `ingress.annotations`
verbatim, so any ClusterIssuer the cluster actually has works. Put an override in
`deploy/values.<environment>.yaml`, not `deploy/values.yaml`, so only that environment
moves; annotations deep-merge, so the app's own `ssl-redirect` keys survive:

```yaml
# deploy/values.staging.yaml
ingress:
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt
```

A name with no matching ClusterIssuer fails **silently**: cert-manager leaves the
Certificate pending, the ingress serves the default certificate, and the Helm release
still goes green. Check `kubectl get clusterissuer <name>` on the target cluster once,
when introducing an issuer to an environment.

### Health

| Key | Default |
|---|---|
| `startupProbe` | `{}` |
| `livenessProbe` | `httpGet / :http`, 15s delay, threshold 6 |
| `readinessProbe` | `httpGet / :http`, 5s delay, threshold 3 |

`helm upgrade --atomic --wait` decides success from pod readiness. Point these at an
endpoint that fails when the app's dependencies are unreachable — a probe that only proves
the process is up makes `--atomic` decorative. Set to `{}` to disable.

### Scaling and disruption

| Key | Default | Notes |
|---|---|---|
| `autoscaling.enabled` | `false` | Renders a **KEDA `ScaledObject`**, not an `autoscaling/v2` HPA |
| `autoscaling.minReplicas` / `.maxReplicas` | `2` / `5` | |
| `autoscaling.targetCPUUtilizationPercentage` | `80` | |
| `autoscaling.targetMemoryUtilizationPercentage` | unset | |
| `autoscaling.triggers`, `.advanced` | `[]` / `{}` | Raw KEDA passthrough |
| `podDisruptionBudget.enabled` | `false` | |

A plain HPA applies cleanly against this cluster and then does nothing, which is why the
chart emits a ScaledObject. Its `scaleTargetRef` must match the Deployment name exactly or
KEDA silently scales nothing.

The PDB is opt-in because at `maxUnavailable: 50%` on a single-replica Deployment it
evaluates to zero allowed disruptions and blocks every node drain.

### Migrations

| Key | Default | Notes |
|---|---|---|
| `migration.enabled` | `false` | |
| `migration.command` / `.args` | `[]` | e.g. `["npx","prisma","migrate","deploy"]` |
| `migration.image.repository` | `""` | Empty means the app image |
| `migration.backoffLimit` | `0` | Never retry against a half-applied schema |
| `migration.activeDeadlineSeconds` | `300` | Keep well under the workflow's `helm-timeout` |
| `migration.hookWeight` | `-5` | Runs before other hooks |
| `migration.restartPolicy`, `.ttlSecondsAfterFinished`, `.resources` | | |

Renders a `pre-install,pre-upgrade` Helm hook Job using the same image and the same
`envFrom` as the Deployment, so the migration cannot read a different `DATABASE_URL` than
the app will.

> **`--atomic` rolls back the release, not the database.** A migration that half-applies
> leaves the schema ahead of the rolled-back code. Migrations must therefore be additive
> and backward-compatible for one release: expand, deploy, contract. That policy is what
> makes "fix forward" always available — see [runbook-rollback.md](runbook-rollback.md).

Check the runtime image actually ships the migration tool. `omnicasa-tools` needed
`node_modules/prisma` and `node_modules/.bin/prisma` copied into its runner stage; without
them `npx` would fetch the CLI from npm at pod start, as a non-root user, in a namespace
that may have no egress.

### Scheduled work and escape hatches

| Key | Default | Notes |
|---|---|---|
| `cronJobs` | `[]` | `{name, schedule, command, args, concurrencyPolicy, suspend, resources}`. Uses the app image and config, so a scheduled task cannot run older code |
| `extraManifests` | `[]` | Raw YAML objects, templated (`{{ .Release.Name }}` works) |
| `commonLabels`, `commonAnnotations` | `{}` | Applied to object metadata — **not** to selector labels, which are immutable |

`extraManifests` exists so no app ever has a reason to fork the chart. Anything that shows
up in it twice belongs in a real template.
