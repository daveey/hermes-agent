"""Registered wall checks are durable, home-owned evidence, not card wake checks."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_shared_blockers as blockers
from tests.hermes_cli.test_kanban_shared_blockers import _blocked_behind, kanban_home


def test_success_is_durable_even_with_worker_probe_running(kanban_home):
    from hermes_cli import kanban_shared_blocker_checks as checks

    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "wall", blocker_id="test-wall")
        assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid)
        now = blockers.blocker_states(conn)["test-wall"].asserted_at + 10
        cfg = {"kanban": {"shared_blocker_checks": {"test-wall": "printf 'récovered'"}}}
        assert checks.run_due_checks(conn, cfg=cfg, now=now) == ["test-wall"]
    with kbc.connect_closing() as conn:
        states = blockers.blocker_states(conn, now=now)
        checks.apply_check_clears(conn, states)
        state = states["test-wall"]
        assert not state.is_open
        assert state.cleared_by == ("", None)
        assert state.cleared_via == "check"
        assert kb.get_task(conn, tid).status == "running"
        event = [e for e in kb.list_events(conn, tid) if e.kind == checks.CHECK_EVENT][-1]
        assert event.payload["output"] == "récovered"


def _cfg(command):
    return {"kanban": {"shared_blocker_checks": {"test-wall": command}}}


def test_not_yet_persists_cadence_across_connections(kanban_home):
    from hermes_cli import kanban_shared_blocker_checks as checks

    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "waiting", blocker_id="test-wall")
        now = blockers.blocker_states(conn)["test-wall"].asserted_at + 10
        due = []
        assert checks.run_due_checks(conn, cfg=_cfg("exit 1"), now=now, due_out=due) == []
        assert due == [now + 120]
    with kbc.connect_closing() as conn:
        checks.run_due_checks(conn, cfg=_cfg("exit 1"), now=now + 119)
        assert len([e for e in kb.list_events(conn, tid) if e.kind == checks.CHECK_EVENT]) == 1
        checks.run_due_checks(conn, cfg=_cfg("exit 1"), now=now + 120)
        assert len([e for e in kb.list_events(conn, tid) if e.kind == checks.CHECK_EVENT]) == 2
        states = blockers.blocker_states(conn, now=now + 120)
        checks.apply_check_clears(conn, states)
        assert states["test-wall"].is_open


def test_three_errors_stop_checks_without_clearing(kanban_home, caplog):
    from hermes_cli import kanban_shared_blocker_checks as checks

    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "broken checker", blocker_id="test-wall")
        now = blockers.blocker_states(conn)["test-wall"].asserted_at + 10
        for offset in (0, 120, 240, 360, 1800):
            checks.run_due_checks(conn, cfg=_cfg("exit 2"), now=now + offset)
        events = [e for e in kb.list_events(conn, tid) if e.kind == checks.CHECK_EVENT]
        assert [e.payload["fails"] for e in events] == [1, 2, 3]
        assert "falling back to worker probes" in caplog.text
        states = blockers.blocker_states(conn, now=now + 1800)
        checks.apply_check_clears(conn, states)
        assert states["test-wall"].is_open
        # Correcting the command immediately resets the broken-check episode.
        assert checks.run_due_checks(conn, cfg=_cfg("exit 0"), now=now + 1801) == ["test-wall"]


def test_home_authority_uses_foreign_ids(kanban_home, monkeypatch):
    from hermes_cli import kanban_shared_blocker_checks as checks
    from hermes_cli import kanban_wake_check as wake

    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "foreign copy", blocker_id="test-wall")
        monkeypatch.setattr(wake, "foreign_ids", lambda _: {tid})
        assert checks.run_due_checks(conn, cfg=_cfg("exit 0")) == []
        assert not [e for e in kb.list_events(conn, tid) if e.kind == checks.CHECK_EVENT]
        monkeypatch.setattr(wake, "foreign_ids", lambda _: set())
        assert checks.run_due_checks(conn, cfg=_cfg("exit 0")) == ["test-wall"]


def test_reassert_during_check_discards_stale_success(kanban_home, monkeypatch):
    from hermes_cli import kanban_shared_blocker_checks as checks

    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "first", blocker_id="test-wall")

        def reassert(command):
            assert not conn.in_transaction
            _blocked_behind(conn, "new assertion", blocker_id="test-wall")
            return 0, "old observation"

        monkeypatch.setattr(checks, "_run_check", reassert)
        assert checks.run_due_checks(conn, cfg=_cfg("unused")) == []
        assert not [e for e in kb.list_events(conn, tid) if e.kind == checks.CHECK_EVENT]
        states = blockers.blocker_states(conn)
        checks.apply_check_clears(conn, states)
        assert states["test-wall"].is_open


def test_later_same_second_assertion_is_not_cleared_by_old_check(kanban_home):
    from hermes_cli import kanban_shared_blocker_checks as checks

    with kbc.connect_closing() as conn:
        _blocked_behind(conn, "first", blocker_id="test-wall")
        assert checks.run_due_checks(conn, cfg=_cfg("exit 0")) == ["test-wall"]
        _blocked_behind(conn, "second", blocker_id="test-wall")
        states = blockers.blocker_states(conn)
        checks.apply_check_clears(conn, states)
        assert states["test-wall"].is_open


def test_default_is_profile_scoped_and_overrides_can_disable(kanban_home):
    from hermes_cli import kanban_shared_blocker_checks as checks

    command = checks.registered_checks({})["github-rest-quota"]
    assert str(kanban_home / "bin" / "github_quota.py") in command
    assert command.endswith(" --reserve-ok")
    assert "github-rest-quota" not in checks.registered_checks(
        {"kanban": {"shared_blocker_checks": {"github-rest-quota": ""}}})


def test_shell_runs_outside_transaction_with_utf8_and_scrubbed_env(kanban_home, tmp_path):
    from hermes_cli import kanban_shared_blocker_checks as checks
    import shlex

    target = tmp_path / "observed.json"
    command = ("printf 'récovered'; "
               f"printf '{{\"home\":\"%s\"}}' \"$HERMES_HOME\" > {shlex.quote(str(target))}")
    with kbc.connect_closing() as conn:
        _blocked_behind(conn, "environment", blocker_id="test-wall")
        assert not conn.in_transaction
        assert checks.run_due_checks(conn, cfg=_cfg(command)) == ["test-wall"]
        assert not conn.in_transaction
    assert json.loads(target.read_text())["home"] == str(kanban_home)


def test_check_timeout_is_an_error_not_a_clear(kanban_home, monkeypatch):
    from hermes_cli import kanban_shared_blocker_checks as checks
    import shlex
    import sys

    monkeypatch.setattr(checks, "CHECK_TIMEOUT_SECONDS", 0.05)
    command = (f"{shlex.quote(sys.executable)} -c "
               + shlex.quote("import threading; threading.Event().wait()"))
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "hung checker", blocker_id="test-wall")
        assert checks.run_due_checks(conn, cfg=_cfg(command)) == []
        event = [e for e in kb.list_events(conn, tid) if e.kind == checks.CHECK_EVENT][-1]
        assert event.payload["rc"] == "timeout"
        assert event.payload["fails"] == 1
        states = blockers.blocker_states(conn)
        checks.apply_check_clears(conn, states)
        assert states["test-wall"].is_open


def test_unavailable_config_skips_commands_instead_of_killing_tick(kanban_home, monkeypatch):
    from hermes_cli import config
    from hermes_cli import kanban_shared_blocker_checks as checks
    from hermes_cli.config_backend import ConfigBackendUnavailable

    def unavailable():
        raise ConfigBackendUnavailable("offline")

    monkeypatch.setattr(config, "load_config", unavailable)
    assert checks.registered_checks() == {}


def test_check_refuses_caller_write_transaction(kanban_home):
    from hermes_cli import kanban_shared_blocker_checks as checks

    with kbc.connect_closing() as conn, kb.write_txn(conn):
        with pytest.raises(ValueError, match="outside a write transaction"):
            checks.run_due_checks(conn, cfg=_cfg("exit 0"))
