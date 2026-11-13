#!/usr/bin/env python3
"""Key a pull request's macOS state lane pass by the lane-surface content it tested.

    macos_state_reuse.py key --files FILE [--repo DIR]

Prints ``key=<cache key>`` (appended to $GITHUB_OUTPUT too), or ``key=`` when no reuse is allowed.

ci.yaml's ``detect`` looks the key up in this PR's Actions cache; a hit means an earlier run of the
same PR already passed ``tests/hermes_state (macOS, mini)`` on the same content of every changed file
that routes the lane, so the lane is turned off for this run. ``macOS state required (v1)`` saves
the key after a pass. A push that only changes files outside the lane surface (a review fix, a
rebase onto a main that did not touch the PR's state files) then costs the one mini runner nothing:
on 2026-11-13, 9 of 31 PR runs on the mini repeated their branch's previous passing surface
(t_01a1d58663f79436).

The key hashes, at the checked-out PR merge commit, the git tree entry (mode and blob id, or absence)
of every changed file that routes the lane plus the lane workflow itself. So any new content in a
routing file, a routing file added to or dropped from the diff, a main change to one of those files
or a lane workflow change gives a new key. Main changes to other surface files do not: the routing
contract already leaves those to their own PRs and the 4-hourly main schedule
(classify_changes.py). An empty or capped file list (the lane runs whatever it touched) has no key.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.ci.classify_changes import COMPARE_FILE_CAP, changed_paths, hermes_state_files

LANE_WORKFLOW = ".github/workflows/hermes-state-macos.yml"
KEY_PREFIX = "hermes-state-macos-pass-v1-"


def tree_entries(repo: Path, paths: list[str]) -> list[str]:
    """``<mode> <type> <id>\\t<path>`` per path at HEAD; ``absent\\t<path>`` for a deleted one."""
    out = subprocess.run(["git", "-C", str(repo), "ls-tree", "-z", "--full-tree", "HEAD", "--", *paths],
                         capture_output=True, check=True, timeout=60).stdout.decode("utf-8", "surrogateescape")
    found = {entry.split("\t", 1)[1]: entry for entry in out.split("\0") if entry}
    return [found.get(path, f"absent\t{path}") for path in paths]


def reuse_key(repo: Path, files: list[str]) -> str:
    """The cache key for this lane-surface content, or "" when the lane may not be reused."""
    if not files or len(files) >= COMPARE_FILE_CAP:
        return ""
    routing = hermes_state_files(files)
    if not routing:
        return ""
    paths = sorted(set(routing) | {LANE_WORKFLOW})
    digest = hashlib.sha256("\n".join(tree_entries(repo, paths)).encode("utf-8", "surrogateescape"))
    return KEY_PREFIX + digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Key a PR's macOS state lane pass by its lane-surface content.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    key = sub.add_parser("key")
    key.add_argument("--files", required=True, help="changed paths, one per line (detect-changes' list)")
    key.add_argument("--repo", default=".")
    args = ap.parse_args(argv)
    lines = Path(args.files).read_text(encoding="utf-8-sig").splitlines()
    try:
        value = reuse_key(Path(args.repo), changed_paths(lines))
    except (OSError, subprocess.SubprocessError) as exc:
        # No key means the lane runs: an unreadable tree never skips it.
        print(f"::warning::macOS state reuse key unavailable ({exc}); the lane runs", file=sys.stderr)
        value = ""
    line = f"key={value}"
    if dest := os.environ.get("GITHUB_OUTPUT"):
        with open(dest, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
