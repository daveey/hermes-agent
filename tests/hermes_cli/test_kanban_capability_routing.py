"""Cross-gateway capability routing: a wall on one gateway opens an owner card where the
capability lives (hub card t_01a17f851d7e9777). [daveey]

Replays the 10-26 22:39 PT block of hub t_5bc8e125: the hub role was denied EC2 Describe*,
the MacBook held AWS SSO admin for account 767406518141, and the card sat parked on a probe
for an hour with no producer until David asked.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_capability_registry as reg
from hermes_cli import kanban_capability_routing as kcr
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

ACCOUNT = "767406518141"
NEED = f"aws:{ACCOUNT}:ec2-describe"
# hub t_5bc8e125 run 2494 (blocked 10-26 22:39 PT, kind=transient, no human_request)
REASON_2239 = (
    "Live pilot blocked on AWS authority; nothing provisioned, no spend or routing change. The hub role "
    "hermes-hub in 767406518141 is denied on ec2:DescribeVpcs, DescribeInstanceTypes, "
    "autoscaling:DescribeAutoScalingGroups and SecretsManager ListSecrets (re-verified this run). The "
    "earlier audit read the account through the MacBook SSO profile david-bloomin-sso (us-west-2), which "
    "the hub worker cannot use. Retry once any route works from the hub."
)

HUB_SCHEMA = """
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE replica (gw TEXT, coll TEXT, key TEXT, version INTEGER, seq INTEGER, data TEXT,
  updated_at INTEGER, PRIMARY KEY (gw, coll, key));
CREATE TABLE sent (id TEXT PRIMARY KEY, body TEXT, state TEXT, result TEXT, created_at REAL,
  done_at REAL, posted INTEGER DEFAULT 0);
"""


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    mail = tmp_path / "mail.json"
    mail.write_text(json.dumps({"gateway": "hub"}))
    monkeypatch.setenv("HERMES_MAIL_CONFIG", str(mail))
    state = tmp_path / "hub-state"
    state.mkdir()
    monkeypatch.setenv("HERMES_HUB_STATE", str(state))
    hub = sqlite3.connect(state / "hub.db")
    hub.executescript(HUB_SCHEMA)
    hub.execute("INSERT INTO meta VALUES ('gateway', 'hub')")
    hub.commit()
    calls: list[list[str]] = []

    def fake_hub(argv):
        # What `hermes-hub create-task` does locally: journal the request in `sent`.
        calls.append(argv)
        assert argv[1] == "create-task"
        rid = argv[argv.index("--id") + 1]
        body = {"id": rid, "to_gw": argv[2], "coll": "tasks", "key": "default/default/new", "action": "create",
                "args": {"title": argv[3], "body": argv[argv.index("--body") + 1],
                         "assignee": argv[argv.index("--assignee") + 1]}}
        hub.execute("INSERT INTO sent (id, body, state, created_at) VALUES (?, ?, 'local', ?) ON CONFLICT(id) DO NOTHING",
                    (rid, json.dumps(body), time.time()))
        hub.commit()
        return 0, rid

    monkeypatch.setattr(kcr, "hub_runner", fake_hub)
    kb.init_db()
    yield {"home": home, "hub": hub, "calls": calls}
    hub.close()


def publish(hub, gw: str, caps: dict[str, float], ok: bool = True):
    """A peer's capabilities record as hermes-hub replicates it (coll=capabilities, key=info)."""
    rec = {"schema": 1, "gateway": gw, "refreshed_at": max(caps.values()),
           "capabilities": [{"name": n, "ok": ok, "checked_at": at} for n, at in caps.items()]}
    hub.execute("INSERT OR REPLACE INTO replica VALUES (?, 'capabilities', 'info', 1, 1, ?, ?)",
                (gw, json.dumps(rec), int(time.time())))
    hub.commit()


def running(conn, **kw) -> str:
    tid = kb.create_task(conn, title="ASG persistent runner pilot", assignee="worker", **kw)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid


