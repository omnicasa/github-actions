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

# Must match actions/resolve-target/target.py's ENV_OVERRIDABLE. Imported rather than
# retyped where possible — this script also runs from a laptop against a checkout of
# only the app repo, so it falls back to a literal copy when the action is not there.
try:
    sys.path.insert(
        0, str(Path(__file__).resolve().parent.parent / "actions" / "resolve-target")
    )
    from target import ENV_OVERRIDABLE as _OVERRIDABLE

    ENV_OVERRIDABLE = set(_OVERRIDABLE)
except ImportError:  # pragma: no cover - only when run outside this repo
    ENV_OVERRIDABLE = {"app", "namespace", "repositoryPrefix", "tlsSecretName", "enabled"}
KNOWN_TOP_LEVEL = {
    "app", "namespace", "build", "workingDirectory", "domainVar", "tlsSecretName",
    "ingress", "required", "env", "environments", "repositoryPrefix",
    "buildArgs",
    "helmValues",
    "secretSources",
    # Top level, this pauses every environment at once — a one-line way to stop a
    # repo deploying during an incident without deleting the workflow. Per
    # environment it lives under environments.<env>.enabled.
    "enabled",
}
KNOWN_SECRET_SOURCE_PROVIDERS = {"doppler"}
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


def check_secret_mounts(path: Path, data: dict, names: list[str]) -> None:
    """Confirm the Dockerfile mounts every declared build secret.

    The manifest lives at <repo>/.github/deploy-manifest.yml and the build context
    defaults to the repo root, so the Dockerfile is findable from here in the normal
    layout. When it is not — a manifest passed from somewhere else, a custom
    `dockerfile:` input on the caller — fall back to an advisory warning rather than
    guessing, because a false ERROR here would block a correct repo.
    """
    repo_root = path.resolve().parent.parent
    working_dir = str(data.get("workingDirectory") or ".").strip() or "."
    dockerfile = repo_root / working_dir / "Dockerfile"
    if not dockerfile.is_file():
        warn(
            f"{path}: could not find {dockerfile} to check that buildArgs.secrets are "
            "mounted — confirm by hand that the Dockerfile has "
            "`RUN --mount=type=secret,id=<NAME>` for: " + ", ".join(names)
        )
        return

    try:
        content = dockerfile.read_text()
    except OSError as exc:
        warn(f"{path}: could not read {dockerfile}: {exc}")
        return

    missing = [n for n in names if f"id={n}" not in content]
    if missing:
        err(
            f"{path}: buildArgs.secrets declares {', '.join(missing)} but "
            f"{dockerfile.name} has no `--mount=type=secret,id=<NAME>` for it — the "
            "value would be rendered and then ignored, and the build would succeed "
            "without it"
        )

    # A secret mount needs BuildKit's dockerfile frontend. Without the directive an
    # older builder rejects the syntax outright, which at least fails loudly — but
    # buildx defaults vary, so say it here rather than at 3am.
    if not content.lstrip().startswith("# syntax="):
        warn(
            f"{path}: {dockerfile.name} uses secret mounts but has no `# syntax=` "
            "directive on line 1; add `# syntax=docker/dockerfile:1.7`"
        )


