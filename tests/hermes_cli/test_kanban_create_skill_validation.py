"""Kanban cards pinned to a skill the assignee cannot load. [daveey]

A card created with ``skills=["x"]`` used to be accepted, then every worker died at start
with ``Error: Unknown skill(s): x`` and the dispatcher retried it as a crash (escalating the
retry model) until the breaker gave up. Contracts:

* create time (kanban_create tool and ``hermes kanban create``) refuses a name the ASSIGNEE
  profile cannot load, with close matches, and creates nothing;
* a dispatcher-owned worker whose pins are all missing still starts its session (#119619);
* a worker that does die of ``Unknown skill(s)`` is parked as an ``assignment`` block on the
  first death, never retried.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

_SESSION_ENV = ("HERMES_SESSION_KEY", "HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_DB",
                "HERMES_KANBAN_BOARD")


def _write_skill(skills_root: Path, name: str) -> None:
    d = skills_root / "testing" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: test skill {name}\n---\n\n# {name}\nBody.\n",
                                encoding="utf-8")


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in _SESSION_ENV:
        monkeypatch.delenv(var, raising=False)
    _write_skill(home / "skills", "real-skill")
    other = home / "profiles" / "other"
    (other / "skills").mkdir(parents=True)
    (other / "config.yaml").write_text("{}\n", encoding="utf-8")
    _write_skill(other / "skills", "other-only")
    kb.init_db()
    return home


def _own(home, monkeypatch, sid="20261014_141400_abc123") -> None:
    """Act as a registered owning chat: a genuine agent create needs real renewable authority."""
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(session_id=sid, source="desktop")
    finally:
        db.close()
    monkeypatch.setenv("HERMES_SESSION_ID", sid)


def _count() -> int:
    with kbc.connect_closing() as conn:
        return len(kb.list_tasks(conn))


def _tool_create(**args) -> dict:
    from tools import kanban_tools as kt
    return json.loads(kt._handle_create({"title": "skill card", **args}))


def _cli_create(*argv: str) -> int:
    from hermes_cli import kanban as kc
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    return kc._cmd_create(root.parse_args(["kanban", "create", "skill card", *argv]))


# create time: tool path ----------------------------------------------------------------------


def test_tool_refuses_unknown_skill_and_creates_nothing(board):
    out = _tool_create(assignee="default", skills=["x"])
    assert "error" in out, out
    assert out.get("code") == "unknown_skill", out
    assert "x" in out["error"] and "default" in out["error"], out
    assert _count() == 0


def test_tool_names_close_matches(board):
    out = _tool_create(assignee="default", skills=["real-skil"])
    assert out.get("code") == "unknown_skill", out
    assert "real-skill" in out["error"], out


def test_tool_accepts_a_skill_the_assignee_has(board, monkeypatch):
    _own(board, monkeypatch)
    out = _tool_create(assignee="default", skills=["real-skill"])
    assert out.get("ok"), out
    assert _count() == 1


def test_tool_checks_the_assignee_profile_not_the_caller(board, monkeypatch):
    _own(board, monkeypatch)
    assert _tool_create(assignee="default", skills=["other-only"]).get("code") == "unknown_skill"
    out = _tool_create(assignee="other", skills=["other-only"])
    assert out.get("ok"), out
    assert _count() == 1


# create time: CLI path -----------------------------------------------------------------------


def test_cli_refuses_unknown_skill_and_creates_nothing(board, capsys):
    rc = _cli_create("--assignee", "default", "--skill", "x")
    assert rc == 2
    assert "Unknown skill" in capsys.readouterr().err
    assert _count() == 0


def test_cli_accepts_a_real_skill(board):
    assert _cli_create("--assignee", "default", "--skill", "real-skill") == 0
    assert _count() == 1


# start time: a stale pin must not kill a dispatcher-owned worker (#119619) ---------------------


class _DummyCLI:
    def __init__(self):
        self.session_id = "s"
        self.system_prompt = "base prompt"
        self.preloaded_skills = []
        self._preload_skills_thread = None


def _finalize(cli_obj, result):
    import threading

    import cli as cli_mod
    cli_obj._preload_skills_result = result
    cli_obj._preload_skills_thread = threading.Thread(target=lambda: None)
    cli_obj._preload_skills_thread.start()
    return cli_mod.HermesCLI.__dict__["finalize_preloaded_skills"](cli_obj)


def test_kanban_worker_with_all_pins_missing_still_starts(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    cli_obj = _DummyCLI()
    _finalize(cli_obj, ("", [], ["x"]))  # must not raise
    assert "x" in cli_obj.system_prompt and "base prompt" in cli_obj.system_prompt


def test_interactive_session_with_all_pins_missing_still_fails_loudly(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    with pytest.raises(ValueError, match=r"Unknown skill\(s\): x"):
        _finalize(_DummyCLI(), ("", [], ["x"]))


# dispatcher: an Unknown-skill death is an assignment block, not a retry ------------------------


def test_unknown_skill_exit_blocks_as_assignment_without_retry(board, monkeypatch):
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    with kbc.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="stale pin", assignee="default", max_retries=5)
        kb.claim_task(conn, tid, claimer=f"{host}:w0")
        pid = 72000
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
        conn.commit()
        task = kb.get_task(conn, tid)
        assert task is not None
        with kbd._open_worker_log(task, None) as log:
            log.write(b'Error: Unknown skill(s): x\n')
        kbd._record_worker_exit(pid, 1 << 8)

        assert tid in kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.status == "triage", task.status
        assert task.block_kind == "assignment", task.block_kind
        assert task.consecutive_failures == 1
        gave_up = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,)).fetchone()
        assert json.loads(gave_up["payload"])["unknown_skill"] is True
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "triage"