def as_worker(monkeypatch, conn, tid):
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb.get_task(conn, tid).current_run_id))


def owner_applies(hub, rid: str, owner_tid: str, status: str):
    """The MacBook applies the create request and its card status replicates back."""
    hub.execute("UPDATE sent SET state='done', result=? WHERE id=?", (json.dumps({"task": owner_tid}), rid))
    hub.execute("INSERT OR REPLACE INTO replica VALUES ('macbook', 'tasks', ?, 1, 1, ?, ?)",
                (f"default/default/{owner_tid}", json.dumps({"id": owner_tid, "status": status}), int(time.time())))
    hub.commit()


# --- names -----------------------------------------------------------------------------------

@pytest.mark.parametrize("held,need,ok", [
    (f"aws:{ACCOUNT}:admin", NEED, True),
    (f"aws:{ACCOUNT}:admin", f"aws:{ACCOUNT}", True),
    (f"aws:{ACCOUNT}:admin", "aws:999999999999:ec2-describe", False),
    (NEED, NEED, True),
    (NEED, f"aws:{ACCOUNT}:ec2-mutate", False),
    ("ssh:mini", "ssh:mini", True),
    ("ssh:mini", "ssh:hub", False),
    ("github:daveey:admin", "github:daveey:repo", True),
])
def test_satisfies(held, need, ok):
    assert reg.satisfies(held, need) is ok


def test_needs_are_namespaced_and_pass_through_profile_requires(env):
    with pytest.raises(ValueError):
        reg.normalize_needs(["terminal"])
    assert reg.normalize_needs([NEED, NEED, "ssh:mini"]) == [NEED, "ssh:mini"]
    from hermes_cli.kanban_capabilities import validate_assignment
    # A gateway need never fails profile policy and is kept on the card.
    assert validate_assignment("worker", requires=[NEED]) == [NEED]


# --- acceptance: replay of the 22:39 block ----------------------------------------------------

def test_replay_t5bc8e125_block_opens_macbook_owner_card_and_parks(env, monkeypatch):
    from tools import kanban_tools as kt
    now = time.time()
    publish(env["hub"], "macbook", {f"aws:{ACCOUNT}:admin": now - 60})
    publish(env["hub"], "mini", {"ssh:hub": now - 60})
    with kbc.connect_closing() as conn:
        tid = running(conn)
        as_worker(monkeypatch, conn, tid)
    out = json.loads(kt._handle_block({"reason": REASON_2239, "kind": "transient", "needs": [NEED]}))
    assert out.get("status") == "scheduled", out
    rid = kcr.request_id(tid, NEED)
    assert rid == f"capability-owner:{tid}:{NEED}"
    assert [r["gateway"] for r in out["capability_routes"]] == ["macbook"]

    # ONE owner card requested on the holder, through hermes-hub create-task.
    (argv,) = env["calls"]
    assert argv[:3] == ["hermes-hub", "create-task", "macbook"]
    assert argv[argv.index("--id") + 1] == rid
    assert argv[argv.index("--origin-session") + 1] == ""
    body = argv[argv.index("--body") + 1]
    assert "david-bloomin-sso" in body and NEED in body
    assert "human_request" in body and f"hub/default/default/{tid}" in body

    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "scheduled" and task.human_request is None
        state = conn.execute("SELECT cmd, every FROM task_wake_checks WHERE task_id=?", (tid,)).fetchone()
        assert state[0] == f"hermes kanban wait-check capability-owner {rid}" and state[1] == kcr.WAKE_EVERY
        routed = [e for e in kb.list_events(conn, tid) if e.kind == kcr.ROUTED_EVENT][-1].payload
        assert routed["routes"][0]["request_id"] == rid
        assert routed["awaiting"][0] == {"kind": "card", "ref": f"macbook/request/{rid}",
                                         "why": f"capability {NEED}"}

    # The hub card waits on the owner card: pending -> running -> done wakes it.
    assert kcr.wait_check(rid)[0] == 1
    owner_applies(env["hub"], rid, "t_owner1", "running")
    assert kcr.wait_check(rid) == (1, f"{rid} -> macbook/t_owner1 running")
    owner_applies(env["hub"], rid, "t_owner1", "done")
    assert kcr.wait_check(rid)[0] == 0


