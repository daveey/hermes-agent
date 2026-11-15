"""Bounded protocol-2 helper spike; real Git/journal, Board transport seam only."""
from __future__ import annotations

import importlib
import importlib.util
import subprocess
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_source_board as board
from hermes_cli import kanban_source_launch as launch
from hermes_cli import kanban_workspace_source as ws
from tests.hermes_cli import kanban_source_support as fake
from tests.hermes_cli import kanban_sync_support as support


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    gen = support.synced_conn(home)
    conn = next(gen)
    b = fake.FakeSourceBoard()
    previous = fake.install(b)
    sync.set_mode(conn, "primary")
    tid = kb.create_task(conn, title="scratch source", assignee="worker")
    assert kb.claim_task(conn, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
    clone = tmp_path / "clone"
    clone.mkdir()
    git(clone, "init")
    git(clone, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "seed")
    source = tmp_path / "source"
    git(clone, "worktree", "add", "-b", f"wt/{tid}", str(source))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    conn.execute("UPDATE tasks SET workspace_kind='scratch', workspace_path=? WHERE id=?", (str(scratch), tid))
    conn.commit()
    # Real clone/common-dir/origin validation; only external GitHub reads are stubbed.
    from hermes_cli import kanban_source_clones as clones
    git(clone, "remote", "add", "origin", "https://github.com/example/source")
    original_git = clones.git

    def local_git(path, *args, **kwargs):
        if args == ("ls-remote", "origin", "HEAD"):
            return subprocess.CompletedProcess(args, 0, "", "")
        return original_git(path, *args, **kwargs)

    monkeypatch.setattr(clones, "git", local_git)
    monkeypatch.setattr(clones, "github_repo", lambda name, timeout=30: {"id": 1, "full_name": name})
    cfg = ws.Settings(enabled=True, requested=True, clones={"example/source": str(clone)}, publish_on_park=True,
                      push_timeout_s=30, preflight_timeout_s=60, materialize_timeout_s=None)
    monkeypatch.setattr(ws, "settings", lambda: cfg)
    yield conn, b, tid, source, scratch, home, clone
    sync.set_lease_client(previous)
    gen.close()


def api():
    name = "hermes_cli.kanban_source_registration"
    assert importlib.util.find_spec(name) is not None, "explicit protocol-2 registration helper is missing"
    return importlib.import_module(name)


def test_initial_records_identity_before_protocol2_rpc(setup):
    conn, b, tid, source, scratch, _home, _clone = setup
    reg = api()
    answer = b.answer

    def inspect(body):
        run = board.attached_run(conn, kb.get_task(conn, tid).current_run_id)
        assert run.state == "pending" and run.path == str(source)
        assert run.machine == sync._state(conn)["gateway_id"] and run.source_protocol == 2
        assert body["mode"] == "initial" and body["source_protocol"] == 2 and body["manifest_revision"] == 0
        return answer(body)

    b.answer = inspect
    run = reg.register_initial(conn, tid, str(source))
    assert run.state == "attached"
    assert kb.get_task(conn, tid).workspace_path == str(scratch)
    assert reg.recover_local(conn, tid, {"task_id": tid, "run_uid": run.run_uid, "machine": run.machine}).path == str(source)


def test_unanswered_initial_replays_durable_observation_on_new_connection(setup):
    from hermes_cli import kanban_db_connect as connect
    conn, b, tid, source, scratch, home, _clone = setup
    reg = api()
    b.offline.add("source_attach")
    with pytest.raises(sync.LeaseUnavailable):
        reg.register_initial(conn, tid, str(source))
    old = board.attached_run(conn, kb.get_task(conn, tid).current_run_id)
    assert old.state == "pending"
    b.offline.clear()
    fresh = connect.connect(home / "kanban.db")
    try:
        restored = reg.register_initial(fresh, tid, str(source))
        assert restored.run_uid == old.run_uid and restored.pre_head == old.pre_head
        assert restored.state == "attached" and kb.get_task(fresh, tid).workspace_path == str(scratch)
    finally:
        fresh.close()


@pytest.mark.parametrize("fault", [None, "null_identity", "unknown_identity", "foreign_identity", "lease_run",
                                  "lease_task", "lease_holder", "lease_expired", "lease_nan", "lease_inf", "custody_holder", "protocol", "revision",
                                  "retained_run", "missing_retained_run"])
def test_successor_uses_grant_predecessor_after_attached_uid_is_null(setup, fault):
    from hermes_cli import kanban_db_connect as connect
    conn, b, tid, source, scratch, home, _clone = setup
    reg = api()
    old = reg.register_initial(conn, tid, str(source))
    conn.execute("UPDATE task_runs SET status='released', ended_at=? WHERE id=?", (int(time.time()), old.run_id))
    conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, claim_expires=NULL WHERE id=?", (tid,))
    conn.commit()
    assert kb.claim_task(conn, tid, ttl_seconds=900, claimer=f"{kb._host_prefix()}:1")
    uid = sync.run_uid(conn, kb.get_task(conn, tid).current_run_id)
    grant = {"status": "applied", "source_predecessor": {"task_id": tid, "run_uid": old.run_uid, "machine": old.machine},
             "lease": {"task_id": tid, "run_uid": uid, "holder": old.machine, "expires": int(time.time() * 1000) + 900000}}
    custody = {"attached_run_uid": None, "source_holder": old.machine, "source_protocol": 2,
               "manifest_revision": 0, "state": "retained", "retained_run_uid": old.run_uid}
    if fault:
        if fault == "null_identity":
            grant["source_predecessor"] = None
        elif fault == "unknown_identity":
            grant["source_predecessor"]["run_uid"] = "missing"
        elif fault == "foreign_identity":
            grant["source_predecessor"]["machine"] = "elsewhere"
        elif fault in {"lease_nan", "lease_inf"}:
            grant["lease"]["expires"] = float(fault.removeprefix("lease_"))
        elif fault == "retained_run":
            custody["retained_run_uid"] = "different-prior-run"
        elif fault == "missing_retained_run":
            custody.pop("retained_run_uid")
        elif fault.startswith("lease_"):
            key = {"lease_run": "run_uid", "lease_task": "task_id", "lease_holder": "holder", "lease_expired": "expires"}[fault]
            grant["lease"][key] = 1 if fault == "lease_expired" else "foreign"
        else:
            key = {"custody_holder": "source_holder", "protocol": "source_protocol", "revision": "manifest_revision"}[fault]
            custody[key] = "elsewhere" if fault == "custody_holder" else 1
        calls = len(b.calls)
        with pytest.raises(launch.AttachRefused, match="source_identity_refused"):
            reg.resume_premanifest(conn, tid, grant, custody)
        assert len(b.calls) == calls
        return
    fresh = connect.connect(home / "kanban.db")
    try:
        restored = reg.resume_premanifest(fresh, tid, grant, custody)
        assert restored.path == old.path and restored.run_uid == uid and restored.mode == "resume"
        assert kb.get_task(fresh, tid).workspace_path == str(scratch)
        receipt = launch.abort_receipt(restored, not_started=True)
        assert receipt["state"] == "unchanged" and receipt["abort"] == "pre_spawn"
    finally:
        fresh.close()
    assert b.body("source_attach")["source_protocol"] == 2


@pytest.mark.parametrize("identity", [None, {}, {"task_id": "foreign", "run_uid": "missing", "machine": "elsewhere"}])
def test_unknown_or_foreign_identity_fails_closed(setup, identity):
    conn, b, tid, _source, _scratch, _home, _clone = setup
    reg = api()
    with pytest.raises(launch.AttachRefused, match="source_identity_refused"):
        reg.recover_local(conn, tid, identity)
    assert b.actions("source_attach") == []


def test_initial_abort_never_claims_pre_spawn(setup):
    conn, _b, tid, source, _scratch, _home, _clone = setup
    run = api().register_initial(conn, tid, str(source))
    receipt = launch.abort_receipt(run, not_started=True)
    assert receipt["state"] == "retained" and "abort" not in receipt


@pytest.mark.parametrize("fault", ["unknown_uid", "foreign_machine", "foreign_task", "old_protocol"])
def test_exact_identity_must_match_durable_record(setup, fault):
    conn, _b, tid, source, _scratch, _home, _clone = setup
    reg = api()
    run = reg.register_initial(conn, tid, str(source))
    identity = {"task_id": tid, "run_uid": run.run_uid, "machine": run.machine}
    if fault == "unknown_uid":
        identity["run_uid"] = "missing"
    elif fault == "foreign_machine":
        conn.execute("UPDATE kanban_source_runs SET machine='elsewhere'")
    elif fault == "foreign_task":
        conn.execute("UPDATE kanban_source_runs SET task_id='another'")
    else:
        conn.execute("UPDATE kanban_source_runs SET source_protocol=1")
    conn.commit()
    with pytest.raises(launch.AttachRefused, match="source_identity_refused"):
        reg.recover_local(conn, tid, identity)


@pytest.mark.parametrize("fault", ["clone", "unregistered", "foreign_branch", "expired", "occupied"])
def test_initial_preconditions_refuse_without_rpc(setup, fault):
    conn, b, tid, source, _scratch, _home, clone = setup

    def expire():
        conn.execute("UPDATE tasks SET lease_expires=1 WHERE id=?", (tid,))
        conn.commit()

    def occupy():
        other = kb.create_task(conn, title="another owner", assignee="worker")
        conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(source), other))
        conn.commit()

    faults = {"clone": lambda: None, "unregistered": ws.settings().clones.clear,
              "foreign_branch": lambda: git(source, "switch", "-c", "unrelated"),
              "expired": expire, "occupied": occupy}
    faults[fault]()
    if fault == "clone":
        source = clone
    with pytest.raises(launch.AttachRefused, match="source_identity_refused") as info:
        api().register_initial(conn, tid, str(source))
    assert b.actions("source_attach") == []
    # The worker sees these through kanban_heartbeat: each names how to fix it.
    guidance = {"clone": "worktree add", "unregistered": "kanban.workspace_source.clones",
                "foreign_branch": f"wt/{tid}"}
    if fault in guidance:
        assert guidance[fault] in info.value.detail
