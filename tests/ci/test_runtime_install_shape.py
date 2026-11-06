"""The runtime-only tests-js install shape is checked on the PRs that can break it.

desktop-bundle-smoke.yml and the install-e2e-*-run.yml workflows install
``pnpm install --filter "{./tests-js}..." --prod`` and launch
tests/install/e2e-assets drivers. They run on release/dispatch only, so a driver
importing a devDependency first failed at release time. js-tests.yml's
``runtime-install-shape`` job installs the same shape on every frontend-lane PR
and resolves every driver import. These tests keep the two in sync.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
# [daveey] The fork conftest (tests/ci/conftest.py) never turns a fork_guard failure into a skip: this file
# asserts the fork's own required-gate wiring.
pytestmark = pytest.mark.fork_guard
_spec = importlib.util.spec_from_file_location(
    "classify_changes", _REPO / "scripts/ci/classify_changes.py"
)
assert _spec and _spec.loader
_classify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_classify)

_WORKFLOWS = [
    "desktop-bundle-smoke.yml",
    "install-e2e-run.yml",
    "install-e2e-macos-run.yml",
    "install-e2e-windows-run.yml",
]
_INSTALL = re.compile(r'^\s*run:\s*(pnpm install .*--filter "\{\./tests-js\}\.\.\." .*)$', re.MULTILINE)
_ASSETS = _REPO / "tests/install/e2e-assets"
_CHECKER = (_REPO / ".github/scripts/check-runtime-driver-imports.mjs").read_text(
    encoding="utf-8"
)


def _yaml(rel: str):
    yaml = pytest.importorskip("hermes_yaml")
    return yaml.safe_load((_REPO / rel).read_text(encoding="utf-8-sig"))


def _install_commands(workflow: str) -> list[str]:
    text = (_REPO / ".github/workflows" / workflow).read_text(encoding="utf-8")
    return [m.strip() for m in _INSTALL.findall(text)]


def test_every_runtime_install_in_the_workflows_is_one_shape():
    shapes = {c for w in _WORKFLOWS for c in _install_commands(w)}
    assert len(shapes) == 1, shapes
    assert next(iter(shapes)) == 'pnpm install --frozen-lockfile --filter "{./tests-js}..." --prod --ignore-scripts'
    # desktop-bundle-smoke 1, install-e2e-run 1, macos 2, windows 2: a new
    # install step must be added to this count on purpose.
    assert sum(len(_install_commands(w)) for w in _WORKFLOWS) == 6


def test_job_installs_exactly_that_shape_and_gates_the_merge():
    job = _yaml(".github/workflows/js-tests.yml")["jobs"]["runtime-install-shape"]
    runs = [s["run"].strip() for s in job["steps"] if "run" in s]
    assert runs[0] == _install_commands(_WORKFLOWS[0])[0]
    assert "check-runtime-driver-imports.mjs" in runs[1]
    assert "windows-bundled-helpers.test.mjs" in runs[1]
    assert not job.get("continue-on-error")
    assert "if" not in job
    ci = _yaml(".github/workflows/ci.yaml")
    assert "frontend" in ci["jobs"]["js-tests"]["if"]
    assert "js-tests" in ci["jobs"]["all-checks-pass"]["needs"]


def test_checker_covers_every_node_script_the_launchers_run():
    """Each e2e-assets .mjs/.cjs/.ts a launcher or workflow names is a checked entry point."""
    launchers = list((_REPO / "tests/install").glob("*.sh")) + list(
        (_REPO / "tests/install").glob("*.ps1")
    )
    launchers += [_REPO / ".github/workflows" / w for w in _WORKFLOWS]
    named: set[str] = set()
    for path in launchers:
        text = path.read_text(encoding="utf-8", errors="replace")
        named |= set(
            re.findall(r"([A-Za-z0-9_-]+(?:\.[a-z]+)*\.(?:mjs|cjs|ts))\b", text)
        )
    on_disk = {p.name for p in _ASSETS.iterdir() if p.suffix in {".mjs", ".cjs", ".ts"}}
    launched = named & on_disk
    # Guards the pattern itself: these are launched by the scripts today.
    assert {
        "desktop-smoke.ts",
        "bundle-inputs.mjs",
        "launch-from-spec.mjs",
        "windows-bundled-helpers.test.mjs",
    } <= launched
    # The checker globs the whole assets dir, so every launched file is covered.
    assert "readdirSync(dir)" in _CHECKER
    assert "const ASSETS = 'tests/install/e2e-assets'" in _CHECKER
    # Launched from outside the assets dir.
    assert "tests-js/scripts/mock-provider-config.ts" in _CHECKER
    for ps1 in ("desktop-smoke-windows.ps1",):
        assert "mock-provider-config.ts" in (_ASSETS / ps1).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "path",
    [
        "tests/install/e2e-assets/desktop-smoke.ts",
        "tests/install/windows-e2e.ps1",
        "scripts/sandbox/generate-e2e-matrix.mjs",
        "scripts/generate-icons.mjs",
        ".github/scripts/check-runtime-driver-imports.mjs",
        "tests-js/scripts/mock-provider-config.ts",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
    ],
)
def test_every_input_of_the_shape_selects_the_frontend_lane(path):
    assert _classify.classify([path])["frontend"] is True
