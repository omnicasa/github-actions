#!/usr/bin/env python3
"""Fetch this environment's Doppler secrets via GitHub OIDC, for render-values to merge in.

No static Doppler token: the job's own OIDC token is exchanged for a short-lived
Doppler one (POST /v3/auth/oidc), scoped by the identity's trust rule and Doppler
project-membership. See docs/env-contract.md for the manifest's secretSources block.

SECURITY INVARIANT: prints key names only, never a value — mask every line of a
multi-line secret, or lines after the first leak in plain text.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "resolve-target"))

from target import PLATFORM_KEYS, emit_output, fail, load_manifest, notice, warn  # noqa: E402

DOPPLER_API = "https://api.doppler.com/v3"
KNOWN_PROVIDERS = {"doppler"}


def mask(value: str) -> None:
    for line in value.splitlines():
        if line:
            print(f"::add-mask::{line}")


def request_json(url: str, *, method: str = "GET", headers: dict | None = None, data: bytes | None = None) -> dict:
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        fail(f"{method} {url.split('?')[0]} -> HTTP {exc.code}: {body[:300]}")
    except urllib.error.URLError as exc:
        fail(f"{method} {url.split('?')[0]} -> {exc.reason}")


def github_oidc_token(audience: str) -> str:
    request_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    request_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not request_url or not request_token:
        fail(
            "no OIDC token available — the caller job needs "
            "`permissions: id-token: write` to fetch Doppler secrets"
        )
    sep = "&" if "?" in request_url else "?"
    resp = request_json(
        f"{request_url}{sep}audience={audience}",
        headers={"Authorization": f"Bearer {request_token}"},
    )
    return resp["value"]


def doppler_token(identity_id: str, github_token: str) -> str:
    body = json.dumps({"identity": identity_id, "token": github_token}).encode()
    resp = request_json(
        f"{DOPPLER_API}/auth/oidc",
        method="POST",
        headers={"Content-Type": "application/json"},
        data=body,
    )
    return resp["token"]


def find_doppler_source(manifest: dict) -> dict | None:
    sources = manifest.get("secretSources") or []
    if not isinstance(sources, list):
        fail("manifest key 'secretSources' must be a list")

    unknown = sorted(
        {str(s.get("provider")) for s in sources if isinstance(s, dict) and s.get("provider") not in KNOWN_PROVIDERS}
    )
    if unknown:
        fail(f"secretSources: unsupported provider(s) {', '.join(unknown)} — only 'doppler' is implemented")

    doppler_sources = [s for s in sources if isinstance(s, dict) and s.get("provider") == "doppler"]
    if len(doppler_sources) > 1:
        fail("secretSources: only one 'doppler' entry is supported")
    return doppler_sources[0] if doppler_sources else None


def skip(reason: str) -> NoReturn:
    notice(reason)
    emit_output("secrets-file", "")
    sys.exit(0)


def main() -> None:
    manifest = load_manifest(Path(os.environ.get("DEPLOY_MANIFEST", ".github/deploy-manifest.yml")))
    environment = os.environ.get("ENVIRONMENT", "").strip()

    source = find_doppler_source(manifest)
    if source is None:
        skip("no secretSources.doppler entry in the manifest — tier 2 (Doppler) is off")

    on_error = str(source.get("onError", "fail")).strip().lower()
    if on_error not in ("fail", "warn"):
        fail(f"secretSources.doppler.onError must be 'fail' or 'warn', not {on_error!r}")

    def give_up(msg: str) -> NoReturn:
        if on_error == "fail":
            fail(msg)
        skip(f"{msg} — continuing without tier 2 (onError: warn)")

    project = str(source.get("project") or "").strip()
    if not project:
        fail("secretSources.doppler.project is required")

    configs = source.get("configs") or {}
    if not isinstance(configs, dict):
        fail("secretSources.doppler.configs must be a mapping of GitHub Environment -> Doppler config")
    config = configs.get(environment)
    if not config:
        skip(
            f"secretSources.doppler.configs has no entry for environment "
            f"'{environment}' — tier 2 is off for this deploy"
        )

    identity_id = os.environ.get("DOPPLER_IDENTITY_ID", "").strip()
    if not identity_id:
        give_up(
            f"secretSources.doppler.configs.{environment} = {config!r}, but "
            "vars.DOPPLER_IDENTITY_ID is not set for this environment"
        )

    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    gh_token = github_oidc_token(f"https://github.com/{owner}")
    mask(gh_token)

    dp_token = doppler_token(identity_id, gh_token)
    mask(dp_token)

    url = f"{DOPPLER_API}/configs/config/secrets/download?project={project}&config={config}&format=json"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {dp_token}"})
        with urllib.request.urlopen(req) as resp:
            secrets = json.load(resp)
    except urllib.error.HTTPError as exc:
        give_up(f"fetching {project}/{config} from Doppler failed: HTTP {exc.code}")

    if not isinstance(secrets, dict):
        give_up(f"Doppler returned a non-object payload for {project}/{config}")

    # Doppler injects these into every config; they name Doppler's own coordinates,
    # never an app value, so they only ever collide with an app key by accident.
    secrets = {k: v for k, v in secrets.items() if not k.startswith("DOPPLER_")}

    denied = sorted(set(secrets) & PLATFORM_KEYS)
    if denied:
        warn(
            "secretSources.doppler config carries platform key name(s), dropped: "
            + ", ".join(denied)
        )
        secrets = {k: v for k, v in secrets.items() if k not in PLATFORM_KEYS}

    for value in secrets.values():
        if isinstance(value, str):
            mask(value)

    output_file = Path(os.environ.get("OUTPUT_FILE", "/tmp/doppler-secrets.json"))
    output_file.write_text(json.dumps(secrets))
    emit_output("secrets-file", str(output_file))
    notice(f"fetched {len(secrets)} key(s) from Doppler {project}/{config} for environment '{environment}'")


if __name__ == "__main__":
    main()
