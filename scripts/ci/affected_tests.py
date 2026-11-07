#!/usr/bin/env python3
"""Pick the Python test files a pull request can affect (tests.yml `plan` job).

    affected_tests.py [--repo DIR] [--base REV] [--head REV] [--changed PATH ...] [--full REASON]
                      [--files-out FILE] [--github-output FILE]

Prints a JSON plan on stdout: {"mode": "full" | "affected" | "none", "reason", "files", "slices"}.
"full" runs the whole suite (the runner's own discovery); "affected" runs `files` with --files-from;
"none" means the diff is empty. A change no test imports or names still runs the smoke set.

Selection, unioned:
  1. changed test files that the full suite would run;
  2. the transitive import closure: every test file that imports, at any depth through the repo's own
     modules, a changed module (stdlib `ast`, imports inside functions count, `import a.b` counts a and a.b);
  3. string references to a changed module ("pkg.mod" / "pkg.mod.attr": monkeypatch and mock.patch targets);
  4. tests whose text names a changed file by repo path or by a distinctive basename (scripts run by path,
     workflow YAML, .ts/.mjs read or spawned by Python tests);
  5. the smoke set: tests/ci plus every test that scans the source tree (rglob/os.walk/git ls-files).
     They break on changes no import graph reaches: docs drift, prompt surface, code health
     (replay of 83 red PR slice jobs, 2026-10-29: 16 failures were tree scans the PR never imported).
The suite falls back to "full" when a changed file shapes every test (WIDE) or the selection is over
FULL_FRACTION of the suite. Main pushes, release-check branches, dispatch and release runs never call this:
tests.yml runs the full suite for any event that is not a pull request.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Changed paths that change how every test runs: the suite, not a selection, is the honest answer.
WIDE = (
    re.compile(r"(^|/)conftest\.py$"),
    re.compile(r"^(pyproject\.toml|uv\.lock|pytest\.ini|setup\.(cfg|py)|tox\.ini)$"),
    # PM activation inputs include the interpreter pin, not just Python dependencies.
    re.compile(r"^pm/(lock\.json|environments\.py|testenv\.py)$"),
    re.compile(r"^scripts/(run_tests\.sh|run_tests_parallel\.py|run-in-hermes-env|_activation\.sh)$"),
    re.compile(r"^scripts/ci/(affected_tests|test_workers|setup_toolchain)\.py$"),
    re.compile(r"^\.github/actions/setup-pm/"),
    re.compile(r"^tests/[^/]+\.py$"),  # tests/ helpers imported by name from anywhere (fakes, fixtures)
)
TREE_SCAN = re.compile(r"rglob\(|os\.walk\(|ls-files|glob\(\s*[\"']\*\*|\.github/workflows|iter_source|walk_repo")
FULL_FRACTION = 0.4
# Basenames too generic to mean one file when a test mentions them.
GENERIC = {"__init__.py", "README.md", "index.ts", "index.js", "main.py", "utils.py", "config.py", "cli.py",
           "package.json", "SKILL.md", "types.ts", "constants.py"}
SLICE_SECONDS = 16 * 300  # one slice: 16 workers x ~5 min of per-file wall time
MAX_SLICES = 8  # tests.yml's full-suite matrix (one tfgar-tests VM per slice)
DEFAULT_FILE_SECONDS = 3.0


def _runner():
    spec = importlib.util.spec_from_file_location("run_tests_parallel", ROOT / "scripts" / "run_tests_parallel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def suite(repo: Path) -> list[str]:
    """The files `scripts/run_tests.sh` with no paths runs: the selection never adds a file outside it."""
    return [p.relative_to(repo).as_posix() for p in _runner()._discover_files([repo / "tests"])]


def module_name(rel: str) -> str | None:
    if not rel.endswith(".py"):
        return None
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts and all(p.isidentifier() for p in parts) else None


def tracked_py(repo: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo), "ls-files", "*.py"], check=True, capture_output=True,
                         text=True, timeout=60).stdout
    return [f for f in out.splitlines() if f and "node_modules/" not in f]


def imports_of(repo: Path, rel: str, modules: set[str]) -> set[str]:
    try:
        tree = ast.parse((repo / rel).read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, SyntaxError, ValueError):
        return set()
    me = module_name(rel) or ""
    package = me if rel.endswith("__init__.py") else me.rpartition(".")[0]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".") if package else []
                parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                base = ".".join(parts + ([base] if base else []))
            names.add(base)
            names.update(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
    found: set[str] = set()
    for name in names:
        parts = name.split(".")
        for i in range(1, len(parts) + 1):  # `import a.b.c` runs a, a.b and a.b.c
            prefix = ".".join(parts[:i])
            if prefix in modules:
                found.add(prefix)
    return found


def importers(repo: Path, changed_modules: set[str]) -> set[str]:
    """Every tracked .py file that imports a changed module, directly or through other repo modules."""
    files = tracked_py(repo)
    by_module = {m: f for f in files if (m := module_name(f))}
    modules = set(by_module)
    reverse: dict[str, set[str]] = {}
    for f in files:
        for dep in imports_of(repo, f, modules):
            reverse.setdefault(dep, set()).add(f)
    seen_files: set[str] = set()
    frontier = [m for m in changed_modules if m in modules]
    seen_modules = set(frontier)
    while frontier:
        for f in reverse.get(frontier.pop(), ()):
            if f in seen_files:
                continue
            seen_files.add(f)
            m = module_name(f)
            if m and m not in seen_modules:
                seen_modules.add(m)
                frontier.append(m)
    return seen_files


def _read(repo: Path, rel: str) -> str:
    try:
        return (repo / rel).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def estimate_seconds(repo: Path, files: list[str]) -> float:
    try:
        durations = json.loads((repo / "test_durations.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        durations = {}
    return sum(float(durations.get(f, DEFAULT_FILE_SECONDS)) for f in files)


def slices_for(seconds: float) -> int:
    return max(1, min(MAX_SLICES, math.ceil(seconds / SLICE_SECONDS)))


def plan(repo: Path, changed: list[str], full_reason: str | None = None) -> dict:
    if full_reason:
        return {"mode": "full", "reason": full_reason, "files": [], "slices": MAX_SLICES}
    # detect-changes emits the new and previous path of a rename on one TSV row.
    # Both sides can affect tests, including a setup file moved out of its old location.
    changed = [path for row in changed for path in row.split("\t") if path]
    wide = sorted(c for c in changed if any(p.search(c) for p in WIDE))
    if wide:
        return {"mode": "full", "reason": f"changes every test's setup: {', '.join(wide[:5])}", "files": [],
                "slices": MAX_SLICES}
    universe = suite(repo)
    in_suite = set(universe)
    selected = {c for c in changed if c in in_suite}
    changed_modules = {m for c in changed if not c.startswith("tests/") and (m := module_name(c))}
    selected |= importers(repo, changed_modules) & in_suite
    string_refs = [re.compile(rf"[\"']{re.escape(m)}([\"']|\.)") for m in changed_modules]
    path_refs = [re.escape(c) for c in changed if "/" in c and Path(c).name not in GENERIC]
    path_refs += [rf"(?<![\w.-]){re.escape(Path(c).name)}(?![\w-])" for c in changed
                  if Path(c).name not in GENERIC and len(Path(c).name) >= 8]
    path_ref = re.compile("|".join(path_refs)) if path_refs else None
    smoke: set[str] = set()
    for f in universe:
        if f in selected:
            continue
        text = _read(repo, f)
        if f.startswith("tests/ci/") or TREE_SCAN.search(text):
            smoke.add(f)
        elif any(p.search(text) for p in string_refs) or (path_ref and path_ref.search(text)):
            selected.add(f)
    if not changed:
        return {"mode": "none", "reason": "no changed files", "files": [], "slices": 0}
    files = sorted(selected | smoke)
    if len(files) > FULL_FRACTION * len(universe):
        return {"mode": "full", "reason": f"{len(files)} of {len(universe)} test files affected", "files": [],
                "slices": MAX_SLICES}
    return {"mode": "affected",
            "reason": f"{len(selected)} affected + {len(smoke)} smoke of {len(universe)} test files",
            "files": files, "slices": slices_for(estimate_seconds(repo, files))}


def changed_files(repo: Path, base: str, head: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo), "diff", "--name-only", f"{base}...{head}"], check=True,
                         capture_output=True, text=True, timeout=60).stdout
    return [line for line in out.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=str(ROOT))
    ap.add_argument("--base", default="HEAD^1")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--changed", nargs="*", help="repo-relative paths instead of git diff BASE...HEAD")
    ap.add_argument("--full", metavar="REASON", help="skip selection: run the full suite for REASON")
    ap.add_argument("--files-out", help="write the selected files here, one per line")
    ap.add_argument("--github-output", help="append mode/reason/slices outputs (GITHUB_OUTPUT)")
    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve()
    if args.full:
        changed = []
    elif args.changed is not None:
        changed = args.changed
    else:
        try:
            changed = changed_files(repo, args.base, args.head)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            # No diff means no evidence for a narrower run.
            detail = (exc.stderr or "").strip()[:200] if isinstance(exc.stderr, str) else "timed out"
            changed, args.full = [], f"git diff {args.base}...{args.head} failed: {detail}"
    result = plan(repo, changed, args.full)
    if args.files_out:
        Path(args.files_out).write_text("".join(f"{f}\n" for f in result["files"]), encoding="utf-8")
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as out:
            out.write(f"mode={result['mode']}\n")
            out.write(f"reason={' '.join(result['reason'].split())}\n")  # one line, or GITHUB_OUTPUT breaks
            out.write(f"count={len(result['files'])}\n")
            out.write(f"slices={json.dumps(list(range(1, result['slices'] + 1)))}\n")
            out.write(f"slice_count={result['slices']}\n")
    print(json.dumps(result, indent=1))
    print(f"affected-tests: {result['mode']}: {result['reason']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
