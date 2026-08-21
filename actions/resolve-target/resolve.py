#!/usr/bin/env python3
"""Emit the deploy target for one environment as step outputs.

Runs before anything touches a cluster or a registry, so the answers the build
job and the deploy job work from are computed once, in one place, from one file.

Prints nothing but names — no `vars`, no `secrets`, nothing that could leak.
"""

from __future__ import annotations

from target import emit_output, notice, resolve_from_env


def main() -> None:
    target = resolve_from_env()

    for key, value in (
        ("enabled", "true" if target.enabled else "false"),
        ("app", target.app),
        ("namespace", target.namespace),
        ("repository", target.repository),
        ("repository-prefix", target.repository_prefix),
    ):
        emit_output(key, value)

    if not target.enabled:
        # A skipped job with a green tick and no explanation is the thing people
        # file bugs about, so say it in the log as well as in the job graph.
        notice(
            f"environment '{target.environment}' is disabled for this repo "
            "(environments.<env>.enabled: false in the deploy manifest) — "
            "build and deploy will be skipped"
        )
        return

    notice(
        f"target: {target.app} in namespace {target.namespace} "
        f"@ {target.repository} (environment {target.environment})"
    )


if __name__ == "__main__":
    main()
