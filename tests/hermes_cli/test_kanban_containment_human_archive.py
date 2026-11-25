"""A human operator archives a contained card whose authority cannot release it (closure ``human_operator``). [daveey]

Replays t_01a1fd2c65f894e3 (11-22): contained by its own run, authority t_01a1fd3526eb2be9 TODO behind open
parents, so neither ``archive`` (authority not terminal) nor ``resume-contained`` (no live authority run) could
close it although David had decided to archive it as superseded.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import json

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_comment_writer as writer_mod
from hermes_cli import kanban_containment_connections as connections
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_wait_info as kwi
from hermes_cli.kanban_containment_human import HumanArchive, for_caller
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
from hermes_cli.kanban_session_wait_rows import _local_wait_info
from tests.hermes_cli import test_kanban_containment_lifecycle as lifecycle
from tests.hermes_cli import test_kanban_containment_terminal_remote as remote

board = lifecycle.board
kanban_home = remote.kanban_home
synced_conn = remote.synced_conn

REASON = "superseded by t_01a1fd3526eb2be9's replacement (comment 238728)"
HUMAN = ("human", None, "clean-ancestry")
AGENT = ("agent", "20261124_000000_abcdef", "env:HERMES_SESSION_ID")


def _as(monkeypatch, who):
    monkeypatch.setattr(writer_mod, "cli_comment_writer", lambda **_kw: who)


def _cli(ids, reason=None, yes=False) -> int:
    return kanban_cli._cmd_archive(argparse.Namespace(task_ids=list(ids), purge_ids=None, reason=reason, yes=yes))


def _parked(conn, *, live=False, chain=("triage",)):
    """A self-contained relay whose authority is TODO behind a chain of parents (``chain[0]`` the direct parent's
    status, each next one that parent's parent; ``()`` none), or, ``live``, still claimed. Returns the direct
    parent as ``blocker``."""
    relay, _consumer, authority, target, spec = lifecycle.setup(conn)
    episode = lifecycle.contain(conn, relay, target, spec)["episode"]
    blocker = None
    if not live:
        conn.execute("UPDATE tasks SET status='todo', current_run_id=NULL, claim_lock=NULL, claim_expires=NULL, "
                     "worker_pid=NULL WHERE id=?", (authority,))
        child = authority
        for status in chain:
            parent = kb.create_task(conn, title=f"Parent ({status})", assignee="default")
            kb.link_tasks(conn, parent, child)
            conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, parent))
            blocker = blocker or parent
            child = parent
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (authority,))
    return relay, authority, blocker, episode


def _events(conn, task, kind):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (task, kind))]


# --- unsynced board: archive_local ---------------------------------------------------------------------------

def test_human_cli_archive_closes_a_parked_authority_hold_on_an_unsynced_board(board, monkeypatch, capsys):
    relay, authority, _blocker, episode = _parked(board)
    _as(monkeypatch, HUMAN)
    assert _cli([relay], REASON, yes=True) == 0
    assert f"Archived {relay}" in capsys.readouterr().out
    assert kb.get_task(board, relay).status == "archived"
    assert connections.held(board, relay)  # closed, never released: dependents stay unsatisfied
    [closed] = _events(board, relay, "containment_closed")
    assert closed["reason"] == "human_operator" and closed["episode"] == episode
    assert closed["actor_kind"] == "human" and closed["operator_reason"] == REASON
    assert closed["authority_status"] == "todo" and closed["actor_gateway"] == "fixture-home"
    [audit] = _events(board, relay, "archived")
    assert audit["closure"] == "human_operator" and audit["reason"] == REASON and audit["actor_kind"] == "human"
    comments = [r[0] for r in board.execute("SELECT body FROM task_comments WHERE task_id=? AND author='kanban-operator'",
                                            (relay,))]
    assert len(comments) == 1 and REASON in comments[0]
    assert kb.get_task(board, authority).status == "todo"  # the authority is left as it is


def test_agent_with_reason_is_refused_with_the_human_only_message(board, monkeypatch, capsys):
    relay, _authority, _blocker, _ = _parked(board)
    _as(monkeypatch, AGENT)
    assert _cli([relay], REASON) == 1
    err = capsys.readouterr().err
    assert "only a human operator may archive a contained card with --reason" in err and "agent" in err
    assert kb.get_task(board, relay).status == "triage"
    assert not _events(board, relay, "containment_closed")


def test_plain_archive_refusal_names_the_human_operator_exit(board, monkeypatch, capsys):
    relay, _authority, _blocker, _ = _parked(board)
    _as(monkeypatch, AGENT)
    assert _cli([relay]) == 1
    err = capsys.readouterr().err
    assert "authority is neither archived nor done" in err and f"hermes kanban archive {relay} --reason" in err
    assert kb.get_task(board, relay).status == "triage"


def test_live_authority_claim_refuses_the_human_archive(board, monkeypatch, capsys):
    relay, authority, _blocker, _ = _parked(board, live=True)
    _as(monkeypatch, HUMAN)
    assert _cli([relay], REASON, yes=True) == 1
    assert "authority_live" in capsys.readouterr().err
    assert kb.get_task(board, relay).status == "triage"
    assert kb.get_task(board, authority).status == "running"


def test_safety_restricted_contained_card_stays_refused(board, monkeypatch, capsys):
    relay, _authority, _blocker, _ = _parked(board)
    kb._append_event(board, relay, "decision_hold_restricted", {"cause": "safety_refusal"})
    _as(monkeypatch, HUMAN)
    assert _cli([relay], REASON, yes=True) == 1
    assert "safety_restricted" in capsys.readouterr().err
    assert kb.get_task(board, relay).status == "triage"
    assert not _events(board, relay, "containment_closed")


def test_reason_on_a_card_that_is_not_contained_is_refused(board, monkeypatch, capsys):
    plain = kb.create_task(board, title="Ordinary card", assignee="default")
    _as(monkeypatch, HUMAN)
    assert _cli([plain], REASON) == 1
    assert "applies only to a contained card" in capsys.readouterr().err
    assert kb.get_task(board, plain).status != "archived"


@pytest.mark.parametrize("reason", ["", "   ", "x" * 2001])
def test_reason_must_name_why(reason):
    with pytest.raises(ContainmentRefused, match="--reason must name why"):
        for_caller(reason, writer=HUMAN)


# --- closure() validation -------------------------------------------------------------------------------------

def test_human_closure_payload_is_validated_exactly(board):
    from hermes_cli.kanban_containment_terminal import closure
    relay, _authority, _blocker, _ = _parked(board)
    assert kb.archive_task(board, relay, human=HumanArchive(REASON))
    stop = dict(board.execute("SELECT * FROM task_events WHERE task_id=? AND kind='contained'", (relay,)).fetchone())
    event = dict(board.execute("SELECT * FROM task_events WHERE task_id=? AND kind='containment_closed'", (relay,)).fetchone())
    assert closure(event, stop)
    good = json.loads(event["payload"])
    for tamper in ({"actor_kind": "agent"}, {"operator_reason": " "}, {"actor_gateway": ""}, {"extra": 1},
                   {"terminal_status": "done"}):
        assert not closure({**event, "payload": json.dumps({**good, **tamper})}, stop), tamper


# --- synced board: the Board decides ---------------------------------------------------------------------------

class HumanHub(remote.ArchiveHub):
    """ArchiveHub plus the decision-retain capability and the Board's human_operator closure/audit shape."""
    def __init__(self, conn):
        super().__init__(conn)
        self.retain = {"decision_retain": 1, "scope_renewal": 1, "containment_human_archive": 1}
        self.reject = None
        self.patched = False

    def decision_retain_capability(self, board):
        assert not self.conn.in_transaction
        return self.retain

    def ops(self, board, ops):
        if self.reject:
            self.posts.append(copy.deepcopy(ops))
            return {"results": [{"client_op_id": ops[0]["client_op_id"], "status": "rejected", **self.reject}]}
        human = ops[0].get("human_operator")
        try:
            return super().ops(board, ops)
        finally:  # also when the reply is lost: the Board wrote it all the same
            if human and not self.patched:
                self.patched = True
                self._human(human)

    def _human(self, human):
        if human:
            row = self.source.events[self.close_event["event_uid"]]["row"]
            payload = json.loads(row["payload"])
            payload.update(reason="human_operator", authority_status="todo", actor_kind="human",
                           actor_gateway=self.ident.gateway, operator_reason=human["reason"])
            row["payload"] = json.dumps(payload)
            audit = {k: v for k, v in json.loads(self.audit["payload"]).items() if k != "source_status"}
            audit.update(closure="human_operator", actor_kind="human", actor_gateway=self.ident.gateway,
                         reason=human["reason"], source_status="triage")
            self.audit["payload"] = json.dumps(audit)


def _synced(conn, monkeypatch):
    from hermes_cli import kanban_sync_client as client
    from tests.hermes_cli import test_kanban_containment_replica as original
    hub = HumanHub(conn)
    original.apply(conn, hub.source, [original.event(hub.stop, hub.source, 1)])
    conn.execute("UPDATE kanban_sync_state SET mode='primary'")
    monkeypatch.setattr(client, "load_identity", lambda: hub.ident)
    monkeypatch.setattr(client, "Hub", lambda ident: hub)
    monkeypatch.setattr(kanban_cli.kbc, "connect_closing", lambda *a, **k: contextlib.nullcontext(conn))
    for key in ("HERMES_KANBAN_TASK", "HERMES_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    assert connections.held(conn, hub.target)
    return hub


def test_human_cli_archive_on_a_synced_board_sends_human_operator(synced_conn, monkeypatch, capsys):
    hub = _synced(synced_conn, monkeypatch)
    _as(monkeypatch, HUMAN)
    assert _cli([hub.target], REASON, yes=True) == 0, capsys.readouterr().err
    [[op]] = hub.posts
    assert op["op"] == "archive" and op["human_operator"] == {
        "version": 1, "actor_kind": "human", "episode": hub.stop["event_uid"], "reason": REASON}
    assert kb.get_task(synced_conn, hub.target).status == "archived"
    assert connections.held(synced_conn, hub.target)
    [audit] = _events(synced_conn, hub.target, "archived")
    assert audit["closure"] == "human_operator" and audit["reason"] == REASON and list(audit)[-1] == "source_status"
    assert synced_conn.execute("SELECT 1 FROM task_comments WHERE task_id=? AND author='kanban-operator'",
                               (hub.target,)).fetchone()


def test_synced_human_archive_without_board_capability_is_board_unsupported(synced_conn, monkeypatch, capsys):
    hub = _synced(synced_conn, monkeypatch)
    hub.retain.pop("containment_human_archive")
    _as(monkeypatch, HUMAN)
    assert _cli([hub.target], REASON, yes=True) == 1
    assert "board_unsupported" in capsys.readouterr().err
    assert hub.posts == []
    assert kb.get_task(synced_conn, hub.target).status == "triage"


def test_synced_board_refusal_prints_detail_and_the_human_exit(synced_conn, monkeypatch, capsys):
    hub = _synced(synced_conn, monkeypatch)
    hub.reject = {"reason": "containment_hold", "detail": "frozen authority is neither archived nor done without a producer"}
    _as(monkeypatch, AGENT)
    assert _cli([hub.target]) == 1
    err = capsys.readouterr().err
    assert "containment_hold (frozen authority is neither archived nor done without a producer)" in err
    assert f"hermes kanban archive {hub.target} --reason" in err
    assert kb.get_task(synced_conn, hub.target).status == "triage"


def test_synced_safety_restricted_card_is_refused_before_the_board(synced_conn, monkeypatch, capsys):
    hub = _synced(synced_conn, monkeypatch)
    synced_conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, 1)",
                        (hub.target, "decision_hold_restricted", json.dumps({"cause": "safety_refusal"})))
    _as(monkeypatch, HUMAN)
    assert _cli([hub.target], REASON, yes=True) == 1
    assert "safety_restricted" in capsys.readouterr().err
    assert hub.posts == []


