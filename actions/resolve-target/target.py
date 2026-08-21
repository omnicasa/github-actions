#!/usr/bin/env python3
"""Resolve a deploy target from the manifest: is it on, what is it called, where.

The environment name is the hinge of the whole pipeline — it is the GitHub
Environment, the registry path prefix and the key into the manifest's
`environments:` block at the same time. Three places used to answer "which
namespace, which repository?" from a hand-rolled copy of that logic: the build
job's inline python, the rollback workflow's inline python, and
`render-values.py`. This module is the one implementation all of them import,
because a build that pushes to `staging/app` while the deploy pulls
`prodtest/app` fails in the cluster, minutes later, as ImagePullBackOff.

Two conventions live here, and only here:

* **Namespace prefix.** `dev` and `prodtest` are second environments on a cluster
  that already hosts the app, so they get a prefixed namespace — `dev-<app>` on
  stage-k8s beside `<app>`, `prodtest-<app>` on prod-k8s beside `<app>`. This is
  a default, not a rule: `environments.<env>.namespace` is still taken verbatim.

* **Registry prefix.** Normally the environment name, so an image ref states the
  environment it was built for. `prodtest` is the exception: it deploys the
  artifact staging already built and tested, so it reads from `staging/<app>`
  rather than rebuilding the same commit under its own prefix.

An environment absent from both tables behaves exactly as it did before this
module existed, which is what keeps repos on the two-environment flow working
untouched.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import NamedTuple, NoReturn

# The only keys an `environments.<name>` block may override. Deliberately short:
# letting an environment redefine its env-var allowlist is how omnicasa-payload's
# ci_dev.yaml drifted into deploying with the wrong environment's secrets.
# `enabled` is on the list because turning an environment off has to live with the
# app facts — a repo with no prodtest cluster entry says so once, here, rather than
# by keeping a different caller workflow from every other repo.
ENV_OVERRIDABLE = ("app", "namespace", "repositoryPrefix", "tlsSecretName", "enabled")

# Environments that share a cluster with another environment of the same app, and so
# cannot share its namespace.
NAMESPACE_PREFIX = {
    "dev": "dev-",
    "prodtest": "prodtest-",
}

# Environments whose image is promoted rather than built. Absent means "the
# environment name is the prefix", which is the rule for everything else.
REPOSITORY_PREFIX = {
    "prodtest": "staging",
}


class Target(NamedTuple):
    """Everything the pipeline needs to know before it talks to a cluster."""

    manifest: dict  # with environments.<env> already folded in
    environment: str
    app: str
    namespace: str
    repository_prefix: str
    repository: str
    enabled: bool


def fail(msg: str) -> NoReturn:
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


def apply_environment_overrides(manifest: dict, environment: str) -> tuple[dict, set[str]]:
    """Fold `environments.<environment>` into the top level of the manifest.

    Returns the merged manifest and the set of keys the environment actually
    overrode — the caller needs that second value to tell "this repo asked for
    namespace `foo`" apart from "this repo said nothing, apply the convention".
    """
    environments = manifest.get("environments") or {}
    if not isinstance(environments, dict):
        fail("manifest key 'environments' must be a mapping of name -> overrides")
    overrides = environments.get(environment)
    if not overrides:
        return manifest, set()
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
    return merged, set(overrides)


def _as_bool(value: object, where: str) -> bool:
    """Accept a YAML bool or the strings a workflow input would hand over."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0"):
        return False
    fail(f"{where} must be true or false, not {value!r}")


def resolve(manifest: dict, environment: str) -> Target:
    """Answer app / namespace / repository / enabled for one environment."""
    if not environment:
        fail("no environment given — it selects the credentials, namespace and image path")

    merged, overridden = apply_environment_overrides(manifest, environment)

    app = str(merged.get("app") or "").strip()
    if not app:
        fail("manifest: 'app' is required (the Helm release and app name)")

    base_namespace = str(merged.get("namespace") or "").strip()
    if not base_namespace:
        fail("manifest: 'namespace' is required (pre-created; CI never creates it)")

    # An explicit override is taken verbatim — the convention must never rewrite a
    # namespace someone deliberately named, because the namespace is what the
    # per-namespace RBAC and the pull secret were created against.
    if "namespace" in overridden:
        namespace = base_namespace
    else:
        namespace = NAMESPACE_PREFIX.get(environment, "") + base_namespace

    # Top-level repositoryPrefix still wins for every environment; it predates the
    # per-environment table and some repos push everything to one path.
    prefix = str(merged.get("repositoryPrefix") or "").strip()
    if not prefix:
        prefix = REPOSITORY_PREFIX.get(environment, environment)
    repository = f"{prefix}/{app}" if prefix else app

    enabled = _as_bool(merged.get("enabled", True), f"environments.{environment}.enabled")

    return Target(
        manifest=merged,
        environment=environment,
        app=app,
        namespace=namespace,
        repository_prefix=prefix,
        repository=repository,
        enabled=enabled,
    )


def resolve_from_env() -> Target:
    """Entry point for the composite action and for render-values.py."""
    manifest_path = Path(os.environ.get("DEPLOY_MANIFEST", ".github/deploy-manifest.yml"))
    environment = os.environ.get("ENVIRONMENT", "").strip()
    return resolve(load_manifest(manifest_path), environment)
