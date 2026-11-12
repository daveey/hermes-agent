"""One worker queue: portable cards get ``runs_on: [pool:worker]``, not a machine pin. [daveey]

11-12 05:40Z: ten ready cards were pinned ``machine:worker-2`` while worker-2 was drain-held, and worker-1 and
worker-4 had free slots; the oldest waited 261 min. Every route that put portable work on a worker wrote one machine
name (the Board reservation, ``heavy_route``, agent moves). Here, one test per entry path: the agent tool create,
the CLI create, the heavy route, ``hermes kanban move --pool``, a machine move without ``host_reason`` (refused), and a
park of a pushed workspace (stays on the pool; only unpushed state pins it, with the reason in the event).
"""
from __future__ import annotations

import json
import subprocess

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_move as km
from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import HUB, MACHINES, WORKER, Gateway

POOL = "pool:worker"
CFG = {"worker_queue": {HUB: POOL}}
W1 = {**MACHINES[WORKER], "labels": ["linux", POOL]}
W2 = {**MACHINES[WORKER], "name": "worker-2", "labels": ["linux", POOL]}


def _machines(*, held=()):
    return {m["name"]: {**m, "hold": m["name"] in held} for m in (MACHINES[HUB], W1, W2, MACHINES["macbook"])}


def _registry(*, held=()):
    machines = list(_machines(held=held).values())
    wn.set_registry_fetch(lambda: {"me": HUB, "machines": machines})


@pytest.fixture
def svc():
    return FakeService(lease_runs=True, machines=MACHINES)


@pytest.fixture
def hub(tmp_path, monkeypatch, svc):
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    monkeypatch.setattr("hermes_cli.kanban_capability_registry.self_gateway", lambda: HUB)
    monkeypatch.setattr(kp, "config", lambda: CFG)
    sync.consume_dispatcher_wake()
    gw = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    gw.enter()
    yield gw
    gw.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _runs_on(conn, tid):
    raw = conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    return json.loads(raw) if raw else None


