#!/usr/bin/env bash
# [daveey] fork-only: pin the Linux jobs of CI to the fork's Blacksmith sizes
# (softmax-universe org billing; 2vcpu light/poll, 4vcpu suites, 8vcpu docker).
# Before 2026-11-11 these were self-hosted hermes-hub/tfgar lanes. Idempotent; re-run after merging upstream:
#   scripts/fork-ci-runner.sh && git commit -am "ci: re-point runners [daveey]"
# macOS/Windows jobs are skipped and arm64 matrix legs dropped (no such
# runners; decision in hosts/hermes-hub/gh-runner/README.md "macOS / Windows").
set -euo pipefail
cd "$(dirname "$0")/../.github/workflows"
# Upstream's Linux labels: GitHub-hosted ubuntu-latest(-32-core)/ubuntu-24.04 and
# Blacksmith blacksmith-<N>vcpu-ubuntu-2404 (never the -arm variant: no arm64 runner).
# The cheap repository guards are steps of ONE repo-guards.yml job (upstream
# consolidation); it runs `docker run` (hadolint), so it is a DOCKER_FILES entry below.
# Also the fork's retired self-hosted Linux lanes, so an old fork commit is re-pointed too.
UPSTREAM_LINUX='ubuntu-latest(-32-core)?|ubuntu-24\.04|blacksmith-[0-9]+vcpu-ubuntu-2404|\[self-hosted, (hermes-hub(-poll)?|ci-fast|docker-ci|tfgar-tests|ci-bwrap)\]'
FILES=(ci.yaml tests.yml lint.yml js-tests.yml rust-tests.yml remote-backends.yml
  bootstrap-installer.yml e2e-desktop-core.yml e2e-desktop-update.yml
  docs-site-checks.yml)
