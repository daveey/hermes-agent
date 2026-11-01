"""A test that resolves a production kanban board fails before anything touches it. [daveey]

Incident: a copy of a kanban test fixture, run from a scratch dir outside ``tests/`` with a Kanban worker's
inherited ``HERMES_KANBAN_DB`` pin, wrote three cards (t_f6b0a5ea, t_13317706, t_4d53a0f1) to the live board.
No conftest was loaded, so neither the sandbox nor ``_kanban_write_guard`` ran. Every case here stands a
fake account root in for the real one, so a guard that fails writes into ``tmp_path``, never the live board.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import hermes_state_guard
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def account_root(tmp_path, monkeypatch) -> Path:
    root = (tmp_path / "account" / ".hermes").resolve()
    root.mkdir(parents=True)
    monkeypatch.setattr(hermes_state_guard, "_account_hermes_root", lambda: root)
    return root


@pytest.mark.parametrize("parts", [("kanban.db",), ("kanban", "boards", "ops", "kanban.db")])
def test_connect_and_init_refuse_a_production_board_before_touching_it(account_root, monkeypatch, parts):
    live = account_root.joinpath(*parts)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(live))           # the inherited worker pin
    for opener in (kbc.connect, kbc.init_db, lambda: kbc.connect(live)):
        with pytest.raises(RuntimeError, match="production kanban board"):
            opener()
    assert sorted(account_root.rglob("*")) == []                   # no file, no sidecar, no board dir


def test_a_board_beside_the_production_one_still_opens(account_root, tmp_path, monkeypatch):
    workspace_db = account_root / "kanban" / "workspaces" / "t_x" / "kanban.db"
    elsewhere = tmp_path / "own" / "kanban.db"
    for db in (workspace_db, elsewhere):
        monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
        with kbc.connect_closing() as conn:
            kb.create_task(conn, title="t", assignee="w")
        assert db.exists()


_OUT_OF_TREE_PROBE = '''
from pathlib import Path

import pytest

import hermes_state_guard
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

ACCOUNT_ROOT = Path({root!r})


@pytest.fixture
def board(tmp_path, monkeypatch):
    # The incident's fixture: HERMES_HOME and Path.home only, the worker's HERMES_KANBAN_DB pin left alone.
    monkeypatch.setattr(hermes_state_guard, "_account_hermes_root", lambda: ACCOUNT_ROOT)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    yield tmp_path


def test_incident_shape_is_refused(board):
    assert kb.kanban_db_path() == ACCOUNT_ROOT / "kanban.db"
    with pytest.raises(RuntimeError, match="production kanban board"):
        kb.init_db()
    with pytest.raises(RuntimeError, match="production kanban board"):
        with kbc.connect_closing() as conn:
            kb.create_task(conn, title="x", assignee="test-worker")
'''


def test_pytest_file_outside_tests_without_conftest_is_refused(account_root, tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    probe = scratch / "probe_test.py"
    probe.write_text(textwrap.dedent(_OUT_OF_TREE_PROBE.format(root=str(account_root))))
    # The child's own pytest must arm the guard: drop our isolation marker and the parent's PYTEST_* vars.
    env = {k: v for k, v in os.environ.items() if k != "HERMES_TEST_ISOLATION" and not k.startswith("PYTEST_")}
    env["HERMES_KANBAN_DB"] = str(account_root / "kanban.db")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), env.get("PYTHONPATH", "")]))
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--noconftest", "-o", "addopts=",
         "--rootdir", str(scratch), str(probe)],
        cwd=scratch, env=env, capture_output=True, text=True, timeout=240, check=False,
    )
    assert run.returncode == 0 and "1 passed" in run.stdout, run.stdout + run.stderr
    assert sorted(account_root.rglob("*")) == []


def test_conftest_guard_denies_real_root_and_pin_when_kanban_home_moved(tmp_path, monkeypatch):
    import tests.conftest as conftest_mod

    moved, real, pin = tmp_path / "moved", tmp_path / "real" / ".hermes", tmp_path / "pinned" / "kanban.db"
    deny = conftest_mod._kanban_deny_paths(moved, real, str(pin))
    assert {moved.resolve(), real.resolve(), pin.resolve()} <= set(deny)
    monkeypatch.setattr(conftest_mod, "_REAL_KANBAN_DENY", deny)
    for db in (real / "kanban.db", pin, moved / "kanban.db"):
        with pytest.raises(RuntimeError, match="kanban_write_guard"):
            kbc.connect(db)
        assert not db.exists() and not db.parent.exists()
