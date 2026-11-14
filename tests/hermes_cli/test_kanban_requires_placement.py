"""``requires`` is checked where a card runs, not only where it was created. [daveey]

t_01a184ac93d31979 was created on the MacBook with ``requires: [terminal, file, git, kanban]`` and
``runs_on: [machine:worker-1]``. The create check read the MacBook's ``default`` profile (has ``kanban``),
worker-1 claimed and ran it twice without a check, and the reviewer's ``kanban_request_changes`` then failed
``assignee_capability_mismatch`` against worker-1's ``default`` (no ``kanban``), which became a human ask.

(a) create / move refuse when every machine ``runs_on`` can land on is known to lack a needed tool, naming it;
(b) a worker node never picks up a card its own profile cannot satisfy;
(c) ``request_changes`` always records the verdict for the implementer that ran the card (a warning at most).
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_move as km
from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_worker_node as wn
from hermes_cli.kanban_capabilities import CapabilityMismatchError
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import BOARD, HUB, MACHINES, WORKER, Gateway

# kanban_move_pool: a machine:<x> target needs a host_reason naming state on x (t_01a1d95c41620e6c).
HR = "the card's worktree is on that host"

WITH_KANBAN = "platform_toolsets:\n  cli: [terminal, file, kanban]\n"
NEEDS = ["terminal", "file", "git", "kanban"]
NO_SHELL = "platform_toolsets:\n  cli: [file]\n"


@pytest.fixture
def svc():
    return FakeService(lease_runs=True, machines=MACHINES)


@pytest.fixture
def gws(tmp_path, monkeypatch, svc):
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    sync.consume_dispatcher_wake()
    hub = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    worker = Gateway(tmp_path, WORKER, svc, monkeypatch, pickup="true")
    # The hub's `default` has board tools (like the MacBook's); worker-1's does not (kanban is default-off).
    with open(hub.home / "config.yaml", "a") as fh:
        fh.write(WITH_KANBAN)
    hub.enter()
    yield hub, worker
    for g in (hub, worker):
        g.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _publish(monkeypatch, tools_by_gateway):
    """What each gateway publishes as ``profile-tools:`` through hermes-hub (None = publishes nothing)."""
    monkeypatch.setattr(kp, "target_info", lambda name, now, remote=None: {
        "profiles": None, "tools": tools_by_gateway.get(name)})


# --- (a) create ------------------------------------------------------------------------------------------------

def test_create_for_a_machine_known_to_lack_a_tool_is_refused_naming_it(gws, monkeypatch):
    hub, _ = gws
    _publish(monkeypatch, {WORKER: {"default": {"terminal", "file", "git"}}})
    before = hub.conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    with pytest.raises(CapabilityMismatchError) as caught:
        kb.create_task(hub.conn, title="heavy", assignee="default", requires=NEEDS,
                       runs_on=["machine:worker-1"])
    msg = str(caught.value)
    assert "worker-1" in msg and "kanban" in msg, msg
    assert caught.value.missing == ["kanban"]
    assert hub.conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == before


def test_create_passes_when_one_matching_machine_is_capable_or_unknown(gws, monkeypatch):
    hub, _ = gws
    # runs_on=[linux] matches the hub too, whose own default has kanban.
    _publish(monkeypatch, {WORKER: {"default": {"terminal", "file", "git"}}})
    assert kb.create_task(hub.conn, title="a", assignee="default", requires=NEEDS, runs_on=["linux"])
    # worker-1 publishes nothing: unknown never refuses (the claim gate decides).
    _publish(monkeypatch, {})
    assert kb.create_task(hub.conn, title="b", assignee="default", requires=NEEDS, runs_on=["machine:worker-1"])
    # worker-1 publishes kanban for default.
    _publish(monkeypatch, {WORKER: {"default": {"terminal", "file", "kanban"}}})
    assert kb.create_task(hub.conn, title="c", assignee="default", requires=NEEDS, runs_on=["machine:worker-1"])
    # a card that needs no board tools is fine on the kanban-less worker.
    _publish(monkeypatch, {WORKER: {"default": {"terminal", "file", "git"}}})
    assert kb.create_task(hub.conn, title="d", assignee="default", requires=["terminal", "file", "git"],
                          runs_on=["machine:worker-1"])


def test_tool_create_refusal_names_the_machine(gws, monkeypatch):
    _hub, _ = gws
    _publish(monkeypatch, {WORKER: {"default": {"terminal", "file", "git"}}})
    from tools import kanban_tools as kt
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    out = json.loads(kt._handle_create({"title": "heavy", "assignee": "default", "requires": NEEDS,
                                        "runs_on": ["machine:worker-1"], "placement": "local"}))
    assert out.get("error") and "worker-1" in out["error"] and "kanban" in out["error"], out


# --- (a) move --------------------------------------------------------------------------------------------------

def _running_hub_card(hub, **kw):
    hub.enter()
    tid = kb.create_task(hub.conn, title="work", assignee="default", runs_on=["machine:hub"], **kw)
    hub.round()
    hub.tick()
    assert kb.get_task(hub.conn, tid).status == "running"
    hub.round()
    return tid, hub.conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]


def test_move_to_a_machine_known_to_lack_a_tool_is_refused_naming_it(gws, monkeypatch):
    hub, _ = gws
    _publish(monkeypatch, {})
    tid, run_id = _running_hub_card(hub, requires=NEEDS)
    _publish(monkeypatch, {WORKER: {"default": {"terminal", "file", "git"}}})
    res = km.move_task(hub.conn, tid, runs_on=["machine:worker-1"], host_reason=HR, reason="heavy", checkpoint="x",
                       expected_run_id=run_id)
    assert not res["ok"] and res["reason_code"] == "capability_mismatch", res
    assert "worker-1" in res["error"] and "kanban" in res["error"]
    t = kb.get_task(hub.conn, tid)
    assert t.status == "running" and json.loads(
        hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]) == ["machine:hub"]
    # Control: the same move succeeds once the worker publishes the tool.
    _publish(monkeypatch, {WORKER: {"default": {"terminal", "file", "kanban"}}})
    res = km.move_task(hub.conn, tid, runs_on=["machine:worker-1"], host_reason=HR, reason="heavy", checkpoint="x",
                       expected_run_id=run_id)
    assert res["ok"], res


def test_queued_move_is_refused_too(gws, monkeypatch):
    hub, _ = gws
    _publish(monkeypatch, {})
    tid = kb.create_task(hub.conn, title="q", assignee="default", requires=NEEDS, runs_on=["machine:hub"],
                         triage=True)
    _publish(monkeypatch, {WORKER: {"default": {"terminal"}}})
    res = km.move_queued(hub.conn, tid, runs_on=["machine:worker-1"], host_reason=HR, reason="heavy")
    assert not res["ok"] and res["reason_code"] == "capability_mismatch" and "worker-1" in res["error"], res


# --- (b) claim -------------------------------------------------------------------------------------------------

def test_worker_does_not_claim_a_card_its_profile_cannot_satisfy(gws, svc, monkeypatch):
    hub, worker = gws
    _publish(monkeypatch, {})  # worker-1 publishes no profile tools (live today): create cannot know
    # worker-1's `default` has no shell.
    with open(worker.home / "config.yaml", "a") as fh:
        fh.write(NO_SHELL)
    tid = kb.create_task(hub.conn, title="heavy", assignee="default", requires=["terminal", "file"],
                         runs_on=["machine:worker-1"])
    hub.round()
    worker.round()
    worker.tick()
    assert kb.get_task(worker.conn, tid) is None, "worker-1 copied a card its default profile cannot satisfy"
    assert tid not in svc.board(BOARD)["leases"]
    hub.round()
    assert kb.get_task(hub.conn, tid).status == "ready"
    # Control: once worker-1's default has a shell, it takes the card.
    (worker.home / "config.yaml").write_text(
        (worker.home / "config.yaml").read_text().replace(NO_SHELL, WITH_KANBAN))
    worker.round()
    worker.tick()
    assert kb.get_task(worker.conn, tid) is not None
    assert svc.board(BOARD)["leases"][tid]["holder"] == WORKER


def test_worker_claims_a_requires_kanban_card_without_kanban_in_its_chat_toolsets(gws, svc, monkeypatch):
    """The 10-28 card: worker-1's `default` has no `kanban` in platform_toolsets.cli, but every dispatched
    worker gets the kanban toolset (model_tools), so it can run AND hand back a requires-kanban card."""
    hub, worker = gws
    _publish(monkeypatch, {})
    tid = kb.create_task(hub.conn, title="heavy", assignee="default", requires=NEEDS, runs_on=["machine:worker-1"])
    hub.round()
    worker.round()
    worker.tick()
    assert svc.board(BOARD)["leases"][tid]["holder"] == WORKER


def test_worker_defers_a_requires_kanban_card_when_kanban_is_disabled(gws, svc, monkeypatch):
    hub, worker = gws
    _publish(monkeypatch, {})
    with open(worker.home / "config.yaml", "a") as fh:
        fh.write("agent:\n  disabled_toolsets: [kanban]\n")
    tid = kb.create_task(hub.conn, title="heavy", assignee="default", requires=NEEDS, runs_on=["machine:worker-1"])
    hub.round()
    worker.round()
    worker.tick()
    assert kb.get_task(worker.conn, tid) is None and tid not in svc.board(BOARD)["leases"]


def test_claim_refusal_reads_replica_row_fields():
    from hermes_cli import kanban_runs_on_capabilities as roc
    assert roc.claim_refusal({"id": "t", "assignee": None, "requires": json.dumps(NEEDS)}) is None
    assert roc.claim_refusal({"id": "t", "assignee": "default", "requires": json.dumps(["bogus"])})


# --- (c) request_changes ---------------------------------------------------------------------------------------

LOCAL = "platform_toolsets: {cli: [terminal, file, kanban]}\nterminal: {backend: local}\n"


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    from pathlib import Path
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    def configure(name, policy=LOCAL):
        target = home / "profiles" / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "config.yaml").write_text(policy)

    configure("implementer")
    configure("reviewer")
    kb.init_db()
    return configure


def _in_review_run(conn):
    tid = kb.create_task(conn, title="ran on a worker node", assignee="implementer", requires=NEEDS)
    assert kb.claim_task(conn, tid) is not None
    assert kb.request_review(conn, tid, reviewer="reviewer", expected_run_id=kb.get_task(conn, tid).current_run_id)
    assert kb.claim_review_task(conn, tid) is not None
    return tid, kb.get_task(conn, tid).current_run_id


def test_request_changes_records_the_verdict_when_the_profile_here_lacks_a_requirement(profiles):
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        tid, run_id = _in_review_run(conn)
        # This machine's `implementer` lost its shell after it ran the card (policy drift, or another
        # machine's profile of the same name).
        profiles("implementer", "platform_toolsets: {cli: [file]}\n")
        ok, detail = kb.request_changes(conn, tid, reason="Fix the flaky assertion", expected_run_id=run_id)
        assert ok and detail == "implementer", detail
        task = kb.get_task(conn, tid)
        assert task.assignee == "implementer" and task.status == "ready" and task.current_run_id is None
        (payload,) = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'changes_requested'", (tid,)).fetchone()
        ev = json.loads(payload)
        assert ev["reason"] == "Fix the flaky assertion" and ev["implementer"] == "implementer"
        assert ev["capability_warning"]["missing"] == ["terminal", "git"]
        outcome = conn.execute("SELECT outcome FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0]
        assert outcome == "changes_requested"


def test_request_changes_tool_records_the_verdict(profiles, monkeypatch):
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    with kbc.connect() as conn:
        tid, run_id = _in_review_run(conn)
    profiles("implementer", "platform_toolsets: {cli: [file]}\n")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    out = json.loads(kt._handle_request_changes({"reason": "Revise the test"}))
    assert not out.get("error"), out
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.assignee == "implementer" and task.status == "ready"


def test_request_changes_without_a_mismatch_records_no_warning(profiles):
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        tid, run_id = _in_review_run(conn)
        assert kb.request_changes(conn, tid, reason="Revise", expected_run_id=run_id)[0]
        (payload,) = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'changes_requested'", (tid,)).fetchone()
        assert "capability_warning" not in json.loads(payload)


def test_request_changes_for_the_10_28_card_needs_no_warning(profiles):
    """requires=[..., kanban], implementer without `kanban` in its chat toolsets: no mismatch at all."""
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        tid, run_id = _in_review_run(conn)
        profiles("implementer", "platform_toolsets: {cli: [terminal, file]}\nterminal: {backend: local}\n")
        assert kb.request_changes(conn, tid, reason="Revise", expected_run_id=run_id)[0]
        (payload,) = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'changes_requested'", (tid,)).fetchone()
        assert "capability_warning" not in json.loads(payload)