sed -i.bak -E "s/^([[:space:]]*runs-on:[[:space:]]*)(${UPSTREAM_LINUX})[[:space:]]*\$/\\1blacksmith-4vcpu-ubuntu-2404/" "${FILES[@]}"
rm -f ./*.bak

# Long-lived pollers go to the poll-only runners (label `hermes-hub-poll` ONLY,
# 0.5 CPU each; hosts/hermes-hub/gh-runner/ci-slice.sh install-poll), so they
# never hold one of the test runners.
sed -i.bak -E "s/^([[:space:]]*runs-on:[[:space:]]*)(\\[self-hosted, hermes-hub\\]|${UPSTREAM_LINUX})[[:space:]]*\$/\\1blacksmith-2vcpu-ubuntu-2404/" ci-review-comment.yml
rm -f ./*.bak

# Aggregator/report jobs run on the bounded ci-fast lane, never the
# observer-only pool (live-comment pollers can await these jobs and fill every
# poll slot, PR #221) nor the heavy pool (PR #266: a 7 s aggregate queued 23 min
# behind full suites). Keep cancellation skipping: a CANCELLED run's always()
# gate used to hold its concurrency group while queued (PR #53, PR #50).
# Every other admitted light job (.github/FAST-LANE.md) is pinned back to
# ci-fast too, so re-running this after an upstream merge cannot send it to the
# heavy pool.
# Detect gets 5 min: a 1-min cap was cancelling whole runs on a loaded hub
# (checkout 52s + compare-API retry), which skipped every lane.
python3 - <<'PY'
import re
# Upstream folded the timing report into all-checks-pass (trailing advisory steps)
# and the supply-chain jobs into repo-guards.yml steps, so the gate is the only
# completion job left. The combined lint.yml job (20 min, includes the full-tree
# ruff + ty diff) and repo-guards (15 min, docker run) are not light jobs.
COMPLETION = {
    'ci.yaml': ('all-checks-pass',),
}
FAST = {
    # macos-state-required is FAST but not COMPLETION: it keeps always() so a
    # skipped or cancelled macOS lane still fails the required check.
    'ci.yaml': ('detect', 'all-checks-pass', 'macos-state-required'),
    'actionlint.yml': ('actionlint',),
}
for f in sorted(set(COMPLETION) | set(FAST)):
    t = open(f).read()
    for name in sorted(set(COMPLETION.get(f, ())) | set(FAST.get(f, ()))):
        m = re.search(r'(?m)^  ' + re.escape(name) + r':\n(?:(?!  \S).*\n)*', t)
        assert m, (f, name)
        block = m.group(0)
        new = block
        if name in COMPLETION.get(f, ()):
            new = re.sub(r'(?m)^(    if: )always\(\)$', r"\1${{ !cancelled() }}", new)
        new = re.sub(r'(?m)^(    runs-on: )(?:\[self-hosted, (?:hermes-hub(?:-poll)?|ci-fast)\]|ubuntu-latest|ubuntu-24\.04|blacksmith-[0-9]+vcpu-ubuntu-2404)$',
                     r'\1blacksmith-2vcpu-ubuntu-2404', new)
        t = t.replace(block, new)
    if f == 'ci.yaml':
        t = re.sub(r"(  detect:\n    name: Detect affected areas\n    runs-on: [^\n]*\n    timeout-minutes: )1\n",
                   r"\g<1>5  # fork: shared, CPU-capped hub runner\n", t)
    open(f, 'w').write(t)
PY

# The Python test slices and the bubblewrap e2e-upgrade shards are the heavy
# lanes (4 vCPU: blacksmith-8vcpu jobs never got a runner on softmax-universe, PR #1077).
python3 - <<'PY'
import re
f = 'tests.yml'; t = open(f).read()
for name in ('test', 'e2e-upgrade'):
    m = re.search(r'(?m)^  ' + re.escape(name) + r':\n(?:(?!  \S).*\n)*', t)
    assert m, (f, name)
    block = m.group(0)
    t = t.replace(block, re.sub(r'(?m)^(    runs-on: )blacksmith-[0-9]+vcpu-ubuntu-2404$', r'\1blacksmith-4vcpu-ubuntu-2404', block))
open(f, 'w').write(t)
PY

# Docker-dependent jobs go to the rootless-docker runners (label `docker-ci`
# ONLY, so plain [self-hosted, hermes-hub] jobs never land on them;
# hosts/hermes-hub/gh-runner/setup-ci-docker.sh). Plain ubuntu jobs above stay
# on the socket-less runners.
DOCKER_FILES=(repo-guards.yml remote-backends.yml docker.yml nix.yml sandbox-image.yml)
sed -i.bak -E "s/^([[:space:]]*runs-on:[[:space:]]*)(\\[self-hosted, hermes-hub(, docker)?\\]|${UPSTREAM_LINUX})[[:space:]]*\$/\\1blacksmith-4vcpu-ubuntu-2404/" "${DOCKER_FILES[@]}"
# Matrix-driven image builds: amd64 -> docker-ci runner; no arm64 runner, so
# drop the arm64 leg; let the secret-free build/test jobs run on the fork
# (publish/merge jobs stay NousResearch-only: they need Docker Hub secrets).
python3 - <<'PY'
import re
for f in ('docker.yml', 'sandbox-image.yml'):
    t = open(f).read()
    t = re.sub(r"runner: (?:ubuntu-latest-32-core|blacksmith-[0-9]+vcpu-ubuntu-2404)\n", "runner: blacksmith-4vcpu-ubuntu-2404\n", t)
    t = re.sub(r"( *)arch: \[amd64, arm64\]\n", r"\1arch: [amd64]  # fork: no arm64 runner\n", t)
    t = re.sub(r"(?:          #[^\n]*\n)*          - arch: arm64\n            runner: (?:ubuntu-latest-32-arm-core|blacksmith-[0-9]+vcpu-ubuntu-2404-arm)\n            platform: linux/arm64\n", "", t)
    t = re.sub(r"\(\(needs\.mode\.outputs\.release != 'true' && github\.repository == 'NousResearch/hermes-agent' && needs\.(detect|mode)\.outputs\.build",
               r"((needs.mode.outputs.release != 'true' && (github.repository == 'NousResearch/hermes-agent' || github.repository == 'softmax-universe/daveey-hermes') && needs.\1.outputs.build", t)
    t = t.replace("  build:\n    if: github.repository == 'NousResearch/hermes-agent'\n",
                  "  build:\n    if: github.repository == 'NousResearch/hermes-agent' || github.repository == 'softmax-universe/daveey-hermes'\n")
    # One shared hub (not a 32-core hosted runner): a cold amd64 image build
    # takes ~50 min there, past the upstream 45 min cap.
    t = t.replace("    runs-on: ${{ matrix.runner }}\n    timeout-minutes: 45\n",
                  "    runs-on: ${{ matrix.runner }}\n    timeout-minutes: 120  # fork: shared hub runner\n")
    if f == 'docker.yml':
        # The always() phase gate only reads needs results: the light 2 vCPU size.
        t = re.sub(r"(  release-phase-gate:\n(?:(?!  \S).*\n)*?    runs-on: )blacksmith-[0-9]+vcpu-ubuntu-2404\n",
                   r"\1blacksmith-2vcpu-ubuntu-2404\n", t)
    open(f, 'w').write(t)
PY
rm -f ./*.bak

# GitHub's artifact quota is exhausted by the billing state: make artifact
# uploads non-fatal (idempotent: skipped when the step already has the key).
python3 - <<'PY'
import glob, re
for f in glob.glob('*.y*ml'):
    lines = open(f).read().split('\n'); ch = False
    i = 0
    while i < len(lines):
        m = re.match(r'^(\s*)(- )?uses:\s*actions/upload-artifact@', lines[i])
        if not m:
            i += 1; continue
        ind = m.group(1) + ('  ' if m.group(2) else '')   # step key indent
        s = i if m.group(2) else max(j for j in range(i + 1) if lines[j].startswith(ind[:-2] + '- '))
        e = i + 1
        while e < len(lines) and (lines[e].strip() == '' or lines[e].startswith(ind)) and not lines[e].startswith(ind[:-2] + '- '):
            e += 1
        coe = [j for j in range(s, e) if re.match(ind.replace(' ', r'\s') + r'continue-on-error:', lines[j]) or re.match(r'^\s*- continue-on-error:', lines[j])]
        if not coe:
            lines.insert(i + 1, ind + 'continue-on-error: true'); ch = True
        elif len(coe) > 1:                      # undo an earlier duplicate insert
            for j in reversed(coe[1:]):
                del lines[j]
            ch = True
        i += 1
    if ch: open(f, 'w').write('\n'.join(lines))
PY


# macOS/Windows have no runner on the fork (decision: hosts/hermes-hub/gh-runner
# README, "macOS / Windows"). Skip those jobs and drop tests-os from the set the
# gate treats as must-run, so the required check reflects what can execute.
cd ../..
python3 - <<'PY'
import re
f = '.github/workflows/ci.yaml'; t = open(f).read()
t = re.sub(r"(  tests-os:\n    name: OS-specific tests\n    needs: detect\n    if: )(?!false  #)[^\n]*", r"\1false  # fork: no macOS/Windows runner", t)
open(f, 'w').write(t)
f = '.github/workflows/bootstrap-installer.yml'; t = open(f).read()
if "    name: install.ps1 protocol surface\n    if: false" not in t:
    t = t.replace("    name: install.ps1 protocol surface\n", "    name: install.ps1 protocol surface\n    if: false  # fork: no Windows runner\n", 1)
open(f, 'w').write(t)
f = 'scripts/ci/required_results.py'; t = open(f).read()
t = t.replace('    "tests-os": ("python", "desktop_updater", "e2e", "e2e_upgrade"),\n', '')
open(f, 'w').write(t)
PY

# tests/ci asserts that tests-os is wired and must-run (upstream contract). On
# the fork that lane is deliberately off, so those assertions are reported as
# skips (reason names the fork) instead of failures. Only the allowlisted upstream
# tests about that lane, and only when their report names it, are converted;
# everything else (fork-authored tests, any test that merely prints the lane in
# a needs list) still fails.
# The hook block is rewritten on every run (an older block is cut from its marker line to EOF), so
# allowlist changes reach trees that were patched before. The heredoc below must equal
# tests/ci/conftest.py; tests/ci/test_fork_os_lane_hook.py compares them.
python3 - <<'PY'
import pathlib
f = pathlib.Path('tests/ci/conftest.py')
t = f.read_text() if f.exists() else ''
i = t.find('# [daveey] fork-ci-runner: tests-os')
f.write_text(t if i == -1 else t[:i])
PY
cat >> tests/ci/conftest.py <<'PY'
# [daveey] fork-ci-runner: tests-os is off on the fork (no macOS/Windows runner,
# hosts/hermes-hub/gh-runner/README.md). Written by scripts/fork-ci-runner.sh.
import ast as _fork_ast
import pathlib as _fork_pathlib
import re as _fork_re

import pytest as _fork_pytest

_FORK_NO_OS_LANE = "# fork: no macOS/Windows runner" in (
    _fork_pathlib.Path(__file__).resolve().parents[2] / ".github/workflows/ci.yaml"
).read_text(encoding="utf-8")
# Only these upstream tests (node id without the parametrize suffix) have the tests-os lane as their
# subject, so only they may become skips. A failure is never a skip just because its report prints the
# lane name (e.g. the `all-checks-pass.needs` list): every other test, including every fork-authored
# one, fails for real. A new upstream test about the lane fails here until it is added on purpose.
_FORK_OS_LANE_TESTS = frozenset({
    "tests/ci/test_classify_changes.py::test_real_update_suites_gate_the_merge_when_their_lane_fires",
    "tests/ci/test_required_results.py::test_selected_update_consumers_must_succeed",
    "tests/ci/test_update_ci_routing.py::test_every_set_lane_reaches_its_consumer_whatever_the_other_lanes_say",
    "tests/ci/test_update_ci_routing.py::test_marker_corpus_change_runs_every_language_that_reads_it",
    "tests/ci/test_update_ci_routing.py::test_pull_requests_and_main_pushes_never_run_e2e",
    "tests/ci/test_update_ci_routing.py::test_replay_rejects_ineffective_required_step",
    "tests/ci/test_update_ci_routing.py::test_replay_uses_executed_gate_output",
    "tests/ci/test_update_ci_routing.py::test_strict_acceptance_dispatch_reaches_every_e2e_suite",
    "tests/ci/test_update_ci_routing.py::test_update_owner_change_dispatches_its_suites_end_to_end",
    "tests/ci/test_update_ci_routing.py::test_update_path_change_reaches_the_suites_that_consume_it",
    "tests/ci/test_update_ci_routing.py::test_update_path_change_selects_every_suite_that_exercises_it",
    "tests/ci/test_update_transitive_routing.py::test_second_hop_update_change_dispatches_real_update_consumers",
})



def _fork_os_lane_failure(item, rep, call):
    """Recognize only the allowlisted upstream assertions' OS-only mismatch.

    Source, locals and arbitrary diagnostic mentions never grant a skip. Match
    the known failed predicate, and for a composite consumer predicate require
    every reported non-OS consumer to have succeeded.
    """
    message = str(getattr(getattr(rep.longrepr, "reprcrash", None), "message", ""))
    params = getattr(getattr(item, "callspec", None), "params", {})
    name = item.originalname or item.name
    if name == "test_real_update_suites_gate_the_merge_when_their_lane_fires":
        return (params.get("job") == "tests-os" and message ==
                "TypeError: unsupported operand type(s) for +: 'bool' and 'str'")
    if name == "test_selected_update_consumers_must_succeed":
        if params.get("jobs") == ("tests-os",) and params.get("result") in ("absent", "skipped"):
            return message.startswith("AssertionError: assert True is (")
        return message.startswith("AssertionError: assert ['tests'] == ['tests', 'tests-os']\n")
    if name == "test_strict_acceptance_dispatch_reaches_every_e2e_suite":
        return message == "AssertionError: tests-os/e2e-windows did not run\nassert None is not None"
    if name in {
        "test_update_path_change_selects_every_suite_that_exercises_it",
        "test_update_path_change_reaches_the_suites_that_consume_it",
    }:
        if _fork_re.search(
            r"desktop_updater never reaches its tests\nassert False\n \+  where False = _windows_desktop_updater_tests_selected\(",
            message,
        ):
            return True
    if name == "test_pull_requests_and_main_pushes_never_run_e2e":
        # Pytest can truncate nested dict reprs with unbalanced delimiters. For
        # the known compound predicate, verify the actual operand as well as
        # the failed expression instead of trying to parse that lossy repr.
        if (message.startswith("AssertionError: assert ('tests' in ") and
                " and 'tests-os' in " in message and call.excinfo is not None):
            run = call.excinfo.traceback[-1].frame.f_locals.get("run")
            return isinstance(run, dict) and "tests" in run and "tests-os" not in run
        # Simple OS membership still strips diagnostic container values.
        while True:
            stripped = _fork_re.sub(r"\[[^\[\]{}]*\]|\{[^\[\]{}]*\}", "", message)
            if stripped == message:
                break
            message = stripped
        return bool(_fork_re.search(r"(?:^|\n|AssertionError: )assert 'tests-os' (?:not )?in *$", message))
    # Remaining allowlisted predicates are all(consumer_map.values()). Equality
    # diffs and membership failures aren't this assertion, even if they name OS.
    if not _fork_re.search(r"(?:^AssertionError: |\n)assert False\n \+  where False = all\(dict_values\(", message):
        return False
    maps = []
    for raw in _fork_re.findall(r"\{[^{}]*\}", message):
        try:
            values = _fork_ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            continue
        if (isinstance(values, dict) and values and
                all(isinstance(k, str) and isinstance(v, bool) for k, v in values.items()) and
                all(k.startswith(("tests/", "tests-os/")) or k in
                    {"python", "rust", "frontend", "desktop_updater"} for k in values)):
            maps.append(values)
    # Do not accept one OS-only map while a different map reports a Linux failure.
    failed = [k for values in maps for k, value in values.items() if not value]
    return bool(failed) and all(k.startswith("tests-os/") or k == "desktop_updater" for k in failed)


@_fork_pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    # A fork_guard test asserts the fork's own wiring: its failure is real even when it names tests-os.
    if (_FORK_NO_OS_LANE and rep.when == "call" and rep.failed and not item.get_closest_marker("fork_guard")
            and item.nodeid.split("[")[0] in _FORK_OS_LANE_TESTS
            and _fork_os_lane_failure(item, rep, call)):
        rep.outcome = "skipped"
        rep.longrepr = (str(item.path), item.location[1] or 0, "fork: asserts the macOS/Windows tests-os lane, which is off on daveey/hermes")
PY
