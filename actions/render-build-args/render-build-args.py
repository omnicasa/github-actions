#!/usr/bin/env python3
"""Render the docker build inputs for one environment.

Reads `.github/deploy-manifest.yml` plus the whole GitHub `vars` and `secrets`
contexts (as JSON, via env), and emits two step outputs for
docker/build-push-action:

  build-args    multiline KEY=value, from manifest `buildArgs.variables`
  secret-files  multiline id=KEY,src=<path>, from manifest `buildArgs.secrets`

WHY THIS EXISTS AT ALL. A caller repo cannot pass environment-scoped values to a
reusable workflow. `jobs.<id>.environment` is not allowed on a job that uses
`uses:`, so every expression in that job's `with:` resolves at repository and
organization scope only — an environment variable reads as empty. The build job
inside the reusable workflow *does* declare `environment:`, so this action runs
where `vars` and `secrets` are the target environment's. Anything an app must
bake in per environment has to be resolved here or not at all.

WHY variables AND secrets ARE SEPARATE. A `--build-arg` is recorded in the image
metadata and in the build cache, readable by anyone who can pull the image. A
buildx `--secret` is a file mounted for the life of one RUN and is recorded
nowhere. Credentials therefore go through `buildArgs.secrets`, which needs a
matching `RUN --mount=type=secret,id=<NAME>` in the app's Dockerfile.

WHAT A BUILD ARG COSTS. Baking an environment's value into the image pins that
image to that environment: two environments then build the same commit twice and
build-once-promote is off the table for that app. Prefer runtime configuration;
reach for `buildArgs` only for values a build genuinely cannot defer, such as
anything a bundler inlines.

SECURITY INVARIANT: this script prints key *names* only, never values. The
`secrets` context is passed in wholesale, so any print of a value would leak
every secret into the job log. Keep every diagnostic on the name side. Keys
absent from the manifest allowlist are ignored outright — `github_token`
included.
"""

from __future__ import annotations

import json
import os
import secrets as _secrets
import stat
import sys
from pathlib import Path

# Sibling action directory. Both actions are checked out from the same ref of this
# repo, side by side, so this resolves both in the runner's _actions cache and in a
# plain clone (which is how CI exercises this file).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "resolve-target"))

from target import fail, notice, resolve_from_env, warn  # noqa: E402


def load_context(var_name: str) -> dict[str, str]:
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
    # Empty strings mean "declared but not set" — treat them as absent so the build
    # falls back to the Dockerfile's own ARG default instead of baking in "".
    return {k: v for k, v in data.items() if isinstance(v, str) and v != ""}


def as_list(manifest: dict, *path: str) -> list[str]:
    node: object = manifest
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    if node is None:
        return []
    if not isinstance(node, list):
        fail("manifest key '" + ".".join(path) + "' must be a list of names")
    return [str(x) for x in node]


