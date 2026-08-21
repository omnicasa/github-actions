#!/usr/bin/env python3
"""Render the two Helm values files the deploy workflow feeds to `helm`.

Reads `.github/deploy-manifest.yml` plus the whole GitHub `vars` and `secrets`
contexts (as JSON, via env), and writes:

  /tmp/release-values.json   image ref, ingress host/TLS/external-dns, pull secret
  /tmp/app-env-values.json   config.env.variables + config.env.secrets

Why a values file instead of `helm --set`: `--set` treats `,` as a list
separator, so any password or comma-separated list silently corrupts the
release. It also puts secret values on the helm command line. Going through a
file also guarantees `helm diff` and `helm upgrade` see byte-identical inputs.

Why the whole context instead of one `env:` entry per key: the manifest is then
the single source of truth. Adding a tunable is one line there, not three
places (workflow env block + python list + the GitHub Environment).

Which app, which namespace and which registry repository are NOT decided here —
they come from ../resolve-target/target.py, shared with the build job and the
rollback workflow so the three can never disagree about where an image lives.

SECURITY INVARIANT: this script prints key *names* only, never values. The
`secrets` context is passed in wholesale, so any print of a value would leak
every secret into the job log. Keep every diagnostic on the name side.
Keys absent from the manifest allowlist are ignored outright — including
`github_token` — so nothing the app did not ask for reaches the cluster.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Sibling action directory. Both actions are checked out from the same ref of this
# repo, side by side, so this resolves both in the runner's _actions cache and in a
# plain clone (which is how CI exercises this file).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "resolve-target"))

from target import emit_output, fail, notice, resolve_from_env, warn  # noqa: E402

RELEASE_OUT = Path(os.environ.get("RELEASE_VALUES_OUT", "/tmp/release-values.json"))
APP_ENV_OUT = Path(os.environ.get("APP_ENV_VALUES_OUT", "/tmp/app-env-values.json"))

# `${NAME}` references inside `env.derived` values.
DERIVED_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def load_context(var_name: str) -> dict:
    """Parse a `toJSON(vars)` / `toJSON(secrets)` payload.

    An unset or empty context is legitimate (a repo with no secrets yet), but a
    non-empty non-object payload means the workflow wired the wrong expression.
    """
    raw = os.environ.get(var_name, "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        fail(f"{var_name} is not valid JSON — expected ${{{{ toJSON(...) }}}}")
    if not isinstance(data, dict):
        fail(f"{var_name} decoded to {type(data).__name__}, expected an object")
    # Empty strings mean "declared but not set" — treat them as absent so the
    # app falls back to its own defaults instead of being handed "".
    return {k: v for k, v in data.items() if isinstance(v, str) and v != ""}


def as_list(manifest: dict, *path: str) -> list[str]:
    node = manifest
    for key in path:
        node = (node or {}).get(key) if isinstance(node, dict) else None
    if node is None:
        return []
    if not isinstance(node, list):
        fail("manifest key '" + ".".join(path) + "' must be a list of names")
    return [str(x) for x in node]


def resolve_derived(
    manifest: dict, available: dict[str, str], secret_names: set[str]
) -> dict[str, str]:
    """Expand `env.derived` templates against every value set for this environment.

    Derived values keep one authoritative source per fact — e.g. a single
    APP_DOMAIN driving both the ingress host and the app's own self-URL, so the
    two can never drift apart.

    The whole environment is in scope, not just the allowlisted app keys, because
    the most useful source is usually `domainVar` — which is consumed by the
    ingress and often not an app variable at all.
    """
    derived_spec = ((manifest.get("env") or {}).get("derived")) or {}
    if not isinstance(derived_spec, dict):
        fail("manifest key 'env.derived' must be a mapping of name -> template")

    out: dict[str, str] = {}
    for name, template in derived_spec.items():
        refs = set(DERIVED_REF.findall(str(template)))
        missing = sorted(r for r in refs if r not in available)
        if missing:
            warn(f"derived {name} omitted: depends on unset " + ", ".join(missing))
            continue
        # Derived values land on the variables side, so a secret baked into one
        # would be readable in the ConfigMap — silently undoing the split.
        leaked = sorted(refs & secret_names)
        if leaked:
            fail(
                f"derived {name} is built from secret(s) {', '.join(leaked)} — it "
                "would end up in the ConfigMap in plain text"
            )
        out[str(name)] = DERIVED_REF.sub(lambda m: available[m.group(1)], str(template))
    return out


def main() -> None:
    # app / namespace / repository and the environments-block merge all come from
    # the shared resolver, so the image this renders cannot point somewhere the
    # build job did not push.
    target = resolve_from_env()
    environment = target.environment
    manifest = target.manifest
    app = target.app
    namespace = target.namespace

    # Belt and braces: the workflow gates on `enabled` before this ever runs, but a
    # caller invoking the action directly must not quietly deploy a switched-off
    # environment.
    if not target.enabled:
        fail(
            f"environment '{environment}' is disabled in the deploy manifest "
            "(environments.<env>.enabled: false) — refusing to render a release for it"
        )

    gh_vars = load_context("VARS_JSON")
    gh_secrets = load_context("SECRETS_JSON")

    image_tag = os.environ.get("IMAGE_TAG", "").strip()
    if not image_tag:
        fail("IMAGE_TAG is empty — the workflow must pass the commit SHA it built")

    # --- required keys: fail before a half-deploy, not during one -----------
    #
    # A blank ingress host renders a structurally valid but useless Ingress, and
    # a blank connection string starts a pod that crash-loops. Both are worse
    # than never starting the upgrade.
    everything = {**gh_vars, **gh_secrets}
    missing_required = [k for k in as_list(manifest, "required") if k not in everything]
    if missing_required:
        fail(
            "not set for environment '"
            + (environment or "?")
            + "': "
            + ", ".join(missing_required)
            + " — add them to the GitHub Environment before redeploying"
        )

    # --- app config: allowlisted by the manifest ---------------------------
    variable_names = as_list(manifest, "env", "variables")
    secret_names = as_list(manifest, "env", "secrets")

    overlap = sorted(set(variable_names) & set(secret_names))
    if overlap:
        fail(
            "declared as both variable and secret: "
            + ", ".join(overlap)
            + " — pick one, or the ConfigMap will expose the secret value"
        )

    variables = {n: everything[n] for n in variable_names if n in everything}
    secrets = {n: everything[n] for n in secret_names if n in everything}

    # Derived values are non-sensitive by construction (URLs built from a
    # domain), so they join the variables side.
    variables.update(resolve_derived(manifest, everything, set(secret_names)))

    omitted = [n for n in variable_names + secret_names if n not in everything]
    if omitted:
        warn(
            "not set, omitted from the release (the app keeps its own default): "
            + ", ".join(omitted)
        )

    # A secret declared in `env.secrets` but present only in `vars` is almost
    # always a misfiled key: the value ends up in a Secret but is also readable
    # in the job log and the repo settings UI.
    misfiled = [n for n in secret_names if n in gh_vars and n not in gh_secrets]
    if misfiled:
        warn(
            "declared as a secret but found in vars (visible in logs): "
            + ", ".join(misfiled)
        )

    # --- release values ----------------------------------------------------
    registry = gh_vars.get("OVH_REGISTRY")
    if not registry:
        fail("vars.OVH_REGISTRY is not set for this environment")

    # The workflow passes the repository the build job actually pushed to. It is
    # computed by the same resolver, so the two agree by construction; the input
    # exists so a promotion between environments can be stated explicitly.
    repository = os.environ.get("REPOSITORY", "").strip() or target.repository

    release: dict[str, object] = {
        "image": {
            "registry": registry,
            "repository": repository,
            # Immutable: always the commit SHA, never a floating tag, so the
            # running image can be traced back to a commit.
            "tag": image_tag,
        }
    }

    pull_secret = gh_vars.get("IMAGE_PULL_SECRET_NAME")
    if pull_secret:
        release["imagePullSecrets"] = [{"name": pull_secret}]
    else:
        warn(
            "vars.IMAGE_PULL_SECRET_NAME is not set — pods will fail to pull from "
            "the private registry with ImagePullBackOff"
        )

    # `ingress.managed: false` hands the whole Ingress to the app's own values
    # file — for apps whose shape (multiple hosts, non-root paths) the single-host
    # block below cannot express.
    ingress_spec = manifest.get("ingress") or {}
    if not isinstance(ingress_spec, dict):
        fail("manifest key 'ingress' must be a mapping")
    ingress_managed = ingress_spec.get("managed", True)

    domain_var = str(manifest.get("domainVar") or "").strip()
    if domain_var and ingress_managed:
        domain = everything.get(domain_var)
        if not domain:
            fail(
                f"{domain_var} is not set — it drives the ingress host, the TLS "
                "certificate and the external-dns record"
            )
        # Defaults to <app>-tls-certificates. Apps migrating from an older chart
        # set tlsSecretName to whatever their live Secret is called: pointing at a
        # new name makes cert-manager issue a fresh certificate on cutover, with a
        # window of TLS errors and a Let's Encrypt rate limit to worry about.
        tls_secret = str(manifest.get("tlsSecretName") or "").strip() or (
            f"{app}-tls-certificates"
        )
        release["ingress"] = {
            # Merges into the annotations in chart/values.yaml rather than
            # replacing them, so the cert-manager default survives.
            "annotations": {
                "external-dns.alpha.kubernetes.io/hostname": domain,
            },
            "hosts": [{"host": domain, "paths": [{"path": "/", "pathType": "Prefix"}]}],
            "tls": [{"secretName": tls_secret, "hosts": [domain]}],
        }
    elif domain_var and not ingress_managed:
        notice(
            "ingress.managed is false — the ingress comes entirely from the app's "
            "values file, and domainVar is used only for the required-key check"
        )

    # Deprecated. `helmValues` merges into release-values.json, which the workflow
    # layers AFTER the app's own deploy/values.yaml — so it silently outranks the
    # file people expect to be authoritative. Put chart overrides in deploy/values.yaml.
    extra = manifest.get("helmValues") or {}
    if extra:
        if not isinstance(extra, dict):
            fail("manifest key 'helmValues' must be a mapping")
        warn(
            "manifest key 'helmValues' is deprecated and outranks deploy/values.yaml; "
            "move these keys into deploy/values.yaml: " + ", ".join(sorted(extra))
        )
        release = deep_merge(release, extra)

    # helm reads values files as YAML, and JSON is valid YAML — so no YAML
    # quoting rules to get wrong for values containing `:`, `#` or newlines.
    RELEASE_OUT.write_text(json.dumps(release, indent=2))
    APP_ENV_OUT.write_text(
        json.dumps({"config": {"env": {"variables": variables, "secrets": secrets}}})
    )

    # `migration` deliberately has no home here. The chart reads .Values.migration.*
    # straight from deploy/values.yaml; a copy in the manifest would be a second
    # switch that looks authoritative and does nothing.
    if "migration" in manifest:
        fail(
            "manifest key 'migration' has no effect — the chart reads migration.* from "
            "deploy/values.yaml. Move it there and delete it here, or the two will drift"
        )

    for key, value in (
        ("app", app),
        ("namespace", namespace),
        ("repository", repository),
        ("image", f"{registry}/{repository}:{image_tag}"),
        ("release-values", str(RELEASE_OUT)),
        ("app-env-values", str(APP_ENV_OUT)),
    ):
        emit_output(key, value)

    # Always state what was actually deployed where. When an `environments:` block
    # or a namespace convention is in play, this line is the difference between a
    # five-minute and a two-hour investigation.
    notice(
        f"rendered {len(variables)} variables and {len(secrets)} secrets for "
        f"{app} in namespace {namespace} @ {repository}:{image_tag}"
    )


def deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


if __name__ == "__main__":
    main()