def check_secret_sources(path: Path, data: dict) -> None:
    sources = data.get("secretSources")
    if sources is None:
        return
    if not isinstance(sources, list):
        err(f"{path}: 'secretSources' must be a list")
        return

    doppler_count = 0
    for i, source in enumerate(sources):
        where = f"secretSources[{i}]"
        if not isinstance(source, dict):
            err(f"{path}: {where} must be a mapping")
            continue
        provider = source.get("provider")
        if provider not in KNOWN_SECRET_SOURCE_PROVIDERS:
            err(
                f"{path}: {where}.provider must be one of "
                f"{sorted(KNOWN_SECRET_SOURCE_PROVIDERS)}, not {provider!r}"
            )
            continue
        doppler_count += 1

        if not source.get("project"):
            err(f"{path}: {where}.project is required")

        configs = source.get("configs")
        if not isinstance(configs, dict) or not configs:
            err(
                f"{path}: {where}.configs must be a non-empty mapping of "
                "GitHub Environment -> Doppler config"
            )

        on_error = source.get("onError", "fail")
        if on_error not in ("fail", "warn"):
            err(f"{path}: {where}.onError must be 'fail' or 'warn', not {on_error!r}")

    if doppler_count > 1:
        err(f"{path}: secretSources has {doppler_count} 'doppler' entries — only one is supported")


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

    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            err(
                f"{path}: 'enabled' must be a YAML boolean (true/false, unquoted), "
                f"not {data['enabled']!r}"
            )
        elif data["enabled"] is False:
            warn(f"{path}: 'enabled' is false at the top level — NO environment deploys")

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

    build_args = data.get("buildArgs") or {}
    if not isinstance(build_args, dict):
        err(f"{path}: 'buildArgs' must be a mapping")
        build_args = {}
    for key in sorted(set(build_args) - {"variables", "secrets"}):
        warn(f"{path}: unknown key 'buildArgs.{key}' (typo? it will be ignored)")

    build_arg_vars = build_args.get("variables") or []
    build_arg_secrets = build_args.get("secrets") or []
    for field, value in (
        ("buildArgs.variables", build_arg_vars),
        ("buildArgs.secrets", build_arg_secrets),
    ):
        if not isinstance(value, list):
            err(f"{path}: '{field}' must be a list")
            continue
        for name in value:
            if not ENV_NAME.match(str(name)):
                err(f"{path}: {field} contains an invalid build arg name: {name!r}")
        dupes = {n for n in value if list(value).count(n) > 1}
        if dupes:
            warn(f"{path}: {field} lists duplicates: {', '.join(sorted(dupes))}")

    if isinstance(build_arg_vars, list) and isinstance(build_arg_secrets, list):
        # The two reach the Dockerfile by different mechanisms — an ARG versus a file
        # under /run/secrets — so a name in both means the Dockerfile can only be
        # right about one of them.
        overlap = sorted(set(build_arg_vars) & set(build_arg_secrets))
        if overlap:
            err(
                f"{path}: declared as both a build arg variable and a build arg secret: "
                f"{', '.join(overlap)} — pick one"
            )
        # A build arg is recorded in the image metadata and the build cache. Anything
        # the app also treats as a secret must not go through that path.
        leaked = sorted(set(build_arg_vars) & set(secrets or []))
        if leaked:
            err(
                f"{path}: {', '.join(leaked)} is in env.secrets but passed as a build "
                "arg variable — build args are readable by anyone who can pull the "
                "image; move it to buildArgs.secrets"
            )
        # Every buildArgs.secrets entry needs a matching RUN --mount=type=secret,id=<NAME>
        # in the Dockerfile, or the value is rendered, mounted and then silently
        # ignored — a green build producing an image built without it.
        if build_arg_secrets:
            check_secret_mounts(path, data, sorted(build_arg_secrets))

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

    check_secret_sources(path, data)

    # The chart reads migration.* from deploy/values.yaml. A copy here is a second
    # switch that reads as authoritative and has no effect at all.
    if "migration" in data:
        err(
            f"{path}: 'migration' belongs in deploy/values.yaml, not the manifest — "
            "the chart reads it from there and ignores this copy"
        )

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

        # `enabled: "false"` is a non-empty string, which is truthy in the workflow
        # expression a reader would expect to gate on. Insist on a real YAML boolean.
        if "enabled" in overrides and not isinstance(overrides["enabled"], bool):
            err(
                f"{path}: environments.{env_name}.enabled must be a YAML boolean "
                f"(true/false, unquoted), not {overrides['enabled']!r}"
            )
        if overrides.get("enabled") is False and len(overrides) > 1:
            warn(
                f"{path}: environments.{env_name} is disabled, so its other overrides "
                "({}) have no effect".format(
                    ", ".join(sorted(set(overrides) - {"enabled"}))
                )
            )


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