def emit_multiline(key: str, value: str) -> None:
    """Publish a multiline step output; no-op locally.

    A random delimiter, not a fixed one: the value is attacker-influenced only in
    the sense that it is app configuration, but a value containing the delimiter
    would otherwise let one build arg terminate the block and inject the rest as
    further outputs.
    """
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    delimiter = f"ghadelim_{_secrets.token_hex(16)}"
    if delimiter in value:  # unreachable in practice; cheap to assert
        fail(f"could not emit {key}: generated delimiter collided with the value")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def parse_extra_args() -> dict[str, str]:
    """Parse the workflow's own `build-args` input into name -> KEY=value.

    Merged here rather than concatenated in the workflow so docker is handed one
    clean block: a `|` list built from two expressions leaves a blank line whenever
    either is empty, and relies on the build action tolerating it.
    """
    out: dict[str, str] = {}
    for line in os.environ.get("EXTRA_BUILD_ARGS", "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            fail(f"build-args input line is not KEY=value: {line!r}")
        out[line.split("=", 1)[0]] = line
    return out


def main() -> None:
    target = resolve_from_env()
    manifest = target.manifest

    arg_names = as_list(manifest, "buildArgs", "variables")
    secret_names = as_list(manifest, "buildArgs", "secrets")

    # Both lists feed the same Dockerfile namespace, and the two paths disagree
    # about how the value arrives (ARG vs /run/secrets/<id>). A name in both means
    # the Dockerfile can only be right about one of them.
    both = sorted(set(arg_names) & set(secret_names))
    if both:
        fail(
            "buildArgs: " + ", ".join(both) + " is in both `variables` and `secrets` — "
            "pick one, they reach the Dockerfile by different mechanisms"
        )

    extra = parse_extra_args()

    if not arg_names and not secret_names:
        if extra:
            notice(
                f"no buildArgs declared for environment '{target.environment}'; "
                "passing the workflow input through: " + ", ".join(sorted(extra))
            )
        else:
            notice(f"no buildArgs declared for environment '{target.environment}'")
        emit_multiline("build-args", "\n".join(extra.values()))
        emit_multiline("secret-files", "")
        return

    variables = load_context("VARS_JSON")
    secrets_ctx = load_context("SECRETS_JSON")

    # A name declared as a build-arg variable but stored as a secret would be
    # masked in the log while still being written into image metadata — the worst
    # of both. Fail rather than warn: unlike the env.variables case this one
    # actively publishes the value.
    misfiled = sorted(n for n in arg_names if n not in variables and n in secrets_ctx)
    if misfiled:
        fail(
            "buildArgs.variables: " + ", ".join(misfiled) + " is stored as a secret. A "
            "build arg is recorded in the image metadata, so move it to "
            "buildArgs.secrets or store it as a variable — not both ways at once."
        )

    # The workflow input first so its order is stable, then the manifest's — which
    # overwrites on a name in both, because only the manifest is resolved at the
    # target environment's scope.
    merged: dict[str, str] = dict(extra)
    missing_args: list[str] = []
    for name in arg_names:
        if name in variables:
            merged[name] = f"{name}={variables[name]}"
        else:
            missing_args.append(name)

    shadowed = sorted(set(extra) & set(arg_names) & set(variables))
    if shadowed:
        warn(
            "the build-args workflow input is overridden by the manifest for: "
            + ", ".join(shadowed)
            + " — remove it from the caller, the manifest value is the environment's"
        )
    build_args = list(merged.values())

    # Mirrors render-values.py: a secret declared here but stored as a var still
    # works, and that is exactly the failure mode to surface — the value reaches
    # the build as a secret while being readable in the settings UI.
    plaintext = sorted(n for n in secret_names if n not in secrets_ctx and n in variables)
    if plaintext:
        warn(
            "buildArgs.secrets stored as variables, readable in the log and the "
            "settings UI: " + ", ".join(plaintext)
        )

    # Exactly the directory the workflow's shred step removes — keep the two in step.
    secrets_dir = Path(
        os.environ.get("SECRETS_DIR")
        or f"{os.environ.get('RUNNER_TEMP') or '/tmp'}/build-secrets"
    )
    secrets_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(secrets_dir, stat.S_IRWXU)

    secret_files: list[str] = []
    missing_secrets: list[str] = []
    for name in secret_names:
        value = secrets_ctx.get(name, variables.get(name))
        if value is None:
            missing_secrets.append(name)
            continue
        # No trailing newline: the Dockerfile reads these with `cat`, and a stray
        # newline in a token is invisible in a log and breaks the auth it is used for.
        dest = secrets_dir / name
        dest.write_text(value, encoding="utf-8")
        os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR)
        secret_files.append(f"id={name},src={dest}")

    for name in missing_args + missing_secrets:
        warn(
            f"buildArg {name} is not set for environment '{target.environment}' — "
            "omitted, so the Dockerfile's own ARG default applies"
        )

    notice(
        f"build args for '{target.environment}': "
        + (", ".join(sorted(n for n in arg_names if n not in missing_args)) or "none")
        + " | build secrets: "
        + (", ".join(sorted(n for n in secret_names if n not in missing_secrets)) or "none")
    )

    emit_multiline("build-args", "\n".join(build_args))
    emit_multiline("secret-files", "\n".join(secret_files))


if __name__ == "__main__":
    main()
