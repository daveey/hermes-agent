#!/usr/bin/env python3
"""Strict CI aggregate gate (shared by ci.yaml and the stable orchestrator).

Behavior tests: tests/ci/test_required_results.py

Input: the JSON ``toJSON(needs)`` of the all-checks-pass job on stdin.
Failures always fail the gate. A skipped or missing update/E2E consumer also
fails whenever ``detect.outputs`` selects its lane. Other skips additionally
fail in release mode unless the job is in :data:`EXCLUDED_JOBS` (the deferred
Desktop E2E). The PR-only checks are steps of ``repo-guards``, a job that runs
on every event, so none of them reaches the gate as a skip. The OSV scan is
advisory in its findings only — its execution is required.

    echo "$NEEDS" | python3 scripts/ci/required_results.py [--release]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

# The deferred Desktop E2E: the only skip a strict run tolerates.
DEFERRED_JOBS = ("e2e-desktop",)
# [daveey] fork-ci-runner: ci.yaml turns these off with ``if: false`` (no macOS/Windows runner), so a
# strict release run that required them could never pass the gate however green every lane was.
FORK_DISABLED_JOBS = ("tests-os",)
EXCLUDED_JOBS = frozenset((*DEFERRED_JOBS, *FORK_DISABLED_JOBS))

NEEDS_JSON_OUTPUT = "needs-json"

# Reusable-workflow parents of the update/E2E consumers, and of the real
# remote-backend and catalog lanes. The actual detect outputs already arrive in
# toJSON(needs); a selected consumer cannot skip merely because this is a PR
# rather than a release.
_UPDATE_CONSUMER_LANES = {
    "tests": ("python", "e2e", "e2e_upgrade"),
    "e2e-desktop-update": ("e2e_desktop_update",),
    "remote-backends": ("remote_backends",),
    "catalog-tests": ("catalog",),
    "hermes-state-macos": ("hermes_state",),
}

# ci.yaml's ``macOS state required (v1)`` consumer: the macOS state lane and its selector.
MACOS_STATE_JOB = "hermes-state-macos"
MACOS_STATE_LANE = "hermes_state"


def evaluate_macos_state(needs: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
    """Verdict for ``macOS state required``: ``ok`` plus a one-line ``reason``.

    A selected lane must succeed; only an explicitly unselected one may skip.
    A detect that did not succeed, or a selection that is neither ``'true'`` nor
    ``'false'``, fails closed: whether the lane was required is unknown.
    """
    entries = needs or {}
    detect = entries.get("detect") or {}
    if detect.get("result") != "success":
        return {"ok": False, "reason": f"detect {detect.get('result') or 'missing'}: lane selection unknown"}
    selected = (detect.get("outputs") or {}).get(MACOS_STATE_LANE)
    lane = (entries.get(MACOS_STATE_JOB) or {}).get("result") or "missing"
    if selected == "true":
        return {"ok": lane == "success", "reason": f"{MACOS_STATE_LANE} selected, {MACOS_STATE_JOB} {lane}"}
    if selected == "false":
        return {"ok": lane in ("skipped", "success"),
                "reason": f"{MACOS_STATE_LANE} not selected, {MACOS_STATE_JOB} {lane}"}
    return {"ok": False, "reason": f"{MACOS_STATE_LANE} selection {selected!r}: lane selection unknown"}


def evaluate_gate(
    needs: dict[str, dict[str, Any]] | None,
    release: bool = False,
) -> dict[str, Any]:
    """Verdict for a ``needs`` context; see the module docstring.

    Returns ``ok``, plus sorted ``failed`` (every non-success, non-allowed
    entry) and ``allowed_skips``.
    """
    failed: list[str] = []
    allowed_skips: list[str] = []
    entries = needs or {}
    lanes = (entries.get("detect") or {}).get("outputs") or {}
    required = {job for job, keys in _UPDATE_CONSUMER_LANES.items()
                if any(lanes.get(key) in ("true", True) for key in keys)}
    if not entries:
        failed.append("<no-needs>")
    failed.extend(required - entries.keys())
    for name, info in entries.items():
        result = (info or {}).get("result")
        if result == "success":
            continue
        if result == "skipped" and name not in required and (not release or name in EXCLUDED_JOBS):
            allowed_skips.append(name)
            continue
        failed.append(name)
    return {
        "ok": not failed,
        "failed": sorted(failed),
        "allowed_skips": sorted(allowed_skips),
    }


def compact_results(needs: dict[str, dict[str, Any]] | None) -> dict[str, str]:
    """{job_name: result} for every entry, for the PR comment assembler."""
    return {name: (info or {}).get("result", "") for name, info in (needs or {}).items()}


def render_report(needs: dict[str, dict[str, Any]] | None, verdict: dict[str, Any]) -> list[str]:
    """Per-job lines plus ::error:: annotations for the failed set."""
    lines: list[str] = []
    for name in sorted(needs or {}):
        result = (needs[name] or {}).get("result", "unknown")
        icon = {"success": "✅", "skipped": "⏭️"}.get(result, "❌")
        lines.append(f"{icon} {name}: {result}")
    if verdict["failed"]:
        failed = verdict["failed"]
        lines.append(f"::error::{len(failed)} job(s) failed: {', '.join(failed)}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--release",
        action="store_true",
        help="Strict mode: a skipped excluded job is the only tolerated skip.",
    )
    parser.add_argument(
        "--macos-state",
        action="store_true",
        help="Judge only the macOS state lane (ci.yaml's macOS state required check).",
    )
    args = parser.parse_args(argv)

    needs = json.load(sys.stdin)
    if args.macos_state:
        verdict = evaluate_macos_state(needs)
        print(("" if verdict["ok"] else "::error::") + verdict["reason"])
        return 0 if verdict["ok"] else 1
    verdict = evaluate_gate(needs, release=args.release)
    compact = compact_results(needs)

    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as fh:
            fh.write(f"{NEEDS_JSON_OUTPUT}={json.dumps(compact)}\n")

    print(f"{NEEDS_JSON_OUTPUT}={json.dumps(compact)}")
    for line in render_report(needs, verdict):
        print(line)
    if verdict["ok"]:
        print("All checks passed" if args.release else "All checks passed (or were skipped)")
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
