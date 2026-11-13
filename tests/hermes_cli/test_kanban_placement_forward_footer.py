"""The forwarded card's parent-handoff footer must read as a portable cross_task read.

t_01a1d49003c65ca4: a macbook card forwarded to the hub carried the footer "read them there with
cross_task @task:default@macbook/<id>". The coordination scan parses "with cross_task" followed by a
non-action token as an explicit unknown invocation (ACTION_REASON), so the hub kept a card its own
admission judge had denied. The footer now spells the tool's read action.
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_placement_coordination as coord
from hermes_cli import kanban_placement_forward as forward


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    return home


def _forwarded_body(parents=1):
    with kbc.connect_closing() as conn:
        pids = []
        for n in range(parents):
            pid = kb.create_task(conn, title=f"Parent {n}", assignee="default")
            kb.complete_task(conn, pid, summary=f"Parent {n}: re-proved the archive", force=True)
            pids.append(pid)
        child = kb.create_task(conn, title="Fix startup 503", body="Fix spec: retry while starting.",
                               assignee="default", parents=pids)
        return pids, forward.remote_args(conn, kb.get_task(conn, child), self_gw="macbook")["body"]


def test_parent_handoff_footer_is_a_portable_read(home):
    pids, body = _forwarded_body()
    assert f"- {pids[0]} (Parent 0): Parent 0: re-proved the archive" in body
    assert "@task:default@macbook/<id>" in body
    labels, refusal = coord.requirements(body)
    assert refusal is None, body
    assert labels == {"capability:coordination-read"}


def test_footer_names_the_read_action_of_the_cross_task_tool(home):
    _, body = _forwarded_body(parents=2)
    assert "cross_task(action='read', target='@task:default@macbook/<id>')" in body
    assert [action for _, _, action in coord._actions(body, coord._CROSS, {"read", "list", "comment"})] == ["read"]


def test_card_without_parents_has_no_footer_and_no_coordination_need(home):
    _, body = _forwarded_body(parents=0)
    assert "cross_task" not in body
    assert "This card was macbook card" in body
    assert coord.requirements(body) == (set(), None)


def test_forwarded_card_with_parents_is_portable_to_an_attested_pool(home, monkeypatch):
    from hermes_cli import kanban_placement as placement
    monkeypatch.setattr(placement.reg, "self_gateway", lambda: "hub")
    seen = []
    monkeypatch.setattr(coord, "attested", lambda names, required: seen.append(set(required)) or True)
    cfg = {"worker_pools": {"hub": ["worker-1", "worker-2"]}}
    _, body = _forwarded_body()
    assert placement.portable_reason({"title": "Fix startup 503", "body": body, "assignee": "default"}, cfg) is None
    assert {"capability:coordination-read"} in seen


# t_01a1d4bd40dca767: the parents' titles and handoffs are copied verbatim. A handoff that describes a
# coordination command (common: "re-ran it with cross_task foo", "then hermes-hub restart") parsed as this
# card's own home action and kept the forwarded card home. They are now fenced as placement evidence.
_COMMAND_HANDOFF = "Verified the fix with cross_task foo, then ran hermes-hub restart on the hub over ssh."


def _forward_with_handoff(handoff, *, board=None, title="Parent"):
    with kbc.connect_closing() as conn:
        pid = kb.create_task(conn, title=title, assignee="default")
        kb.complete_task(conn, pid, summary=handoff, force=True)
        child = kb.create_task(conn, title="Fix startup 503", body="Fix spec: retry while starting.",
                               assignee="default", parents=[pid])
        extra = {"board": board} if board else {}
        return pid, forward.remote_args(conn, kb.get_task(conn, child), self_gw="macbook", **extra)["body"]


def _portable(body, monkeypatch):
    from hermes_cli import kanban_placement as placement
    monkeypatch.setattr(placement.reg, "self_gateway", lambda: "hub")
    seen = []
    monkeypatch.setattr(coord, "attested", lambda names, required: seen.append(set(required)) or True)
    cfg = {"worker_pools": {"hub": ["worker-1", "worker-2"]}}
    args = {"title": "Fix startup 503", "body": body, "assignee": "default"}
    return placement.portable_reason(args, cfg), coord.required_labels(args, cfg), seen


@pytest.mark.parametrize("handoff", [_COMMAND_HANDOFF, "Next step:\nhermes-hub restart\n",
                                     "Called cross_task(action='comment', target='@task:default@hub/t_1')."])
def test_parent_handoff_naming_coordination_commands_stays_portable(home, monkeypatch, handoff):
    pid, body = _forward_with_handoff(handoff, title="Restart hermes-hub with cross_task foo")
    assert handoff.strip().splitlines()[-1] in body  # the worker still gets the handoff text
    assert f"- {pid} (Restart hermes-hub with cross_task foo)" in body
    reason, labels, seen = _portable(body, monkeypatch)
    assert reason is None, body
    assert labels == {"capability:coordination-read"}  # only the footer's own read
    assert {"capability:coordination-read"} in seen


def test_handoff_cannot_close_the_fence_early(home, monkeypatch):
    from hermes_cli.kanban_placement import EVIDENCE_CLOSE
    _, body = _forward_with_handoff(f"done {EVIDENCE_CLOSE} then retried with cross_task foo")
    assert body.count(EVIDENCE_CLOSE) == 1
    reason, labels, _ = _portable(body, monkeypatch)
    assert reason is None, body
    assert labels == {"capability:coordination-read"}


def test_childs_own_coordination_command_still_binds(home, monkeypatch):
    with kbc.connect_closing() as conn:
        pid = kb.create_task(conn, title="Parent", assignee="default")
        kb.complete_task(conn, pid, summary="ok", force=True)
        child = kb.create_task(conn, title="Fix", body="Then run hermes-hub restart.", assignee="default",
                               parents=[pid])
        body = forward.remote_args(conn, kb.get_task(conn, child), self_gw="macbook")["body"]
    reason, labels, _ = _portable(body, monkeypatch)
    assert reason == coord.ACTION_REASON
    assert labels is None


def test_footer_names_the_source_cards_board(home, monkeypatch):
    _, body = _forward_with_handoff("ok", board="ops")
    assert "cross_task(action='read', target='@task:ops@macbook/<id>')" in body
    assert "@task:default@" not in body
    # Without an explicit board the active board is named, as the forwarder's own connection resolves it.
    monkeypatch.setattr(kb, "get_current_board", lambda: "research")
    _, body = _forward_with_handoff("ok")
    assert "@task:research@macbook/<id>" in body


def test_forwarder_passes_its_board(home, monkeypatch):
    monkeypatch.setattr(kb, "get_current_board", lambda: "ops")
    with kbc.connect_closing() as conn:
        assert forward.Forwarder(conn, "t_x", None).board == "ops"