def test_second_route_of_same_need_is_exhausted_not_a_new_owner(env, monkeypatch):
    publish(env["hub"], "macbook", {f"aws:{ACCOUNT}:admin": time.time()})
    with kbc.connect_closing() as conn:
        tid = running(conn)
        kcr.route(conn, tid, [NEED], reason=REASON_2239)
        kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        with pytest.raises(kcr.CapabilityRouteError) as exc:
            kcr.route(conn, tid, [NEED], reason="again")
    assert exc.value.code == "capability_route_exhausted"
    assert len(env["calls"]) == 1


# --- acceptance: refusals ---------------------------------------------------------------------

def test_need_no_gateway_holds_gets_typed_refusal_pointing_at_human_request(env, monkeypatch):
    from tools import kanban_tools as kt
    publish(env["hub"], "macbook", {f"aws:{ACCOUNT}:admin": time.time()})
    with kbc.connect_closing() as conn:
        tid = running(conn)
        as_worker(monkeypatch, conn, tid)
    out = json.loads(kt._handle_block({"reason": "need billing admin", "needs": ["aws:111111111111:billing"]}))
    assert out.get("rejection") == "no_capability_holder", out
    assert "human_request" in out["error"]
    assert env["calls"] == []
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "running"


def test_stale_registry_data_triggers_no_routing(env, monkeypatch):
    from tools import kanban_tools as kt
    publish(env["hub"], "macbook", {f"aws:{ACCOUNT}:admin": time.time() - 2 * 3600})
    with kbc.connect_closing() as conn:
        tid = running(conn)
        as_worker(monkeypatch, conn, tid)
    out = json.loads(kt._handle_block({"reason": REASON_2239, "needs": [NEED]}))
    assert out.get("rejection") == "capability_registry_stale", out
    assert "macbook" in out["error"]
    assert env["calls"] == []
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "running"
        assert not [e for e in kb.list_events(conn, tid) if e.kind == kcr.ROUTED_EVENT]


def test_not_held_entry_never_routes(env):
    publish(env["hub"], "macbook", {f"aws:{ACCOUNT}:admin": time.time()}, ok=False)
    with kbc.connect_closing() as conn:
        tid = running(conn)
        with pytest.raises(kcr.CapabilityRouteError) as exc:
            kcr.route(conn, tid, [NEED], reason="x")
    assert exc.value.code == "no_capability_holder"


def test_capability_held_locally_is_not_routed(env):
    publish(env["hub"], "macbook", {f"aws:{ACCOUNT}:admin": time.time()})
    reg.registry_path().parent.mkdir(parents=True, exist_ok=True)
    reg.registry_path().write_text(json.dumps({"schema": 1, "refreshed_at": time.time(), "capabilities": [
        {"name": NEED, "ok": True, "checked_at": time.time()}]}))
    with kbc.connect_closing() as conn:
        tid = running(conn)
        with pytest.raises(kcr.CapabilityRouteError) as exc:
            kcr.route(conn, tid, [NEED], reason="x")
    assert exc.value.code == "capability_held_locally"
    assert env["calls"] == []


def test_needs_with_human_request_is_refused(env, monkeypatch):
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        tid = running(conn)
        as_worker(monkeypatch, conn, tid)
    out = json.loads(kt._handle_block({"reason": "x", "needs": [NEED], "kind": "needs_input",
                                       "human_request": {"action": "a", "why_human": "b", "interaction": "approval",
                                                         "scope": "c", "evidence": "d"}}))
    assert "error" in out and env["calls"] == []


