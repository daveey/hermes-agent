"""The kanban_create tool consults the hub admission judge on every hub-bound path. [daveey]

Fake judge only (never a live model): hub auto create with ``hub_reason``, ``force_hub`` inside and
outside a dispatcher worker, ``runs_on machine:hub`` keeping its explicit intent, and an explicit
``placement="hub"`` from another gateway (denied = refused, nothing forwarded).
"""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_placement as placement
from tests.hermes_cli import test_kanban_portable_reason_audit as audit_support
from tests.hermes_cli import hub_judge_support
from tools import kanban_tools as kt

kanban_home = audit_support.kanban_home
hub_judge = hub_judge_support.hub_judge
MARKED = "Read ~/.hermes/logs/agent.log on the hub"


def _events(tid, kind="hub_admission"):
    with kbc.connect() as conn:
        return [e.payload for e in kb.list_events(conn, tid) if e.kind == kind]


@pytest.fixture
def synced(kanban_home):
    """A board that syncs as primary, so ``runs_on`` rows exist."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as ksc
    (kanban_home / "config.yaml").write_text("kanban:\n  sync:\n    enabled: true\n  placement:\n"
                                             "    worker_pools:\n      hub: [worker-1]\n")
    kb.init_db()
    with kbc.connect(kanban_home / "kanban.db") as db:
        ksc.ensure_identity(db, "default", "hub")
        sync.set_mode(db, "primary")
    return kanban_home


@pytest.fixture
def on_gateway(kanban_home, monkeypatch, hub_judge):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKER_TASK", raising=False)
    monkeypatch.setattr(kt, "_persisted_identity", lambda: "default")
    from tests.tools.test_kanban_create_overlap import register_chat
    chat = register_chat(kanban_home, "20261123_120000_hub_admission_owner")
    monkeypatch.setenv("HERMES_SESSION_ID", chat)

    def use(gateway):
        monkeypatch.setattr(placement.reg, "self_gateway", lambda: gateway)
        return hub_judge
    return use


@pytest.mark.parametrize("admit", [True, False])
def test_tool_hub_auto_create_passes_hub_reason(on_gateway, admit):
    judge = on_gateway("hub")
    judge.admit_next = admit
    result = json.loads(kt._handle_create({"title": MARKED, "requires": [],
                                           "hub_reason": "needs the hub's signed-in Chrome"}))
    assert result.get("ok"), result
    assert [c["hub_reason"] for c in judge.calls] == ["needs the hub's signed-in Chrome"]
    (event,) = _events(result["task_id"])
    assert event["admit"] is admit
    with kbc.connect() as conn:
        task = kb.get_task(conn, result["task_id"])
    if admit:
        assert task.placement_policy is None
    else:
        assert task.placement_policy == "worker-first-v1"
        assert "hub:write" in task.requires


@pytest.mark.parametrize("variable", ["HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK"])
def test_tool_force_hub_refused_inside_worker(on_gateway, monkeypatch, variable):
    judge = on_gateway("hub")
    monkeypatch.setenv(variable, "t_parent")
    result = json.loads(kt._handle_create({"title": MARKED, "requires": [], "force_hub": True}))
    assert not result.get("ok")
    assert "force_hub" in json.dumps(result)
    assert judge.calls == []
    with kbc.connect() as conn:
        assert kb.list_tasks(conn) == []


def test_tool_force_hub_outside_worker_recorded(on_gateway):
    judge = on_gateway("hub")
    result = json.loads(kt._handle_create({"title": MARKED, "requires": [], "force_hub": True}))
    assert result.get("ok"), result
    assert judge.calls == []
    (event,) = _events(result["task_id"])
    assert event["forced"] is True and event["admit"] is True
    with kbc.connect() as conn:
        assert kb.get_task(conn, result["task_id"]).placement_policy is None


@pytest.mark.parametrize("admit", [True, False])
def test_tool_runs_on_machine_hub_keeps_explicit_intent(synced, on_gateway, monkeypatch, admit):
    from hermes_cli import kanban_sync_runstate as runstate
    from hermes_cli import kanban_worker_node as wn
    judge = on_gateway("hub")
    judge.admit_next = admit
    monkeypatch.setattr(runstate, "explicit_runs_on", lambda conn, value, courier=False: value)
    monkeypatch.setattr(runstate, "stranded_create", lambda *a, **kw: "")
    monkeypatch.setattr(wn, "check_machine_pins", lambda *a, **kw: None)
    result = json.loads(kt._handle_create({"title": "Rotate the Board admin key", "requires": [],
                                           "runs_on": ["machine:hub"]}))
    assert len(judge.calls) == 1, result
    with kbc.connect() as conn:
        tasks = kb.list_tasks(conn)
        if admit:
            assert result.get("ok"), result
            row = conn.execute("SELECT runs_on FROM tasks WHERE id=?", (result["task_id"],)).fetchone()
            assert json.loads(row[0]) == ["machine:hub"]
        else:
            assert not result.get("ok") and tasks == []
            assert result.get("hub_admission", {}).get("admit") is False


@pytest.mark.parametrize("runs_on", ["machine:hub", [" machine:hub"], "machine:hub,linux"])
def test_tool_runs_on_shapes_naming_hub_are_judged(synced, on_gateway, monkeypatch, runs_on):
    """Review t_01a1ceabf1ca19c8: string and padded shapes made a machine:hub card with zero calls.

    The real tool normalizer runs (explicit_runs_on is not patched)."""
    from hermes_cli import kanban_sync_runstate as runstate
    from hermes_cli import kanban_worker_node as wn
    judge = on_gateway("hub")
    judge.admit_next = False
    monkeypatch.setattr(runstate, "stranded_create", lambda *a, **kw: "")
    monkeypatch.setattr(wn, "check_machine_pins", lambda *a, **kw: None)
    result = json.loads(kt._handle_create({"title": "Rotate the Board admin key", "requires": [],
                                           "runs_on": runs_on}))
    assert len(judge.calls) == 1, result
    assert not result.get("ok"), result
    with kbc.connect() as conn:
        assert kb.list_tasks(conn) == []


@pytest.mark.parametrize("admit", [True, False])
def test_tool_courier_create_landing_on_hub_is_judged(synced, on_gateway, admit):
    """A routed create (macbook→hub auto route) is judged on the hub before its txn; a deny
    re-enrolls it in the pool with requires hub:write (spec owner 11-12)."""
    judge = on_gateway("hub")
    judge.admit_next = admit
    intent = {"request_id": f"request-hub-{admit}",
              "owner": {"gateway": "hub", "profile": "default", "board": "default"},
              "origin": {"gateway": "macbook", "profile": "default", "session": "s_mac"}}
    result = json.loads(kt._handle_create({"title": MARKED, "requires": [], "assignee": "default"},
                                          origin_intent=intent))
    assert len(judge.calls) == 1, result
    assert result.get("task_id"), result
    with kbc.connect() as conn:
        task = kb.get_task(conn, result["task_id"])
    assert task is not None
    if admit:
        assert task.placement_policy is None
    else:
        assert task.placement_policy == "worker-first-v1" and "hub:write" in (task.requires or [])
    assert [e["admit"] for e in _events(result["task_id"])] == [admit]


@pytest.mark.parametrize("first", [False, True])
def test_tool_courier_replay_reuses_the_first_landing(synced, on_gateway, first):
    """Review t_01a1ced25af29218: a re-applied request whose verdict flipped (an unavailable deny is
    not cached) rebuilt a different envelope and raised origin_creation_conflict."""
    judge = on_gateway("hub")
    judge.admit_next = first
    intent = {"request_id": f"request-replay-{first}",
              "owner": {"gateway": "hub", "profile": "default", "board": "default"},
              "origin": {"gateway": "macbook", "profile": "default", "session": "s_mac"}}
    args = {"title": MARKED, "requires": [], "assignee": "default"}
    one = json.loads(kt._handle_create(dict(args), origin_intent=intent))
    judge.admit_next = not first
    two = json.loads(kt._handle_create(dict(args), origin_intent=intent))
    assert one.get("task_id") and two.get("task_id") == one["task_id"], two
    assert len(judge.calls) == 1  # the replay makes no model call
    with kbc.connect() as conn:
        assert len(kb.list_tasks(conn)) == 1
        task = kb.get_task(conn, one["task_id"])
    assert (task.placement_policy is None) is first


@pytest.mark.parametrize("admit", [True, False])
def test_tool_placement_hub_from_another_gateway(on_gateway, monkeypatch, admit):
    import functools
    judge = on_gateway("mac")
    judge.admit_next = admit
    sent = []

    def fake_remote(view, **kw):
        sent.append((view, kw["target"]))
        return {"ok": True, "request_id": "req-1", "queued_only": True}
    monkeypatch.setattr(placement, "create_remote", fake_remote)
    # The hub is live and carries every option, so routing (not liveness) decides.
    monkeypatch.setattr(placement, "decide", functools.partial(placement.decide, live=lambda _: None, carries=True))
    result = json.loads(kt._handle_create({"title": "Rotate the Board admin key", "requires": [],
                                           "placement": "hub"}))
    assert len(judge.calls) == 1, (result, sent)
    with kbc.connect() as conn:
        assert kb.list_tasks(conn) == []  # never a local copy
    if admit:
        assert [target for _, target in sent] == ["hub"]
    else:
        assert sent == [] and not result.get("ok")
        assert result["hub_admission"]["admit"] is False


def test_tool_non_pool_assignee_refusal_names_the_pool_route(on_gateway):
    """t_01a1d09e42e2c4fd (sweep slug profile-not-in-pool, evidence t_01a1cde9714608c4): a hub chat filed
    Mac-only tailnet_exec work as assignee mac-ops, a profile no pool worker runs, so the card sat in the
    hub's own queue. Admission now refuses it; the refusal must name the assignee as the pin and the pool
    route (a pool profile plus runs_on capability:tailnet-exec), not just "assignee not allowlisted"."""
    judge = on_gateway("hub")
    judge.admit_next = False
    result = json.loads(kt._handle_create({
        "title": "Mini disk: durable headroom so the governor disk hold releases", "assignee": "mac-ops",
        "requires": [], "body": "Do, via tailnet_exec host=mini: audit the CI runner work dir and free space."}))
    assert not result.get("ok") and result.get("created") is False, result
    assert len(judge.calls) == 1
    text = result["error"]
    assert "'mac-ops'" in text and "pool profiles: default" in text, text
    assert "assignee='default'" in text and "capability:tailnet-exec" in text, text
    with kbc.connect() as conn:
        assert kb.list_tasks(conn) == []
