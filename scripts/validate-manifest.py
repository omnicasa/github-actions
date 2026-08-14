#!/usr/bin/env python3
"""Validate a deploy manifest without a cluster, a runner or any credentials.

    python3 scripts/validate-manifest.py [.github/deploy-manifest.yml ...]

Catches the mistakes that otherwise surface as a failed deploy or, worse, a green
deploy of a misconfigured release. Exits non-zero on any ERROR; WARNINGs are advisory.

This is deliberately separate from render-values.py: that one needs a populated
GitHub context and runs mid-deploy, this one runs on a laptop against the file alone.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError as exc:
    print("PyYAML required: python3 -m pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from exc

# Must match render-values.py's ENV_OVERRIDABLE.
ENV_OVERRIDABLE = {"app", "namespace", "repositoryPrefix", "tlsSecretName"}
KNOWN_TOP_LEVEL = {
    "app", "namespace", "build", "workingDirectory", "domainVar", "tlsSecretName",
    "ingress", "required", "env", "migration", "environments", "repositoryPrefix",
    "helmValues",
}
# Kubernetes names: RFC 1123 labels.
DNS_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DERIVED_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

errors: list[str] = []
warnings: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


def check(path: Path) -> None:
    try:
        data = yaml.safe_load(path.read_text())
    except Exception as exc:
        err(f"{path}: not valid YAML: {exc}")
        return
    if not isinstance(data, dict):
        err(f"{path}: top level must be a mapping")
        return

    for key in sorted(set(data) - KNOWN_TOP_LEVEL):
        warn(f"{path}: unknown top-level key '{key}' (typo? it will be ignored)")

    app = data.get("app")
    namespace = data.get("namespace")

    for field, value in (("app", app), ("namespace", namespace)):
        if not value:
            err(f"{path}: '{field}' is required")
        elif not DNS_NAME.match(str(value)):
            err(f"{path}: '{field}' must be a DNS-1123 name (lowercase, digits, -): {value!r}")
        elif len(str(value)) > 53:
            # The chart truncates names at 63; a Deployment's generated pod names
            # need the remaining characters.
            err(f"{path}: '{field}' is {len(str(value))} chars, keep it under 54")

    # app == namespace is the convention everywhere; a mismatch is legal but is
    # usually a copy-paste error from another repo's manifest.
    if app and namespace and app != namespace:
        warn(f"{path}: app ({app}) != namespace ({namespace}); intended?")

    env = data.get("env") or {}
    if not isinstance(env, dict):
        err(f"{path}: 'env' must be a mapping")
        env = {}

    variables = env.get("variables") or []
    secrets = env.get("secrets") or []
    derived = env.get("derived") or {}

    for field, value in (("env.variables", variables), ("env.secrets", secrets)):
        if not isinstance(value, list):
            err(f"{path}: '{field}' must be a list")
            continue
        for name in value:
            if not ENV_NAME.match(str(name)):
                err(f"{path}: {field} contains an invalid env var name: {name!r}")
        dupes = {n for n in value if list(value).count(n) > 1}
        if dupes:
            warn(f"{path}: {field} lists duplicates: {', '.join(sorted(dupes))}")

    if isinstance(variables, list) and isinstance(secrets, list):
        overlap = sorted(set(variables) & set(secrets))
        if overlap:
            err(
                f"{path}: declared as both variable and secret: {', '.join(overlap)} "
                "— the ConfigMap would carry the secret value in plain text"
            )

    if not isinstance(derived, dict):
        err(f"{path}: 'env.derived' must be a mapping")
        derived = {}
    declared = set(variables or []) | set(secrets or [])
    for name, template in derived.items():
        if not ENV_NAME.match(str(name)):
            err(f"{path}: env.derived has an invalid name: {name!r}")
        refs = set(DERIVED_REF.findall(str(template)))
        if not refs:
            warn(f"{path}: env.derived.{name} has no ${{...}} reference; is it really derived?")
        leaked = sorted(refs & set(secrets or []))
        if leaked:
            err(
                f"{path}: env.derived.{name} is built from secret(s) {', '.join(leaked)} "
                "— derived values land in the ConfigMap in plain text"
            )
        if name in declared:
            err(f"{path}: env.derived.{name} is also declared in env.variables/secrets")

    domain_var = data.get("domainVar")
    required = data.get("required") or []
    if not isinstance(required, list):
        err(f"{path}: 'required' must be a list")
        required = []

    ingress = data.get("ingress") or {}
    if not isinstance(ingress, dict):
        err(f"{path}: 'ingress' must be a mapping")
        ingress = {}
    managed = ingress.get("managed", True)

    if domain_var:
        if domain_var not in required:
            # Unset, it renders a structurally valid Ingress with no host — a green
            # deploy nobody can reach.
            warn(f"{path}: domainVar {domain_var} is not in 'required'; add it")
    elif managed:
        warn(f"{path}: no domainVar set — the release will have no ingress host")

    for name in required:
        if name != domain_var and name not in declared:
            warn(
                f"{path}: required key {name} is not declared in env.variables/secrets, "
                "so it is checked but never passed to the app"
            )

    if not managed and not data.get("tlsSecretName"):
        warn(f"{path}: ingress.managed is false — deploy/values.yaml must define the whole Ingress")

    if "helmValues" in data:
        warn(
            f"{path}: 'helmValues' is deprecated and outranks deploy/values.yaml; "
            "move those keys into deploy/values.yaml"
        )

    migration = data.get("migration") or {}
    if not isinstance(migration, dict):
        err(f"{path}: 'migration' must be a mapping")
    elif migration.get("enabled") and not migration.get("command"):
        err(f"{path}: migration.enabled is true but migration.command is empty")

    environments = data.get("environments") or {}
    if not isinstance(environments, dict):
        err(f"{path}: 'environments' must be a mapping")
        environments = {}
    for env_name, overrides in environments.items():
        if not isinstance(overrides, dict):
            err(f"{path}: environments.{env_name} must be a mapping")
            continue
        unknown = sorted(set(overrides) - ENV_OVERRIDABLE)
        if unknown:
            err(
                f"{path}: environments.{env_name} may only override "
                f"{', '.join(sorted(ENV_OVERRIDABLE))} — not {', '.join(unknown)}"
            )
        for field in ("app", "namespace"):
            value = overrides.get(field)
            if value and not DNS_NAME.match(str(value)):
                err(f"{path}: environments.{env_name}.{field} is not a DNS-1123 name: {value!r}")


def main() -> int:
    paths = [Path(p) for p in sys.argv[1:]] or [Path(".github/deploy-manifest.yml")]
    for path in paths:
        if not path.is_file():
            err(f"{path}: not found")
            continue
        check(path)

    for msg in warnings:
        print(f"warn : {msg}")
    for msg in errors:
        print(f"ERROR: {msg}")

    print(f"\n{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
