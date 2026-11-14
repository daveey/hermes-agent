"""Real Git content/age checks at the report entry point; no live board or checkout."""
from __future__ import annotations

import os
import subprocess

import pytest

from tests.ci.test_nightly_e2e import T0, FakeBoard, FakeGitHub, _dispatch, ne


def git(core, *args, age=10800):
    env = {**os.environ, "GIT_AUTHOR_DATE": f"{int(T0 - age)} +0000",
           "GIT_COMMITTER_DATE": f"{int(T0 - age)} +0000"}
    return subprocess.run(["git", "-C", str(core), *args], env=env, check=True,
                          capture_output=True).stdout.decode().strip()


@pytest.fixture
def pin(tmp_path, monkeypatch):
    core = tmp_path / "core"
    source = core / "scripts" / "ci"
    source.mkdir(parents=True)
    deployed = tmp_path / "lib" / "nightly-e2e" / "abcdef123456"
    deployed.mkdir(parents=True)
    for name in ne.PIN_FILES:
        (source / name).write_bytes(b"# original\r\n")
        (deployed / name).write_bytes(b"# original\r\n")
    git(core, "init")
    git(core, "config", "user.email", "test@example.invalid")
    git(core, "config", "user.name", "Test")
    git(core, "add", ".")
    git(core, "commit", "-m", "base")
    git(core, "update-ref", "refs/remotes/origin/main", "HEAD")
    monkeypatch.setattr(ne, "__file__", str(deployed / "nightly_e2e.py"))
    monkeypatch.setattr(ne, "DEFAULT_CORE", core)
    return core, source, deployed, FakeGitHub(), FakeBoard(), ne.load_state(tmp_path / "state.json")


def change(core, source, name, age):
    (source / name).write_bytes(b"# newer\n")
    git(core, "add", ".")
    git(core, "commit", "-m", "main change", age=age)
    git(core, "update-ref", "refs/remotes/origin/main", "HEAD")


def test_missing_dependency_pin_is_reported(pin):
    _core, _source, deployed, gh, board, state = pin
    (deployed / "flaky_intake.py").unlink()
    assert "stale pin:" in ne.report(gh, board, state, now=T0)[0]
    assert "flaky_intake.py" in next(iter(board.cards.values()))["body"]


def test_identical_pin_is_silent(pin):
    _core, _source, _deployed, gh, board, state = pin
    assert ne.report(gh, board, state, now=T0) == []
    assert board.created == []


@pytest.mark.parametrize("name", ["nightly_e2e.py", "flaky_kanban.py", "flaky_intake.py", "flaky_http.py",
                                  "flaky_artifact.py", "flaky_artifact_poll.py"])
def test_stale_pin_files_exactly_one_card_on_two_reports(pin, name):
    core, source, deployed, gh, board, state = pin
    change(core, source, name, 7201)
    ne.report(gh, board, state, now=T0)
    ne.report(gh, board, state, now=T0 + 1)
    assert len(board.created) == 1
    card = next(iter(board.cards.values()))
    assert card["comments"] == []
    assert deployed.name in card["body"]
    assert git(core, "rev-parse", "origin/main") in card["body"]
    for filename in ne.PIN_FILES:
        assert git(core, "rev-parse", f"origin/main:scripts/ci/{filename}") in card["key"]
    assert "nightly_e2e_dispatch.py" in card["body"]
    assert "nightly_e2e_report.py" in card["body"]
    assert state["cards"] == {}


@pytest.mark.parametrize("age", [7199, 7200])
def test_recent_main_change_does_not_file(pin, age):
    core, source, _deployed, gh, board, state = pin
    change(core, source, "nightly_e2e.py", age)
    assert ne.report(gh, board, state, now=T0) == []
    assert board.created == []


def test_pin_check_exception_still_settles_run(pin, monkeypatch):
    _core, _source, _deployed, gh, board, state = pin
    entry = _dispatch(gh, state)
    gh.finish(entry["run_id"], "success")
    def broken(*args, **kwargs):
        raise AssertionError("broken\ncheck")
    monkeypatch.setattr(ne, "check_pin", broken)
    lines = ne.report(gh, board, state, now=T0)
    assert state["pending"] == []
    assert len(lines) == 2
    assert "pin check failed" in lines[0] and "\n" not in lines[0]
    assert "green" in lines[1]


def test_in_checkout_skips_check(pin, monkeypatch):
    core, source, _deployed, gh, board, state = pin
    monkeypatch.setattr(ne, "__file__", str(source / "nightly_e2e.py"))
    git(core, "update-ref", "-d", "refs/remotes/origin/main")
    assert ne.report(gh, board, state, now=T0) == []


def test_real_git_error_still_settles(pin):
    core, _source, _deployed, gh, board, state = pin
    git(core, "update-ref", "-d", "refs/remotes/origin/main")
    entry = _dispatch(gh, state)
    gh.finish(entry["run_id"], "success")
    lines = ne.report(gh, board, state, now=T0)
    assert len(lines) == 2
    assert "pin check failed" in lines[0]
    assert "green" in lines[1]
    assert state["pending"] == []


def test_refresh_only_when_main_identity_changes(pin):
    core, source, _deployed, gh, board, state = pin
    change(core, source, "nightly_e2e.py", 10800)
    ne.report(gh, board, state, now=T0)
    assert ne.report(gh, board, state, now=T0 + 1) == []
    git(core, "commit", "--allow-empty", "-m", "unrelated main advance")
    git(core, "update-ref", "refs/remotes/origin/main", "HEAD")
    ne.report(gh, board, state, now=T0 + 2)
    assert len(board.created) == 1
    assert len(next(iter(board.cards.values()))["comments"]) == 1


def test_cli_core_is_forwarded(pin, monkeypatch, tmp_path, capsys):
    core, source, _deployed, gh, board, _state = pin
    change(core, source, "nightly_e2e.py", 10800)
    monkeypatch.setattr(ne, "NightlyBoard", lambda *a: board)
    monkeypatch.setattr(ne, "RestGitHub", lambda *a: gh)
    monkeypatch.setattr(ne, "_token", lambda: "test")
    monkeypatch.setattr(ne.time, "time", lambda: T0)
    monkeypatch.setattr(ne, "DEFAULT_CORE", tmp_path / "not-a-repo")
    args = ["report", "--core", str(core), "--state", str(tmp_path / "cli.json")]
    assert ne.main(args) == 0
    assert ne.main(args) == 0
    assert len(board.created) == 1
    assert "pin check failed" not in capsys.readouterr().out