# --- a parked authority is reported as stalled; a waiting one is not ----------------------------------------

def _wait(conn, task):
    wait = _local_wait_info(conn, [task]).get(task)
    assert wait is not None
    return wait


@pytest.mark.parametrize("chain", [("running",), ("ready",), ("scheduled",), (), ("todo", "running")])
def test_authority_that_can_still_progress_is_an_ordinary_wait(board, chain):
    relay, authority, blocker, _ = _parked(board, chain=chain)
    wait = _wait(board, relay)
    assert wait["wait_state"] == "waiting" and wait["agent_owned"] is False
    assert authority in wait["waiting_on"] and "human operator" not in wait["waiting_on"]
    if blocker:
        assert blocker in wait["waiting_on"]


@pytest.mark.parametrize("chain", [("triage",), ("blocked",), ("todo", "triage"), ("todo", "todo", "blocked")])
def test_authority_behind_parents_that_cannot_progress_is_stalled_and_named(board, chain):
    relay, authority, blocker, _ = _parked(board, chain=chain)
    wait = _wait(board, relay)
    assert wait["wait_state"] == "stalled" and wait["agent_owned"] is False
    assert authority in wait["waiting_on"] and blocker in wait["waiting_on"]
    assert f"hermes kanban archive {relay} --reason" in wait["waiting_on"] and "<id>" not in wait["waiting_on"]
    assert wait["contained"]["authority"]["waits_on"] == [{"id": blocker, "status": chain[0]}]


