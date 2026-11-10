"""The runtime-only tests-js install shape is checked on the PRs that can break it.

desktop-bundle-smoke.yml and the install-e2e-*-run.yml workflows install
``pnpm install --filter "{./tests-js}..." --prod`` (inline, or through the
justfile's ``js-install-tests`` recipe) and launch
tests/install/e2e-assets drivers. They run on release/dispatch only, so a driver
importing a devDependency first failed at release time. js-tests.yml's
``runtime-install-shape`` job installs the same shape on every frontend-lane PR
and resolves every driver import. These tests keep the two in sync.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.ci import just_recipes

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
_RUN = re.compile(r"^\s*(?:-\s+)?run:\s*(\S.*)$", re.MULTILINE)
_INSTALL = re.compile(r'^\s*run:\s*(pnpm install .*--filter "\{\./tests-js\}\.\.\." .*)$', re.MULTILINE)
_SHAPE = 'pnpm install --frozen-lockfile --filter "{./tests-js}..." --prod --ignore-scripts'
_ASSETS = _REPO / "tests/install/e2e-assets"
_CHECKER = (_REPO / ".github/scripts/check-runtime-driver-imports.mjs").read_text(
    encoding="utf-8"
)


def _yaml(rel: str):
    yaml = pytest.importorskip("hermes_yaml")
    return yaml.safe_load((_REPO / rel).read_text(encoding="utf-8-sig"))


def _install_commands(workflow: str) -> list[str]:
    """Each one-line ``run:`` of the shape, seen through ``just <recipe>`` steps.

    Upstream moved the install-e2e lanes' command into ``just js-install-tests``;
    the shape is what the recipe (and any recipe it runs) executes, not the step text.
    """
    text = (_REPO / ".github/workflows" / workflow).read_text(encoding="utf-8")
    commands = []
    for run in _RUN.findall(text):
        for command in just_recipes.expand(_REPO / "justfile", run):
            if _INSTALL.match(f"run: {command}"):
                commands.append(command.strip())
    return commands


def test_just_reader_matches_just_dry_run():
    """The reader's expansion is what `just` itself would run, for every recipe it models.

    `just --dry-run` prints a nested `just <recipe>` line without descending, so the
    comparison is one level deep; nesting is covered by the controls below.
    """
    just = shutil.which("just")
    if not just:
        pytest.skip("just is not on PATH; the reader is still exercised by the shape test")
    recipes = just_recipes.parse(_REPO / "justfile")
    assert "js-install-tests" in recipes
    modelled = 0
    for name in recipes:
        try:
            expected = just_recipes.recipe_commands(_REPO / "justfile", name, follow_just=False)
            just_recipes.recipe_commands(_REPO / "justfile", name)  # the nested reading resolves too
        except just_recipes.UnsupportedRecipe:
            continue
        dry = subprocess.run(
            [just, "--justfile", str(_REPO / "justfile"), "--dry-run", name],
            cwd=_REPO, capture_output=True, text=True, check=True,
        )
        assert dry.stderr.splitlines() == expected, name
        modelled += 1
    assert modelled >= 1


@pytest.mark.parametrize(
    ("body", "ok"),
    [
        # Transitive: a recipe that runs another recipe, or depends on one.
        (f"inner:\n    {_SHAPE}\njs-install-tests:\n    @just inner\n", True),
        (f"inner:\n    {_SHAPE}\njs-install-tests: inner\n    echo done\n", True),
        (f"js-install-tests:\n    {_SHAPE}\n", True),
        # Broken recipes: a full install (the count drops), or the shape minus --prod
        # (two shapes), each directly or one recipe down.
        ("js-install-tests:\n    pnpm install --frozen-lockfile\n", False),
        (f"js-install-tests:\n    {_SHAPE.replace(' --prod', '')}\n", False),
        (f"inner:\n    {_SHAPE.replace(' --prod', '')}\njs-install-tests:\n    just inner\n", False),
        ("inner:\n    pnpm install --frozen-lockfile\njs-install-tests: inner\n", False),
    ],
)
def test_install_commands_follow_recipes(tmp_path, monkeypatch, body, ok):
    """Negative controls: the shape is read through (transitive) recipes, and a broken one is caught."""
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/inline.yml").write_text(
        f"jobs:\n  j:\n    steps:\n      - run: {_SHAPE}\n", encoding="utf-8"
    )
    (tmp_path / ".github/workflows/recipe.yml").write_text(
        "jobs:\n  j:\n    steps:\n      - name: Install\n        run: just js-install-tests\n", encoding="utf-8"
    )
    (tmp_path / "justfile").write_text(body, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_REPO", tmp_path)
    if ok:
        _assert_one_shape(["inline.yml", "recipe.yml"], 2)
    else:
        with pytest.raises(AssertionError):
            _assert_one_shape(["inline.yml", "recipe.yml"], 2)


@pytest.mark.parametrize(
    "body",
    [
        "js-install-tests:\n    pnpm install {{ args }}\n",  # interpolation
        "js-install-tests arg:\n    pnpm install\n",  # parameters
        "[unix]\njs-install-tests:\n    pnpm install\n[windows]\njs-install-tests:\n    pnpm i\n",
        "js-install-tests:\n    #!/usr/bin/env bash\n    pnpm install\n",
        "other:\n    true\n",  # missing recipe
        "a:\n    just js-install-tests\njs-install-tests:\n    just a\n",  # cycle
    ],
)
def test_unmodelled_recipes_fail_closed(tmp_path, body):
    (tmp_path / "justfile").write_text(body, encoding="utf-8")
    with pytest.raises(just_recipes.UnsupportedRecipe):
        just_recipes.expand(tmp_path / "justfile", "just js-install-tests")


def _assert_one_shape(workflows: list[str], total: int) -> None:
    shapes = {c for w in workflows for c in _install_commands(w)}
    assert len(shapes) == 1, shapes
    assert next(iter(shapes)) == _SHAPE
    assert sum(len(_install_commands(w)) for w in workflows) == total


def test_every_runtime_install_in_the_workflows_is_one_shape():
    # desktop-bundle-smoke 1 (inline), install-e2e-run 1, macos 2, windows 2 (each
    # `just js-install-tests`): a new install step must be added to this count on purpose.
    _assert_one_shape(_WORKFLOWS, 6)


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
        # The install-e2e lanes run the shape as `just js-install-tests`.
        "justfile",
    ],
)
def test_every_input_of_the_shape_selects_the_frontend_lane(path):
    assert _classify.classify([path])["frontend"] is True
