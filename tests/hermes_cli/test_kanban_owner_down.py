"""A profile owner whose service is failed/masked/disabled holds the card; it never charges it. [daveey]

hermes-hub 2026-11-02 08:21Z: ``hermes-gateway-askeve-builder.service`` was ``failed`` (a pause stopped
it). t_01a19b8ff5c2ddc9 (assignee askeve-builder) was claimed twice in 40 s; each worker got ``Gateway
inaccessible: service_failed``, exited 1, was booked ``crashed``, and failure_limit=2 sent the card to
triage although no turn ran (self-heal defect kanban-ef16a9e091).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from hermes_cli import gateway_client, kanban_worker_client
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

GUE = gateway_client.GatewayUnavailableError
OWNER_DOWN_EXIT = 68
UNIT = "hermes-gateway-askeve-builder.service"


def _submit_env(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)
    for key, value in {"HERMES_KANBAN_BOARD": "default", "HERMES_KANBAN_TASK": "t1", "HERMES_KANBAN_RUN_ID": "7",
                       "HERMES_KANBAN_CLAIM_LOCK": "h:1", "HERMES_KANBAN_DB": str(tmp_path / "kanban.db")}.items():
        monkeypatch.setenv(key, value)


def _failed_owner(code="service_failed"):
    dials = [0]

    @asynccontextmanager
    async def _connect(*_, **__):
        dials[0] += 1
        raise GUE(f"Gateway inaccessible: {code}", "inaccessible", code)
        yield
    return _connect, dials


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(kbd, "_worker_final_output", lambda *a, **kw: "")
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: (lambda name: True))
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    for name in ("askeve-builder", "worker-ok"):
        profile = home / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text(_managed_yaml())
    kb.init_db()
    return home


def _exits(monkeypatch, code):
    from hermes_cli.kanban_db_dispatch_exit_code import exit_code_kind
    monkeypatch.setattr(kbd, "_classify_worker_exit", lambda pid: exit_code_kind(code))


def _running(conn, tid, pid):
    kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':', 1)[0]}:w{pid}")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))


def _down(monkeypatch, *, code="service_failed"):
    """The read-only service probe reports the askeve-builder owner as persistently down."""
    from hermes_cli import kanban_owner_down as od
    od._cache.clear()
    probes = []

    def _probe(profile):
        probes.append(profile)
        if profile != "askeve-builder":
            return None
        return od.OwnerDown(profile, code, UNIT, "exit 1")
    monkeypatch.setattr(od, "_probe_uncached", _probe)
    monkeypatch.setattr(od.sys, "platform", "linux")
    return probes


def test_worker_exits_owner_down_on_a_failed_service(monkeypatch, tmp_path, capsys):
    _submit_env(monkeypatch, tmp_path)
    connect, dials = _failed_owner()
    monkeypatch.setattr(gateway_client, "connect_gateway", connect)
    assert kanban_worker_client._submit() == OWNER_DOWN_EXIT
    assert dials[0] == 1  # persistent: no redial loop
    assert "Gateway inaccessible: service_failed" in capsys.readouterr().err


@pytest.mark.parametrize("code", ["service_masked", "service_disabled"])
def test_worker_exits_owner_down_on_masked_or_disabled(monkeypatch, tmp_path, code):
    _submit_env(monkeypatch, tmp_path)
    connect, _ = _failed_owner(code)
    monkeypatch.setattr(gateway_client, "connect_gateway", connect)
    assert kanban_worker_client._submit() == OWNER_DOWN_EXIT


def test_owner_down_service_failed_parks_uncounted(home, monkeypatch, tmp_path):
    """The 11-02 sequence end to end: two real worker exits on ``service_failed`` under failure_limit=2
    cost the card nothing, and the next tick parks it ``owner_down`` instead of spawning again."""
    connect, _ = _failed_owner()
    monkeypatch.setattr(gateway_client, "connect_gateway", connect)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ask eve work", assignee="askeve-builder")
        for pid in (71001, 71002):
            _running(conn, tid, pid)
            _submit_env(monkeypatch, tmp_path)
            rc = kanban_worker_client._submit()
            monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
            _exits(monkeypatch, rc)
            kbd.detect_crashed_workers(conn)
            task = kb.get_task(conn, tid)
            assert task.status == "ready" and task.consecutive_failures == 0
        outcomes = [r["outcome"] for r in conn.execute("SELECT outcome FROM task_runs WHERE task_id=?", (tid,))]
        assert outcomes == ["owner_down", "owner_down"]
        _down(monkeypatch)
        spawned = []
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(a) or None)
        task = kb.get_task(conn, tid)
        assert task.status == "scheduled" and task.consecutive_failures == 0
        assert not spawned
        reason = conn.execute("SELECT summary FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                              (tid,)).fetchone()[0]
        assert reason.startswith("owner_down:askeve-builder:service_failed")


def test_preflight_skips_claim_for_failed_owner(home, monkeypatch):
    probes = _down(monkeypatch)
    with kbc.connect() as conn:
        down = kb.create_task(conn, title="held", assignee="askeve-builder")
        up = kb.create_task(conn, title="runs", assignee="worker-ok")
        spawned = []
        res = kbd.dispatch_once(conn, spawn_fn=lambda task, *a, **k: spawned.append(task.id) or None)
        assert spawned == [up]
        assert kb.get_task(conn, down).status == "scheduled"
        runs = conn.execute("SELECT outcome FROM task_runs WHERE task_id=?", (down,)).fetchall()
        assert [r[0] for r in runs] == ["scheduled"]  # the park's own record; no worker run was claimed
        assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind IN ('claimed','spawned')",
                                (down,)).fetchone()
        assert (down, "askeve-builder", "service_failed") in res.owner_down
        wake = conn.execute("SELECT cmd FROM task_wake_checks WHERE task_id=?", (down,)).fetchone()
        assert wake and wake[0] == "exit 1"
        assert "default" not in probes  # the default profile is never probed


def test_owner_down_dedup_card(home, monkeypatch):
    _down(monkeypatch)
    with kbc.connect() as conn:
        fixer = kb.create_task(conn, title="Ask Eve: durable pinned-owner units", body=f"repair {UNIT}",
                               assignee="default")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (fixer,))
        held = [kb.create_task(conn, title=f"held {i}", assignee="askeve-builder") for i in range(3)]
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None)
        cards = conn.execute("SELECT id, status FROM tasks WHERE title=?",
                             ("profile askeve-builder owner down: service_failed",)).fetchall()
        assert len(cards) == 1
        card = cards[0][0]
        assert [p for p in kb.parent_ids(conn, card)] == [fixer]
        assert cards[0][1] == "todo"  # waits on the open card that owns the unit
        comments = [r[0] for r in conn.execute("SELECT body FROM task_comments WHERE task_id=?", (card,))]
        assert len(comments) == 2 and all("Also holding" in c for c in comments)
        assert all(kb.get_task(conn, t).status == "scheduled" for t in held)
        # A later sighting while the card is open comments; once it is done a fresh one is filed.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (card,))
        late = kb.create_task(conn, title="late", assignee="askeve-builder")
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: None)
        assert kb.get_task(conn, late).status == "scheduled"
        open_cards = conn.execute("SELECT id FROM tasks WHERE title=? AND status != 'done'",
                                  ("profile askeve-builder owner down: service_failed",)).fetchall()
        assert len(open_cards) == 1 and open_cards[0][0] != card


def test_unknown_refusal_code_keeps_todays_path(monkeypatch, tmp_path):
    _submit_env(monkeypatch, tmp_path)
    connect, _ = _failed_owner("unsafe_control_peer")
    monkeypatch.setattr(gateway_client, "connect_gateway", connect)
    assert kanban_worker_client._submit() == 1


def test_wake_check_command_reads_unit_state(tmp_path):
    from hermes_cli import kanban_owner_down as od
    import subprocess
    fake = tmp_path / "systemctl"
    for state, rc in (("failed\nenabled", 1), ("inactive\nmasked", 1), ("active\nenabled", 0)):
        fake.write_text(f"#!/bin/sh\nprintf '{state}\\n'\n")
        fake.chmod(0o755)
        cmd = od.wake_check_for(UNIT)
        out = subprocess.run(["bash", "-c", cmd], env={"PATH": f"{tmp_path}:/usr/bin:/bin"}, check=False)
        assert out.returncode == rc, state
    fake.write_text("#!/bin/sh\nexit 1\n")
    assert subprocess.run(["bash", "-c", od.wake_check_for(UNIT)], env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
                          check=False).returncode == 2
