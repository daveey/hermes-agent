"""The Prebuilt TUI install shape is checked on the PRs that can break it.

termux-verify.yml's ``tui`` job installs ui-tui + tests-js + apps/desktop and
their dependency closure, then runs the TUI checks. Its path
filter covers scripts/termux, pm and its own tests, so a tests-js or ui-tui
change never ran it on its own PR: 7a33a706f5 added a tests-js test that
assumed apps/desktop was installed, passed the main CI's full install, and
first failed on the unrelated pm PR #339 (run 41097455062).

js-tests.yml's ``tui-install-shape`` job runs the same install and commands on
every PR that selects the frontend lane. These tests keep the two jobs from
drifting apart and keep the job inside the merge gate.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.fork_guard

_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("classify_changes", _REPO / "scripts/ci/classify_changes.py")
assert _spec and _spec.loader
_classify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_classify)


def _yaml(rel: str):
    yaml = pytest.importorskip("hermes_yaml")
    return yaml.safe_load((_REPO / rel).read_text(encoding="utf-8-sig"))


def _shape(job: dict) -> list:
    """The steps that define the install shape: setup inputs and every run command."""
    out = []
    for step in job["steps"]:
        uses = step.get("uses", "")
        if uses == "./.github/actions/setup-pm":
            out.append(("setup-pm", step.get("with") or {}))
        elif "run" in step:
            out.append(("run", step["run"].strip()))
    return out


def test_main_ci_job_matches_the_termux_prebuilt_tui_job():
    termux = _yaml(".github/workflows/termux-verify.yml")["jobs"]["tui"]
    main = _yaml(".github/workflows/js-tests.yml")["jobs"]["tui-install-shape"]
    assert _shape(main) == _shape(termux)
    runs = [cmd for kind, cmd in _shape(main) if kind == "run"]
    # The point of the job: a minimal install, not the full workspace.
    assert runs[0] == ('pnpm install --frozen-lockfile --filter "{./ui-tui}..." '
                       '--filter "{./tests-js}..." --filter "{./apps/desktop}..."')
    assert "pnpm --dir tests-js run check" in runs[-1]
    assert "pnpm --dir ui-tui run check" in runs[-1]
    assert "pnpm --dir ui-tui run build" in runs[-1]
    assert not main.get("continue-on-error")


def test_job_gates_the_merge_through_js_tests():
    ci = _yaml(".github/workflows/ci.yaml")
    js = ci["jobs"]["js-tests"]
    assert js["uses"] == "./.github/workflows/js-tests.yml"
    assert "frontend" in js["if"]
    assert "js-tests" in ci["jobs"]["all-checks-pass"]["needs"]
    assert "if" not in _yaml(".github/workflows/js-tests.yml")["jobs"]["tui-install-shape"]


@pytest.mark.parametrize("path", [
    "tests-js/run-workspace-checks.test.mjs",
    "ui-tui/src/entry.tsx",
    "ui-tui/packages/hermes-ink/src/index.ts",
    "apps/shared/src/index.ts",
    ".github/scripts/run-workspace-checks.mjs",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "scripts/build/tui.mjs",
    "eslint.config.shared.mjs",
    ".npmrc",
])
def test_every_input_of_the_shape_selects_the_frontend_lane(path):
    assert _classify.classify([path])["frontend"] is True
