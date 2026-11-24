"""Cross-gateway capability routing: a wall on one gateway opens an owner card where the
capability lives (hub card t_01a17f851d7e9777). [daveey]

Replays the 10-26 22:39 PT block of hub t_5bc8e125: the hub role was denied EC2 Describe*,
the MacBook held AWS SSO admin for account 767406518141, and the card sat parked on a probe
for an hour with no producer until David asked.
"""
from __future__ import annotations

import json
import os
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
    # hub_cli_path() resolves `hermes-hub` through PATH first. On a host with the client installed
    # (hermes-hub, the MacBook: ~/.local/bin/hermes-hub) argv[0] became that absolute path, so these
    # asserts depended on the host. Drop PATH entries that carry a real hermes-hub.
    monkeypatch.setenv("PATH", os.pathsep.join(
        d for d in os.environ.get("PATH", "").split(os.pathsep) if d and not (Path(d) / "hermes-hub").exists()))
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
    from tests.tools.test_kanban_create_overlap import register_chat
    monkeypatch.setenv('HERMES_SESSION_ID', register_chat(home))
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
        # The no-producer-no-park contract sees a declared, live producer card.
        parked = [e for e in kb.list_events(conn, tid) if e.kind == "scheduled"][-1].payload
        assert parked["awaiting"]["kind"] == "card" and parked["awaiting"]["ref"] == rid
        assert parked["awaiting"]["gateway"] == "macbook" and "request local" in parked["awaiting"]["observed"]

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
    """Hours-old data (the holder's probe is down) refuses; a record that lapsed minutes ago parks and
    retries instead (test_kanban_capability_relay)."""
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


def test_rejected_owner_request_wakes_now_with_the_refusal(env):
    """A rejected request never makes an owner card: wake the card at once (0) with the refusal, never the
    "cannot tell" 2 that woke it only after three strikes as wake_check_broken (t_01a1d4dc5eb85b00)."""
    env["hub"].execute("INSERT INTO sent VALUES ('capability-owner:t_x:ssh:mini', ?, 'rejected', ?, 0, 0, 1)",
                       (json.dumps({"to_gw": "mini", "key": "default/default/new"}), json.dumps({"error": "no"})))
    env["hub"].commit()
    rc, msg = kcr.wait_check("capability-owner:t_x:ssh:mini")
    assert rc == 0 and "rejected by mini" in msg and "no" in msg, msg
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


# --- review follow-ups ---------------------------------------------------------------------------

def test_admin_covers_only_at_depth_three():
    assert not reg.satisfies("ssh:admin", "ssh:mini")
    assert reg.satisfies(f"aws:{ACCOUNT}:admin", f"aws:{ACCOUNT}:ec2-describe")


def test_request_ids_never_merge_distinct_needs():
    a = kcr.request_id("t_1", "token-broker:user/x")
    b = kcr.request_id("t_1", "token-broker:user-x")
    long1 = kcr.request_id("t_1", "aws:" + "a" * 150 + ":one")
    long2 = kcr.request_id("t_1", "aws:" + "a" * 150 + ":two")
    assert len({a, b}) == 2 and len({long1, long2}) == 2
    assert all(len(x) <= 128 for x in (a, b, long1, long2))
    assert kcr.request_id("t_1", NEED) == f"capability-owner:t_1:{NEED}"


def test_partial_failure_retry_resends_identical_envelopes(env, monkeypatch):
    now = time.time()
    publish(env["hub"], "macbook", {f"aws:{ACCOUNT}:admin": now})
    publish(env["hub"], "mini", {"ssh:mini-lan": now})
    real = kcr.hub_runner
    fail = {"on": "mini"}

    def flaky(argv):
        if argv[2] == fail["on"]:
            env["calls"].append(argv)
            return 2, "boom"
        return real(argv)

    monkeypatch.setattr(kcr, "hub_runner", flaky)
    with kbc.connect_closing() as conn:
        tid = running(conn)
        with pytest.raises(kcr.CapabilityRouteError) as exc:
            kcr.route(conn, tid, [NEED, "ssh:mini-lan"], reason=REASON_2239)
        assert exc.value.code == "capability_hub_unavailable"
        assert "retrying this call resends them unchanged" in str(exc.value)
        assert kb.get_task(conn, tid).status == "running"
        # The macbook holder record is re-checked meanwhile; the retry must still be an exact resend.
        publish(env["hub"], "macbook", {f"aws:{ACCOUNT}:admin": time.time() + 5})
        fail["on"] = None
        res = kcr.route(conn, tid, [NEED, "ssh:mini-lan"], reason=REASON_2239)
    assert [r["gateway"] for r in res["routes"]] == ["macbook", "mini"]
    mac = [c for c in env["calls"] if c[2] == "macbook"]
    assert len(mac) == 2 and mac[0] == mac[1]


