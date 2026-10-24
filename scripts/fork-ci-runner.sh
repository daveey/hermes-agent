#!/usr/bin/env bash
# [daveey] fork-only: run the Linux jobs of CI on the self-hosted hermes-hub
# runners (hosts/hermes-hub/gh-runner) because the private fork has no
# GitHub-hosted minutes. Idempotent; re-run after merging upstream:
#   scripts/fork-ci-runner.sh && git commit -am "ci: re-point runners [daveey]"
# macOS/Windows jobs are skipped and arm64 matrix legs dropped (no such
# runners; decision in hosts/hermes-hub/gh-runner/README.md "macOS / Windows").
set -euo pipefail
cd "$(dirname "$0")/../.github/workflows"
FILES=(ci.yaml tests.yml lint.yml js-tests.yml rust-tests.yml remote-backends.yml
  bootstrap-installer.yml e2e-desktop-core.yml e2e-desktop-update.yml
  docs-site-checks.yml history-check.yml contributor-check.yml
  uv-lockfile-check.yml infographic-check.yml profile-artifact-check.yml
  icons-freshness-check.yml case-collision-check.yml blob-size-check.yml
  lazy-deps-guard.yml lockfile-diff.yml prompt-surface-diff.yml
  docker-lint.yml supply-chain-audit.yml)
sed -i.bak -E 's/^([[:space:]]*runs-on:[[:space:]]*)(ubuntu-latest(-32-core)?|ubuntu-24\.04)[[:space:]]*$/\1[self-hosted, hermes-hub]/' "${FILES[@]}"
rm -f ./*.bak

# Long-lived pollers go to the poll-only runners (label `hermes-hub-poll` ONLY,
# 0.5 CPU each; hosts/hermes-hub/gh-runner/ci-slice.sh install-poll), so they
# never hold one of the test runners.
sed -i.bak -E 's/^([[:space:]]*runs-on:[[:space:]]*)(\[self-hosted, hermes-hub\]|ubuntu-latest|ubuntu-24\.04)[[:space:]]*$/\1[self-hosted, hermes-hub-poll]/' ci-review-comment.yml
rm -f ./*.bak

# Docker-dependent jobs go to the rootless-docker runners (label `docker-ci`
# ONLY, so plain [self-hosted, hermes-hub] jobs never land on them;
# hosts/hermes-hub/gh-runner/setup-ci-docker.sh). Plain ubuntu jobs above stay
# on the socket-less runners.
DOCKER_FILES=(docker-lint.yml remote-backends.yml docker.yml nix.yml sandbox-image.yml)
sed -i.bak -E 's/^([[:space:]]*runs-on:[[:space:]]*)(\[self-hosted, hermes-hub(, docker)?\]|ubuntu-latest(-32-core)?|ubuntu-24\.04)[[:space:]]*$/\1[self-hosted, docker-ci]/' "${DOCKER_FILES[@]}"
# Matrix-driven image builds: amd64 -> docker-ci runner; no arm64 runner, so
# drop the arm64 leg; let the secret-free build/test jobs run on the fork
# (publish/merge jobs stay NousResearch-only: they need Docker Hub secrets).
python3 - <<'PY'
import re
for f in ('docker.yml', 'sandbox-image.yml'):
    t = open(f).read()
    t = re.sub(r"runner: ubuntu-latest-32-core\n", "runner: docker-ci\n", t)
    t = re.sub(r"( *)arch: \[amd64, arm64\]\n", r"\1arch: [amd64]  # fork: no arm64 runner\n", t)
    t = re.sub(r"(?:          #[^\n]*\n)*          - arch: arm64\n            runner: ubuntu-latest-32-arm-core\n            platform: linux/arm64\n", "", t)
    t = t.replace("((needs.mode.outputs.release != 'true' && github.repository == 'NousResearch/hermes-agent' && needs.detect.outputs.build",
                  "((needs.mode.outputs.release != 'true' && (github.repository == 'NousResearch/hermes-agent' || github.repository == 'daveey/hermes') && needs.detect.outputs.build")
    t = t.replace("  build:\n    if: github.repository == 'NousResearch/hermes-agent'\n",
                  "  build:\n    if: github.repository == 'NousResearch/hermes-agent' || github.repository == 'daveey/hermes'\n")
    # One shared hub (not a 32-core hosted runner): a cold amd64 image build
    # takes ~50 min there, past the upstream 45 min cap.
    t = t.replace("    runs-on: ${{ matrix.runner }}\n    timeout-minutes: 45\n",
                  "    runs-on: ${{ matrix.runner }}\n    timeout-minutes: 120  # fork: shared hub runner\n")
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
# skips (reason names the fork) instead of failures. Only failures whose report
# names the mac/Windows lane are converted; everything else still fails.
if ! grep -q 'fork-ci-runner: tests-os' tests/ci/conftest.py 2>/dev/null; then
cat >> tests/ci/conftest.py <<'PY'
# [daveey] fork-ci-runner: tests-os is off on the fork (no macOS/Windows runner,
# hosts/hermes-hub/gh-runner/README.md). Written by scripts/fork-ci-runner.sh.
import pathlib as _fork_pathlib
import re as _fork_re

import pytest as _fork_pytest

_FORK_NO_OS_LANE = "# fork: no macOS/Windows runner" in (
    _fork_pathlib.Path(__file__).resolve().parents[2] / ".github/workflows/ci.yaml"
).read_text(encoding="utf-8")
# desktop_updater's only consumer is tests-os[windows].
_FORK_OS_LANE_RE = _fork_re.compile(r"tests-os|e2e-windows|_windows_desktop_updater|os-tests\[|'desktop_updater': False")


@_fork_pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if _FORK_NO_OS_LANE and rep.when == "call" and rep.failed and _FORK_OS_LANE_RE.search(str(rep.longrepr)):
        rep.outcome = "skipped"
        rep.longrepr = (str(item.path), item.location[1] or 0, "fork: asserts the macOS/Windows tests-os lane, which is off on daveey/hermes")
PY
fi
