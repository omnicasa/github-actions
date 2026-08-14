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

MANIFEST = Path(os.environ.get("DEPLOY_MANIFEST", ".github/deploy-manifest.yml"))
RELEASE_OUT = Path(os.environ.get("RELEASE_VALUES_OUT", "/tmp/release-values.json"))
APP_ENV_OUT = Path(os.environ.get("APP_ENV_VALUES_OUT", "/tmp/app-env-values.json"))

# `${NAME}` references inside `env.derived` values.
DERIVED_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# The only keys an `environments.<name>` block may override. Deliberately short:
# letting an environment redefine its env-var allowlist is how omnicasa-payload's
# ci_dev.yaml drifted into deploying with the wrong environment's secrets.
ENV_OVERRIDABLE = ("app", "namespace", "repositoryPrefix", "tlsSecretName")


def fail(msg: str) -> "NoReturn":  # noqa: F821
    """Annotate the failure so it lands on the workflow summary, then stop."""
    print(f"::error::{msg}", file=sys.stderr)
    sys.exit(1)


def notice(msg: str) -> None:
    print(f"::notice::{msg}")


def warn(msg: str) -> None:
    print(f"::warning::{msg}")


def emit_output(key: str, value: str) -> None:
    """Publish a step output when running inside Actions; no-op locally."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{key}={value}\n")


def load_manifest(path: Path) -> dict:
    if not path.is_file():
        fail(f"{path} not found — the deploy workflow requires a deploy manifest")
    text = path.read_text()
    try:
        import yaml  # available on GitHub-hosted ubuntu runners

        data = yaml.safe_load(text)
    except ImportError:
        fail("PyYAML is not installed on this runner; add a `pip install pyyaml` step")
    except Exception as exc:  # malformed YAML
        fail(f"{path} is not valid YAML: {exc}")
    if not isinstance(data, dict):
        fail(f"{path} must contain a YAML mapping at the top level")
    return data


def apply_environment_overrides(manifest: dict, environment: str) -> dict:
    """Fold `environments.<environment>` into the top level of the manifest.

    Exists so a repo with a deviating dev environment (different app name,
    different namespace, images pushed under a different registry prefix) needs
    one block here rather than a second copy of the whole workflow.
    """
    environments = manifest.get("environments") or {}
    if not isinstance(environments, dict):
        fail("manifest key 'environments' must be a mapping of name -> overrides")
    overrides = environments.get(environment)
    if not overrides:
        return manifest
    if not isinstance(overrides, dict):
        fail(f"manifest key 'environments.{environment}' must be a mapping")

    unknown = sorted(set(overrides) - set(ENV_OVERRIDABLE))
    if unknown:
        fail(
            f"environments.{environment} may only override "
            + ", ".join(ENV_OVERRIDABLE)
            + " — not "
            + ", ".join(unknown)
        )

    merged = dict(manifest)
    merged.update(overrides)
    notice(
        f"environment '{environment}' overrides: "
        + ", ".join(f"{k}={overrides[k]}" for k in sorted(overrides))
    )
    return merged


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
    environment = os.environ.get("ENVIRONMENT", "").strip()

    manifest = load_manifest(MANIFEST)
    manifest = apply_environment_overrides(manifest, environment)

    gh_vars = load_context("VARS_JSON")
    gh_secrets = load_context("SECRETS_JSON")

    app = str(manifest.get("app") or "").strip()
    if not app:
        fail(f"{MANIFEST}: 'app' is required (the Helm release and app name)")

    namespace = str(manifest.get("namespace") or "").strip()
    if not namespace:
        fail(f"{MANIFEST}: 'namespace' is required (pre-created; CI never creates it)")

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

    prefix = str(manifest.get("repositoryPrefix") or environment).strip()
    repository = os.environ.get("REPOSITORY", "").strip() or (
        f"{prefix}/{app}" if prefix else app
    )

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

    migration = manifest.get("migration") or {}
    migration_enabled = bool(migration.get("enabled")) if isinstance(migration, dict) else False

    for key, value in (
        ("app", app),
        ("namespace", namespace),
        ("repository", repository),
        ("image", f"{registry}/{repository}:{image_tag}"),
        ("migration-enabled", "true" if migration_enabled else "false"),
        ("release-values", str(RELEASE_OUT)),
        ("app-env-values", str(APP_ENV_OUT)),
    ):
        emit_output(key, value)

    # Always state what was actually deployed where. When an `environments:` block
    # is in play, this line is the difference between a five-minute and a two-hour
    # investigation.
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