def test_preflight_does_not_park_a_card_claimed_during_the_hub_call(env, monkeypatch):
    publish(env["hub"], "macbook", {f"aws:{ACCOUNT}:admin": time.time()})
    real = kcr.hub_runner
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="pilot", assignee="worker", requires=[NEED])

        def claim_meanwhile(argv):
            out = real(argv)
            with kbc.connect_closing() as other:
                assert kb.claim_task(other, tid, claimer="worker") is not None
            return out

        monkeypatch.setattr(kcr, "hub_runner", claim_meanwhile)
        placed = kcr.preflight(conn, tid)
        assert placed["code"] == "capability_route_invalid_state"
        assert kb.get_task(conn, tid).status == "running"


def test_owner_body_is_redacted(env):
    publish(env["hub"], "macbook", {f"aws:{ACCOUNT}:admin": time.time()})
    secret = "AKIA" + "Q" * 16
    with kbc.connect_closing() as conn:
        tid = running(conn)
        kcr.route(conn, tid, [NEED], reason=f"denied; key {secret} aws_secret_access_key=abcd1234abcd1234abcd")
    body = env["calls"][0][env["calls"][0].index("--body") + 1]
    assert secret not in body


def test_rejected_owner_request_may_be_rerouted_once(env):
    publish(env["hub"], "macbook", {f"aws:{ACCOUNT}:admin": time.time()})
    with kbc.connect_closing() as conn:
        tid = running(conn)
        kcr.route(conn, tid, [NEED], reason="x")
        rid = kcr.request_id(tid, NEED)
        env["hub"].execute("UPDATE sent SET state='rejected', result='{}' WHERE id=?", (rid,))
        env["hub"].commit()
        kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        res = kcr.route(conn, tid, [NEED], reason="x")
        assert res["routes"][0]["request_id"] == kcr.request_id(tid, NEED, 2) == f"{rid}::g2"
        env["hub"].execute("UPDATE sent SET state='rejected', result='{}' WHERE id=?", (f"{rid}::g2",))
        env["hub"].commit()
        kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        with pytest.raises(kcr.CapabilityRouteError) as exc:
            kcr.route(conn, tid, [NEED], reason="x")
    assert exc.value.code == "capability_route_exhausted"


def test_hub_collector_output_is_what_routing_reads(env, tmp_path, monkeypatch):
    """Cross-repo contract: the hermes-hub client's collect_capabilities record routes."""
    import importlib.util
    path = Path("/home/ubuntu/code/hermes-hub/client/hermes_hub.py")
    if not path.exists():
        pytest.skip("hermes-hub checkout not present")
    spec = importlib.util.spec_from_file_location("hub_client_contract", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "collect_capabilities"):
        pytest.skip("hermes-hub client predates the capabilities collection")
    peer = tmp_path / "peer"
    (peer / "kanban").mkdir(parents=True)
    (peer / "kanban" / "capabilities.json").write_text(json.dumps({
        "schema": 1, "refreshed_at": time.time(),
        "capabilities": [{"name": f"aws:{ACCOUNT}:admin", "ok": True, "checked_at": time.time()}]}))
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    rec = mod.collect_capabilities(peer)["info"]
    env["hub"].execute("INSERT INTO replica VALUES ('macbook', 'capabilities', 'info', 1, 1, ?, 0)",
                       (mod.canon(rec),))
    env["hub"].commit()
    assert reg.locate(NEED)["holders"][0][0] == "macbook"


