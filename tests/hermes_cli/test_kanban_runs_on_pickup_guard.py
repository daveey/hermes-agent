"""An explicit ``runs_on=[machine:<x>]`` is refused unless x advertises pickup in the Board registry. [daveey]

10-28: three hub cards created with ``runs_on=["machine:macbook"]`` sat ``ready`` until archived: only the MacBook
may lease them, and it never copies hub cards (``/v2/machines`` ``pickup: null``). Every write of ``runs_on`` is
covered: create (agent tool and ``hermes kanban create``; placement-derived
``runs_on`` keeps its own rule) and ``kanban move`` (queued card and the agent
tool from a running card). ``kanban edit`` / the dashboard cannot write ``runs_on``. Auto-placement is unchanged.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_move as km
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import HUB, MACHINES, WORKER, Gateway

# kanban_move_pool: a machine:<x> target needs a host_reason naming state on x (t_01a1d95c41620e6c).
HR = "the card's worktree is on that host"


@pytest.fixture
def svc():
    return FakeService(lease_runs=True, machines=MACHINES)


@pytest.fixture
def gws(tmp_path, monkeypatch, svc):
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    sync.consume_dispatcher_wake()
    hub = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    worker = Gateway(tmp_path, WORKER, svc, monkeypatch, pickup="true")
    hub.enter()
    yield hub, worker
    for g in (hub, worker):
        g.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _registry(macbook_pickup):
    """The live 10-29 registry shape: every entry carries ``pickup`` (null unless the machine published one)."""
    mac = {**MACHINES["macbook"], "pickup": macbook_pickup}
    wn.set_registry_fetch(lambda: {"me": HUB, "machines": [MACHINES[HUB], MACHINES[WORKER], mac]})


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def _tool(handler, args):
    from tools import kanban_tools
    return json.loads(getattr(kanban_tools, handler)(args))


# --- create ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("macbook_pickup", [None, False, "off"])
def test_tool_create_pinned_to_a_machine_without_pickup_is_refused(gws, macbook_pickup):
    hub, _ = gws
    _registry(macbook_pickup)
    before = _count(hub.conn)
    out = _tool("_handle_create", {"title": "mac readback", "assignee": "default", "runs_on": ["machine:macbook"]})
    assert out.get("code") == "placement_unavailable", out
    assert 'placement="macbook"' in out["error"] and out.get("created") is False, out
    assert _count(hub.conn) == before


def test_tool_create_pinned_to_an_unknown_machine_is_refused(gws):
    _hub, _ = gws
    _registry(None)
    out = _tool("_handle_create", {"title": "x", "assignee": "default", "runs_on": ["machine:nosuchbox"]})
    assert out.get("code") == "placement_unavailable" and 'placement="nosuchbox"' in out["error"], out


def test_a_missing_pickup_field_counts_as_no_pickup(gws):
    _hub, _ = gws
    mac = {k: v for k, v in MACHINES["macbook"].items() if k != "pickup"}
    wn.set_registry_fetch(lambda: {"me": HUB, "machines": [MACHINES[HUB], MACHINES[WORKER], mac]})
    out = _tool("_handle_create", {"title": "x", "assignee": "default", "runs_on": ["machine:macbook"]})
    assert out.get("code") == "placement_unavailable" and "pickup=None" in out["error"], out


def test_an_unreachable_registry_refuses_a_machine_pin(gws):
    _hub, _ = gws

    def down():
        raise OSError("board down")
    wn.set_registry_fetch(down)
    out = _tool("_handle_create", {"title": "x", "assignee": "default", "runs_on": ["machine:macbook"]})
    assert out.get("code") == "placement_unavailable" and "unreachable" in out["error"], out


@pytest.mark.parametrize("macbook_pickup", ["explicit", "all"])
def test_tool_create_pinned_to_a_machine_that_advertises_pickup_is_accepted(gws, macbook_pickup):
    hub, _ = gws
    _registry(macbook_pickup)
    out = _tool("_handle_create", {"title": "mac readback", "assignee": "default", "runs_on": ["machine:macbook"]})
    assert "error" not in out, out
    row = hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (out["task_id"],)).fetchone()
    assert json.loads(row[0]) == ["machine:macbook"]


def test_own_machine_and_plain_labels_are_not_checked(gws):
    _hub, _ = gws
    _registry(None)
    for runs_on in (["machine:hub"], ["linux"]):
        out = _tool("_handle_create", {"title": f"on {runs_on}", "assignee": "default", "runs_on": runs_on})
        assert out.get("runs_on") == runs_on, out


def test_cli_create_gets_the_same_two_results(gws):
    hub, _ = gws
    from hermes_cli import kanban as kcli
    _registry(None)
    before = _count(hub.conn)
    out = kcli.run_slash('create "mac" --assignee default --runs-on machine:macbook --json')
    assert "placement_unavailable" in out and 'placement="macbook"' in out, out
    assert _count(hub.conn) == before
    _registry("explicit")
    tid = json.loads(kcli.run_slash('create "mac" --assignee default --runs-on machine:macbook --json'))["id"]
    assert json.loads(hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]) == [
        "machine:macbook"]


# --- move -----------------------------------------------------------------------------------------------------

def test_move_queued_to_a_machine_without_pickup_is_refused_and_accepted_with_it(gws):
    hub, _ = gws
    _registry(None)
    tid = kb.create_task(hub.conn, title="queued", assignee="default", runs_on=["machine:hub"])
    res = km.move_queued(hub.conn, tid, runs_on=["machine:macbook"], host_reason=HR, reason="needs the mac")
    assert res["ok"] is False and res["reason_code"] == "placement_unavailable", res
    assert 'placement="macbook"' in res["error"]
    assert json.loads(hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]) == [
        "machine:hub"]
    _registry("all")
    res = km.move_queued(hub.conn, tid, runs_on=["machine:macbook"], host_reason=HR, reason="needs the mac")
    assert res["ok"] and res["matches"] == ["macbook"], res


def test_cli_move_gets_the_same_two_results(gws, capsys):
    hub, _ = gws
    from hermes_cli import kanban as kcli
    _registry(None)
    tid = kb.create_task(hub.conn, title="queued", assignee="default", runs_on=["machine:hub"])
    out = kcli.run_slash(f'move {tid} --runs-on machine:macbook --host-reason "Mac-only work" --reason "needs the mac" --json')
    res = json.loads(out)
    assert res["ok"] is False and res["reason_code"] == "placement_unavailable", res
    _registry("explicit")
    res = json.loads(kcli.run_slash(f'move {tid} --runs-on machine:macbook --host-reason "Mac-only work" --reason "needs the mac" --json'))
    assert res["ok"] and res["runs_on"] == ["machine:macbook"], res


def test_tool_move_of_a_running_card_to_a_machine_without_pickup_is_refused(gws, monkeypatch):
    hub, _ = gws
    _registry(None)
    tid = kb.create_task(hub.conn, title="work", assignee="default", runs_on=["machine:hub"])
    hub.round()
    hub.tick()
    run_id = hub.conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    out = _tool("_handle_move", {"task_id": tid, "runs_on": ["machine:macbook"], "reason": "needs the mac",
                                 "checkpoint": "step 1 done"})
    assert "does not pick up" in out["error"] and 'placement="macbook"' in out["error"], out
    task = kb.get_task(hub.conn, tid)
    assert task is not None and task.status == "running"


# --- auto-placement unchanged -----------------------------------------------------------------------------------

def test_placement_pin_still_becomes_runs_on_only_with_pickup(gws, monkeypatch):
    """``placement=<machine>`` keeps its own rule (placement_runs_on): pickup -> runs_on, none -> gateway route."""
    hub, _ = gws
    monkeypatch.setattr("hermes_cli.kanban_capability_registry.self_gateway", lambda: HUB)
    _registry(None)
    assert wn.placement_runs_on(hub.conn, WORKER) == ["machine:worker-1"]
    assert wn.placement_runs_on(hub.conn, "macbook") is None
    out = _tool("_handle_create", {"title": "on the worker", "assignee": "default", "placement": WORKER})
    assert out["runs_on"] == ["machine:worker-1"], out