def _moved(conn, tid):
    return [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'moved' ORDER BY id", (tid,))]


# --- create -----------------------------------------------------------------------------------------------------

def test_tool_create_queues_a_portable_card_for_the_pool_while_one_worker_is_held(hub):
    from tools import kanban_tools
    _registry(held=("worker-1",))
    out = json.loads(kanban_tools._handle_create({"title": "summarise the failing CI jobs", "assignee": "default",
                                                  "requires": []}))
    assert out.get("ok") and out["runs_on"] == [POOL], out
    assert _runs_on(hub.conn, out["task_id"]) == [POOL]
    assert "worker_queue" in out["placement"]["why"], out
    # No machine is named, so the held worker cannot strand it: unheld worker-2 is eligible right now.
    card = dict(hub.conn.execute("SELECT * FROM tasks WHERE id = ?", (out["task_id"],)).fetchone())
    assert wn.eligible(card, "worker-2", _machines(held=("worker-1",))) is None
    assert wn.eligible(card, "worker-1", _machines(held=("worker-1",))) == "held"


def test_tool_create_keeps_host_work_and_an_empty_pool_off_the_queue(hub):
    from tools import kanban_tools
    _registry()
    out = json.loads(kanban_tools._handle_create({"title": "restart the gateway service on the hub",
                                                  "assignee": "default", "requires": []}))
    assert out.get("ok") and _runs_on(hub.conn, out["task_id"]) is None, out
    _registry(held=("worker-1", "worker-2"))
    out = json.loads(kanban_tools._handle_create({"title": "summarise CI", "assignee": "default", "requires": []}))
    assert out.get("ok") and _runs_on(hub.conn, out["task_id"]) is None, out


def test_cli_create_queues_a_portable_card_for_the_pool(hub):
    from hermes_cli import kanban as kcli
    _registry(held=("worker-1",))
    out = kcli.run_slash('create "summarise the failing CI jobs" --assignee default --json')
    tid = json.loads(out)["id"]
    assert _runs_on(hub.conn, tid) == [POOL], out
    out = kcli.run_slash('create "pinned" --assignee default --runs-on machine:worker-2 --json')
    assert _runs_on(hub.conn, json.loads(out)["id"]) == ["machine:worker-2"], out  # an explicit pin wins


def test_heavy_route_accepts_a_pool_label_and_checks_it_is_live(hub):
    cfg = {"heavy_route": {HUB: POOL}}
    _registry(held=("worker-1",))
    d = kp.decide({"title": "Run the regression tests for the parser"}, cfg=cfg, self_gw=HUB)
    assert d["where"] == "local" and d["runs_on"] == [POOL], d
    wn.set_registry_fetch(None)
    _registry(held=("worker-1", "worker-2"))
    d = kp.decide({"title": "Run the regression tests for the parser"}, cfg=cfg, self_gw=HUB)
    assert "runs_on" not in d, d  # no unheld machine carries the label: the card stays on the hub


# --- move -------------------------------------------------------------------------------------------------------

def test_cli_move_pool_requeues_a_worker_pin_for_any_worker(hub):
    from hermes_cli import kanban as kcli
    _registry()
    tid = kb.create_task(hub.conn, title="stuck on worker-1", assignee="default", runs_on=["machine:worker-1"])
    out = kcli.run_slash(f'move {tid} --pool --reason "worker-1 is held; any worker can run it" --json')
    res = json.loads(out)
    assert res["ok"] and res["runs_on"] == [POOL] and res["matches"] == ["worker-1", "worker-2"], res
    assert _runs_on(hub.conn, tid) == [POOL]


def test_machine_move_to_one_pool_worker_needs_a_host_reason(hub):
    from hermes_cli import kanban as kcli
    _registry()
    tid = kb.create_task(hub.conn, title="work", assignee="default", runs_on=[POOL])
    res = km.move_queued(hub.conn, tid, runs_on=["machine:worker-2"], reason="worker-2 has free slots")
    assert not res["ok"] and res["reason_code"] == "host_reason_required" and "--pool" in res["error"], res
    out = kcli.run_slash(f'move {tid} --runs-on machine:worker-2 --reason "free slots" --json')
    assert json.loads(out)["reason_code"] == "host_reason_required", out
    assert _runs_on(hub.conn, tid) == [POOL]  # nothing was written
    res = km.move_queued(hub.conn, tid, runs_on=["machine:worker-2"], reason="repro the worker-2 disk alert",
                         host_reason="the card fixes worker-2's own disk")
    assert res["ok"], res
    assert _moved(hub.conn, tid)[-1]["host_reason"] == "the card fixes worker-2's own disk"


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


def test_park_of_a_pushed_workspace_keeps_the_pool_and_only_unpushed_state_pins(hub, tmp_path):
    _registry()
    remote, ws = tmp_path / "remote.git", tmp_path / "ws"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    _git(tmp_path, "clone", "-q", str(remote), str(ws / "repo"))
    (ws / "repo" / "a.txt").write_text("a\n")
    _git(ws / "repo", "add", "a.txt")
    _git(ws / "repo", "commit", "-qm", "a")
    _git(ws / "repo", "push", "-q", "origin", "HEAD:refs/heads/wt/x")
    tid = kb.create_task(hub.conn, title="parked pool card", assignee="default", runs_on=[POOL])
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(ws), tid))
    # The parked card is on worker-2 (its run ended there): pinning it back to worker-2 is refused while clean+pushed.
    move = dict(runs_on=["machine:worker-2"], reason="resume here", host_reason="my worktree is here",
                me="worker-2", machines=_machines())
    res = km.move_queued(hub.conn, tid, **move)
    assert not res["ok"] and res["reason_code"] == "pool_keeps_pushed", res
    assert _runs_on(hub.conn, tid) == [POOL]
    (ws / "repo" / "b.txt").write_text("b\n")
    _git(ws / "repo", "add", "b.txt")
    _git(ws / "repo", "commit", "-qm", "b")  # an unpushed commit on worker-2
    res = km.move_queued(hub.conn, tid, **move)
    assert res["ok"], res
    ev = _moved(hub.conn, tid)[-1]
    assert ev["unpushed_state"].startswith("unpushed commits in ") and ev["host_reason"] == "my worktree is here", ev