def test_retry_after_partial_failure_keeps_queued_owner_even_if_holder_went_stale(env, monkeypatch):
    now = time.time()
    publish(env["hub"], "macbook", {f"aws:{ACCOUNT}:admin": now})
    publish(env["hub"], "mini", {"ssh:mini-lan": now})
    real = kcr.hub_runner
    fail = {"on": "mini"}

    def flaky(argv):
        if argv[2] == fail["on"]:
            return 2, "boom"
        return real(argv)

    monkeypatch.setattr(kcr, "hub_runner", flaky)
    with kbc.connect_closing() as conn:
        tid = running(conn)
        with pytest.raises(kcr.CapabilityRouteError):
            kcr.route(conn, tid, [NEED, "ssh:mini-lan"], reason=REASON_2239)
        publish(env["hub"], "macbook", {f"aws:{ACCOUNT}:admin": now - 3 * 3600})  # stale now
        fail["on"] = None
        res = kcr.route(conn, tid, [NEED, "ssh:mini-lan"], reason=REASON_2239)
        assert kb.get_task(conn, tid).status == "scheduled"
    assert [r["gateway"] for r in res["routes"]] == ["macbook", "mini"]


def test_background_refresh_never_runs_real_probes_under_tests(monkeypatch):
    monkeypatch.setenv("HERMES_TEST_ISOLATION", "1")
    assert reg.maybe_refresh_async() is False


# --- owner results as appliers report them (hub card t_01a181a33c586660) ----------------------

def _create_json_output(owner_tid: str) -> str:
    """What the MacBook applier stored on 10-28: `hermes kanban create --json` text in `output`, clipped
    to 2000 chars with a trailing ellipsis (hermes-hub `clip`), so it is not valid JSON."""
    text = json.dumps({"id": owner_tid, "title": "Capability owner: host:macbook for hub/t_x (verify)",
                       "body": "Capability owner card for waiting card hub/default/default/t_x. " + "x" * 3000,
                       "assignee": "default", "status": "ready"}, indent=2)
    return text[:1999] + "\u2026"


def _owner_result(hub, rid: str, result: dict, owner_tid: str = "t_01a18131b2b44af4", status: str = "done"):
    hub.execute("INSERT OR REPLACE INTO sent (id, body, state, result, created_at, done_at, posted) "
                "VALUES (?, ?, 'done', ?, 0, 0, 1)",
                (rid, json.dumps({"to_gw": "macbook", "key": "default/default/new"}), json.dumps(result)))
    hub.execute("INSERT OR REPLACE INTO replica VALUES ('macbook', 'tasks', ?, 1, 1, ?, ?)",
                (f"default/default/{owner_tid}", json.dumps({"id": owner_tid, "status": status}), int(time.time())))
    hub.commit()


@pytest.mark.parametrize("shape", ["task", "task_id", "id", "output_json", "output_clipped"])
def test_owner_status_reads_the_owner_id_from_every_applier_shape(env, shape):
    rid = "capability-owner:t_01a180daf776c25e:host:macbook"
    tid = "t_01a18131b2b44af4"
    result = {
        "task": {"task": tid},
        "task_id": {"ok": True, "created": True, "task_id": tid},
        "id": {"id": tid},
        "output_json": {"output": json.dumps({"id": tid, "status": "ready"}, indent=2)},
        "output_clipped": {"output": _create_json_output(tid)},
    }[shape]
    _owner_result(env["hub"], rid, result, tid, "done")
    assert kcr.owner_status(rid) == (0, f"{rid} -> macbook/{tid} done")
    _owner_result(env["hub"], rid, result, tid, "ready")
    assert kcr.owner_status(rid)[0] == 1


def test_owner_status_without_any_id_is_cannot_tell(env):
    rid = "capability-owner:t_x:host:macbook"
    for result in ({"output": "garbage"}, {"output": ""}, {}, {"output": {"no": "id"}}):
        _owner_result(env["hub"], rid, result)
        rc, msg = kcr.owner_status(rid)
        assert rc == 2 and "done without a task id" in msg, (result, msg)