def test_rejected_owner_request_is_cannot_tell_not_not_yet(env):
    env["hub"].execute("INSERT INTO sent VALUES ('capability-owner:t_x:ssh:mini', ?, 'rejected', ?, 0, 0, 1)",
                       (json.dumps({"to_gw": "mini", "key": "default/default/new"}), json.dumps({"error": "no"})))
    env["hub"].commit()
    assert kcr.wait_check("capability-owner:t_x:ssh:mini")[0] == 2
    assert kcr.wait_check("capability-owner:t_unknown:ssh:mini")[0] == 2


# --- placement preflight ----------------------------------------------------------------------

def test_card_declaring_needs_is_prerouted_at_creation(env, monkeypatch):
    from tools import kanban_tools as kt
    publish(env["hub"], "macbook", {f"aws:{ACCOUNT}:admin": time.time()})
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    out = json.loads(kt._handle_create({"title": "ASG pilot", "assignee": "worker", "body": "Provision the pilot",
                                        "requires": [NEED]}))
    assert out.get("ok") is not False and out.get("task_id"), out
    tid = out["task_id"]
    placed = out["capability_placement"]
    assert placed["routes"][0]["gateway"] == "macbook"
    assert out["status"] == "scheduled"
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.requires == [NEED]
        assert task.status == "scheduled"
    assert env["calls"][0][2] == "macbook"


def test_preflight_without_holder_creates_with_warning(env, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    out = json.loads(kt._handle_create({"title": "t", "assignee": "worker", "requires": ["ssh:nowhere"]}))
    assert out["task_id"] and out["status"] == "ready"
    assert out["capability_placement"]["code"] == "no_capability_holder"
    assert env["calls"] == []


# --- registry ---------------------------------------------------------------------------------

def test_refresh_live_checks_configured_probes(env):
    cfg = {"builtin": False, "probes": {f"aws:{ACCOUNT}:admin": "true", "browser:signed-in": "false"},
           "ssh_hosts": {}}
    rec = reg.refresh(cfg)
    by = {c["name"]: c for c in rec["capabilities"]}
    assert by[f"aws:{ACCOUNT}:admin"]["ok"] is True
    assert by["browser:signed-in"]["ok"] is False
    assert all(abs(c["checked_at"] - time.time()) < 60 for c in by.values())
    assert rec["gateway"] == "hub"
    # Metadata only: the file never carries probe commands or output.
    text = reg.registry_path().read_text()
    assert "true" not in text.replace('"ok": true', "")
    assert reg.read_local()["capabilities"] == rec["capabilities"]


# --- CLI ----------------------------------------------------------------------------------------

def _cli(*argv: str) -> int:
    import argparse

    from hermes_cli import kanban as kc
    top = argparse.ArgumentParser()
    kc.build_parser(top.add_subparsers(dest="_top"))
    return kc.kanban_command(top.parse_args(["kanban", *argv]))


def test_cli_block_need_routes_and_wait_check_reads_the_owner(env, capsys):
    publish(env["hub"], "macbook", {f"aws:{ACCOUNT}:admin": time.time()})
    with kbc.connect_closing() as conn:
        tid = running(conn)
    assert _cli("block", tid, REASON_2239, "--need", NEED) == 0
    assert "requested on macbook" in capsys.readouterr().out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "scheduled"
    rid = kcr.request_id(tid, NEED)
    assert _cli("wait-check", "capability-owner", rid) == 1
    owner_applies(env["hub"], rid, "t_owner2", "done")
    assert _cli("wait-check", "capability-owner", rid) == 0


def test_cli_block_need_without_holder_exits_2_and_keeps_card(env, capsys):
    with kbc.connect_closing() as conn:
        tid = running(conn)
    assert _cli("block", tid, "x", "--need", "ssh:nowhere") == 2
    assert "no_capability_holder" in capsys.readouterr().err
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "running"