@pytest.mark.parametrize("status", ["triage", "blocked"])
def test_authority_that_is_itself_parked_is_stalled(status):
    card = {"id": "t_00000000000000aa", "status": "triage", "contained": {
        "episode": "019a98b4-0000-7000-8000-000000000001", "contained_at": 1_800_000_000,
        "authority": {"id": "t_1123456789abcdef", "status": status, "at": None}, "producer": None}}
    wait = kwi.describe_replica(card, now=1_800_000_100)
    assert wait["wait_state"] == "stalled" and "t_1123456789abcdef" in wait["waiting_on"]
    assert f"itself {status}" in wait["waiting_on"] and "hermes kanban archive t_00000000000000aa" in wait["waiting_on"]


def test_replica_todo_authority_without_parent_facts_is_an_ordinary_wait():
    card = {"id": "t_00000000000000aa", "status": "triage", "contained": {
        "episode": "019a98b4-0000-7000-8000-000000000001", "contained_at": 1_800_000_000,
        "authority": {"id": "t_1123456789abcdef", "status": "todo", "at": None}, "producer": None}}
    assert kwi.describe_replica(card, now=1_800_000_100)["wait_state"] == "waiting"


def test_running_authority_is_still_an_ordinary_wait(board):
    relay, authority, _blocker, _ = _parked(board, live=True)
    wait = _wait(board, relay)
    assert wait["wait_state"] == "waiting" and wait["waiting_on"] == f"release by card {authority} (running)"