def test_woken_card_needing_another_gateway_is_claimed_here_once_its_owner_is_done(env):
    """After the owner card is done the wake check exits 0 and the waiting card lands `ready` on THIS
    gateway, where a worker can claim it although `requires` names host:macbook (hub
    t_01a180daf776c25e). The need was routed and fulfilled; only its evidence is left to record."""
    from hermes_cli import kanban_wake_check as kwc
    need = "host:macbook"
    publish(env["hub"], "macbook", {need: time.time()})
    with kbc.connect_closing() as conn:
        tid = running(conn, requires=[need])
        kcr.route(conn, tid, [need], reason="verify on the MacBook")
        state = kwc.get_state(conn, tid)
    rid = kcr.request_id(tid, need)
    _owner_result(env["hub"], rid, {"output": _create_json_output("t_01a1814e609954cb")}, "t_01a1814e609954cb", "done")
    rc, out = kcr.wait_check(rid)
    assert rc == 0, out
    with kbc.connect_closing() as conn:
        assert kwc.apply_result(conn, tid, state["cmd"], rc, out, generation=state["generation"]) == \
            kwc.CONDITION_MET_EVENT
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.get_task(conn, tid).status == "running"


# --- owner create vs the waiting card itself (hub t_01a18669308f507f) ------------------------

# The owner title embeds this (truncated at 80). 9 shared words, score 0.6: the target's
# create_overlap matched the requester itself and wait-check exited 2 forever.
OVERLAP_TITLE = (
    "Fix prompt-surface lint renderer importing sysconfig after spoofing sys.platform on macOS"
)


def _parse_create_options(argv: list[str]) -> dict:
    opts = {}
    for i, arg in enumerate(argv):
        if arg == "--option" and i + 1 < len(argv):
            key, sep, raw = argv[i + 1].partition("=")
            assert sep, argv[i + 1]
            opts[key] = json.loads(raw)
    return opts


def _apply_owner_create(env, monkeypatch):
    """create-task as the target gateway applies it: overlap against open cards, reject or accept.

    A refusal is recorded on the sent request (state=rejected), which is what made
    ``wait-check`` exit 2. Acceptance records a done create with an owner id and no replica
    yet, so ``wait-check`` stays 1 (pending).
    """
    from hermes_cli import kanban_overlap

    applied: dict = {}

    def applying_hub(argv):
        rid = argv[argv.index("--id") + 1]
        title = argv[3]
        body = argv[argv.index("--body") + 1]
        opts = _parse_create_options(argv)
        env["hub"].execute(
            "INSERT INTO sent (id, body, state, created_at) VALUES (?, ?, 'local', ?)",
            (rid, json.dumps({"id": rid, "to_gw": argv[2], "args": {"title": title}}), time.time()))
        env["hub"].commit()
        matches = kanban_overlap.find(
            title=title, body=body, target_board="default", session_id=None, self_tid=None, parents=[])
        distinct = [str(x) for x in (opts.get("distinct_from") or [])]
        reason = str(opts.get("distinct_reason") or "")
        applied["matches"] = [m["id"] for m in matches]
        applied["distinct"] = distinct
        if matches and not kanban_overlap.covered(matches, distinct, reason):
            env["hub"].execute(
                "UPDATE sent SET state='rejected', result=? WHERE id=?",
                (json.dumps({"code": "create_overlap", "matches": [m["id"] for m in matches]}), rid))
            env["hub"].commit()
            applied["rejected"] = True
            return 0, rid
        owner = "t_01a18679owner1"
        env["hub"].execute(
            "UPDATE sent SET state='done', result=? WHERE id=?",
            (json.dumps({"task": owner}), rid))
        env["hub"].commit()
        applied["owner"] = owner
        return 0, rid

    monkeypatch.setattr(kcr, "hub_runner", applying_hub)
    return applied


def _route_overlapping(env, monkeypatch, *, stranger: bool):
    need = "host:macbook"
    publish(env["hub"], "macbook", {need: time.time()})
    applied = _apply_owner_create(env, monkeypatch)
    with kbc.connect_closing() as conn:
        awaited = kb.create_task(conn, title=OVERLAP_TITLE, assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (awaited,))
        extra = None
        if stranger:
            extra = kb.create_task(conn, title=OVERLAP_TITLE, assignee="worker")
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='running' WHERE id=?", (extra,))
        tid = kb.create_task(conn, title=OVERLAP_TITLE, assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "scheduled", {
                "awaiting": {"kind": "card", "ref": awaited, "why": "same work, already owned"},
                "reason": OVERLAP_TITLE,
            })
        try:
            kcr.route(conn, tid, [need], reason=OVERLAP_TITLE)
        except Exception as exc:
            applied["error"] = f"{type(exc).__name__}: {exc}"
    return tid, awaited, extra, kcr.request_id(tid, need), applied


def test_overlap_skips_the_waiting_card_a_capability_owner_quotes(env):
    """Target-side guard: even a create-task that drops --option must not match the waiting card."""
    from hermes_cli import kanban_overlap
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title=OVERLAP_TITLE, assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        sibling = kb.create_task(
            conn, title=f"Capability owner: host:macbook for hub/{tid} ({OVERLAP_TITLE[:80]})",
            assignee="worker")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (sibling,))
    title = f"Capability owner: ssh:mini for hub/{tid} ({OVERLAP_TITLE[:80]})"
    body = f"Capability owner card for waiting card hub/default/default/{tid}.\n"
    matches = kanban_overlap.find(
        title=title, body=body, target_board="default", session_id=None, self_tid=None, parents=[])
    ids = [m["id"] for m in matches]
    assert tid not in ids and sibling not in ids, ids


def test_old_create_task_client_still_queues_without_the_overlap_option(env, monkeypatch):
    """hermes-hub < 1.9.11 exits 2 on distinct_from. Routing must still queue the owner card."""
    need = "host:macbook"
    publish(env["hub"], "macbook", {need: time.time()})
    calls: list[list[str]] = []

    def old_client(argv):
        calls.append(list(argv))
        if any(str(a).startswith("distinct_from=") for a in argv):
            return 2, "create_option_unknown: distinct_from (allowed: skills)"
        rid = argv[argv.index("--id") + 1]
        env["hub"].execute(
            "INSERT INTO sent (id, body, state, created_at) VALUES (?, ?, 'local', ?)",
            (rid, json.dumps({"id": rid, "to_gw": argv[2]}), time.time()))
        env["hub"].commit()
        return 0, rid

    monkeypatch.setattr(kcr, "hub_runner", old_client)
    with kbc.connect_closing() as conn:
        tid = running(conn)
        out = kcr.route(conn, tid, [need], reason="need the MacBook")
    assert out["status"] == "scheduled"
    assert len(calls) == 2
    assert any(str(a).startswith("distinct_from=") for a in calls[0])
    assert not any(str(a).startswith("distinct_") for a in calls[1])
    assert kcr.wait_check(kcr.request_id(tid, need))[0] == 1


def test_capability_route_creates_owner_when_reason_duplicates_requester_title(env, monkeypatch):
    """The owner card quotes the waiting card. That must not be create_overlap against the
    waiting card or the cards it awaits, and wait-check must stay pending (1), not 2."""
    tid, awaited, _extra, rid, applied = _route_overlapping(env, monkeypatch, stranger=False)
    assert applied.get("owner"), applied
    assert not applied.get("error"), applied
    assert tid in applied["distinct"] and awaited in applied["distinct"], applied
    rc, msg = kcr.wait_check(rid)
    assert rc == 1, msg


def test_unrelated_title_twin_still_rejects_the_owner_create(env, monkeypatch):
    """Excluding the waiting card must not disable overlap. A different open card with the
    same title is still a second owner, and wait-check wakes the card with the rejection (0)."""
    _tid, awaited, stranger, rid, applied = _route_overlapping(env, monkeypatch, stranger=True)
    assert applied.get("rejected"), applied
    assert "create_overlap" in applied.get("error", ""), applied
    assert stranger in applied["matches"] and stranger not in applied["distinct"]
    assert awaited in applied.get("distinct", [])
    rc, msg = kcr.wait_check(rid)
    assert rc == 0 and "rejected" in msg and "create_overlap" in msg, msg