# --- dependents, Board refusals, marker fallback, changed retries ---------------------------------------------

def test_human_archive_lists_dependents_and_requires_yes(board, monkeypatch, capsys):
    relay, _authority, _blocker, _ = _parked(board)
    consumer = board.execute("SELECT child_id FROM task_links WHERE parent_id=?", (relay,)).fetchone()[0]
    _as(monkeypatch, HUMAN)
    assert _cli([relay], REASON) == 1
    out = capsys.readouterr()
    assert consumer in out.out and "cannot be unlinked" in out.out and "--yes" in out.err
    assert kb.get_task(board, relay).status == "triage"
    assert _cli([relay], REASON, yes=True) == 0
    assert kb.get_task(board, relay).status == "archived" and kb.get_task(board, consumer).status == "todo"


def test_human_archive_without_dependents_needs_no_yes(board, monkeypatch, capsys):
    relay, _authority, _blocker, _ = _parked(board)
    consumer = board.execute("SELECT child_id FROM task_links WHERE parent_id=?", (relay,)).fetchone()[0]
    assert kb.archive_task(board, consumer)
    _as(monkeypatch, HUMAN)
    assert _cli([relay], REASON) == 0
    assert "dependent" not in capsys.readouterr().out


@pytest.mark.parametrize("reason,detail", [
    ("authority_live", "authority t_x has a live run or lease; let it release the card"),
    ("containment_episode_changed", "the named episode is not the card's current containment episode"),
])
def test_synced_board_human_refusals_print_reason_and_detail(synced_conn, monkeypatch, capsys, reason, detail):
    hub = _synced(synced_conn, monkeypatch)
    hub.reject = {"reason": reason, "detail": detail}
    _as(monkeypatch, HUMAN)
    assert _cli([hub.target], REASON, yes=True) == 1
    err = capsys.readouterr().err
    assert f"Board refused terminal archive: {reason} ({detail})" in err and "--reason \"<why>\"" not in err
    assert len(hub.posts) == 1 and "human_operator" in hub.posts[0][0]
    assert kb.get_task(synced_conn, hub.target).status == "triage"
    assert synced_conn.execute("SELECT confirmed FROM containment_delivery").fetchone()[0] == -1


def test_unsynced_marker_falls_back_to_the_single_attested_episode(synced_conn, monkeypatch):
    hub = _synced(synced_conn, monkeypatch)
    assert synced_conn.execute("SELECT containment_episode FROM tasks WHERE id=?", (hub.target,)).fetchone()[0] is None
    assert kb.archive_task(synced_conn, hub.target, human=HumanArchive(REASON))
    assert hub.posts[0][0]["human_operator"]["episode"] == hub.stop["event_uid"]


def test_retry_with_a_changed_reason_is_refused_before_the_board(synced_conn, monkeypatch):
    hub = _synced(synced_conn, monkeypatch)
    hub.fail = True
    with pytest.raises(TimeoutError):
        kb.archive_task(synced_conn, hub.target, human=HumanArchive(REASON))
    hub.fail = False
    with pytest.raises(ContainmentRefused, match="different metadata"):
        kb.archive_task(synced_conn, hub.target, human=HumanArchive("another reason"))
    assert len(hub.posts) == 1
    assert kb.archive_task(synced_conn, hub.target, human=HumanArchive(REASON))  # the same reason recovers
    assert hub.posts[0] == hub.posts[1]
