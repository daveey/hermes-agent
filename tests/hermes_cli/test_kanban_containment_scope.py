"""Scope, epoch, spend and partial-evidence controls for local containment."""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_containment_lifecycle as lifecycle
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_sync import uuid7
from tests.hermes_cli import test_kanban_containment_lifecycle as helpers

board = helpers.board


@pytest.mark.parametrize("mutation,sql", [
    ("missing_run", None),
    ("ended", "UPDATE task_runs SET ended_at=1 WHERE id=?"),
    ("expired", "UPDATE task_runs SET claim_expires=1 WHERE id=?"),
    ("no_expiry", "UPDATE task_runs SET claim_expires=NULL WHERE id=?"),
    ("task_expired", "UPDATE tasks SET claim_expires=1 WHERE current_run_id=?"),
    ("task_no_expiry", "UPDATE tasks SET claim_expires=NULL WHERE current_run_id=?"),
    ("task_no_lock", "UPDATE tasks SET claim_lock=NULL WHERE current_run_id=?"),
    ("task_wrong_lock", "UPDATE tasks SET claim_lock='another-holder' WHERE current_run_id=?"),
    ("task_not_running", "UPDATE tasks SET status='ready' WHERE current_run_id=?"),
    ("different_current", "UPDATE tasks SET current_run_id=NULL WHERE current_run_id=?"),
    ("malformed_uid", None),
])
def test_actor_must_be_current_live_lease_not_just_a_run_record(board, mutation, sql):
    relay, _, _, target, spec = helpers.setup(board)
    uid = {"missing_run": uuid7(), "malformed_uid": "invalid"}.get(mutation, helpers.run_uid(board, relay))
    rid = kb.get_task(board, relay).current_run_id
    if sql is not None:
        board.execute(sql, (rid,))
    with pytest.raises(lifecycle.ContainmentRefused, match="actor"):
        lifecycle.contain_local(board, target=target, resume_policy=spec, actor_task=relay,
                                actor_run_uid=uid, summary="Fixture")
    assert not board.execute("SELECT 1 FROM task_events WHERE kind='contained'").fetchone()


@pytest.mark.parametrize("summary", [None, "", " ", "a" * 2001])
def test_containment_summary_is_bounded_before_installation(board, summary):
    relay, _, _, target, spec = helpers.setup(board)
    with pytest.raises(lifecycle.ContainmentRefused, match="summary"):
        lifecycle.contain_local(board, target=target, resume_policy=spec, actor_task=relay,
                                actor_run_uid=helpers.run_uid(board, relay), summary=summary)
    assert not board.execute("SELECT 1 FROM sqlite_master WHERE name='containment_attestations'").fetchone()


def test_live_ancestor_can_contain_unclaimed_relay_but_unrelated_actor_cannot(board):
    relay, consumer, authority, target, spec = helpers.setup(board)
    # No lifecycle shortcut is used to end the old relay fixture run.
    assert kb.block_task(board, relay, reason="Fixture release")
    board.execute("UPDATE tasks SET status='todo' WHERE id=?", (relay,))
    actor_uid = helpers.run_uid(board, authority)
    with pytest.raises(lifecycle.ContainmentRefused, match="descendant"):
        lifecycle.contain_local(board, target=target, resume_policy=spec, actor_task=authority,
                                actor_run_uid=actor_uid, summary="Fixture")
    kb.link_tasks(board, authority, relay)
    edges = helpers.legacy._edges(board)
    contained = lifecycle.contain_local(board, target=target, resume_policy=spec, actor_task=authority,
                                        actor_run_uid=actor_uid, summary="Fixture")
    assert kb.get_task(board, authority).status == "running"
    assert kb.get_task(board, consumer).status == "todo"
    resumed = helpers.release(board, target, contained["episode"], authority)
    assert resumed["status"] == "todo"  # the still-live authority parent is not satisfied by release
    assert helpers.legacy._edges(board) == edges


@pytest.mark.parametrize("field,value", [("human_request", "{}"), ("status", "done"), ("status", "archived")])
def test_foreign_containment_does_not_overwrite_human_or_terminal_custody(board, field, value):
    relay, _, authority, target, spec = helpers.setup(board)
    assert kb.block_task(board, relay, reason="Fixture release")
    kb.link_tasks(board, authority, relay)
    board.execute(f"UPDATE tasks SET {field}=? WHERE id=?", (value, relay))
    with pytest.raises(lifecycle.ContainmentRefused):
        lifecycle.contain_local(board, target=target, resume_policy=spec, actor_task=authority,
                                actor_run_uid=helpers.run_uid(board, authority), summary="Fixture")


def test_containment_release_never_promotes_independent_spend_stop(board):
    relay, _, authority, target, spec = helpers.setup(board)
    with kb.write_txn(board):
        kb._append_event(board, relay, "spend_budget_triaged", {"fixture": True})
    before = [tuple(r) for r in board.execute("SELECT * FROM task_events WHERE kind='spend_budget_triaged'")]
    episode = helpers.contain(board, relay, target, spec)["episode"]
    result = helpers.release(board, target, episode, authority)
    assert result["status"] == "triage"
    assert before == [tuple(r) for r in board.execute("SELECT * FROM task_events WHERE kind='spend_budget_triaged'")]
    assert not board.execute("SELECT 1 FROM task_events WHERE kind='promoted_manual'").fetchone()


def test_finishing_preexisting_producer_run_is_not_new_readiness(board):
    relay, _, authority, target, spec = helpers.setup(board)
    producer = kb.create_task(board, title="Inflight producer fixture", assignee="default")
    assert kb.claim_task(board, producer, claimer="default")
    rid = kb.get_task(board, producer).current_run_id
    puid = uuid7()
    board.execute("UPDATE task_runs SET uid=? WHERE id=?", (puid, rid))
    spec["producer"] = {**target, "task_id": producer}
    episode = helpers.contain(board, relay, target, spec)["episode"]
    frozen = json.loads(board.execute("SELECT payload FROM task_events WHERE uid=?", (episode,)).fetchone()[0])
    assert frozen["producer_baseline"] == {"event_uid": None, "run_uid": puid}
    assert kb.complete_task(board, producer, summary="Inflight run completed")
    board.execute("UPDATE task_events SET uid=? WHERE task_id=? AND kind='completed'", (uuid7(), producer))
    with pytest.raises(lifecycle.ContainmentRefused, match="new"):
        lifecycle.resume_local(board, target=target, episode=episode, actor_task=authority,
                               actor_run_uid=helpers.run_uid(board, authority), kind="producer")
    assert kb.get_task(board, relay).status == "triage"


def test_missing_producer_baseline_uid_refuses_without_ending_relay(board):
    relay, _, authority, target, spec = helpers.setup(board)
    spec["producer"] = {**target, "task_id": authority}
    board.execute("UPDATE task_runs SET uid=NULL WHERE task_id=?", (authority,))
    with pytest.raises(lifecycle.ContainmentRefused, match="baseline"):
        helpers.contain(board, relay, target, spec)
    assert kb.get_task(board, relay).status == "running"


def test_repeated_contain_and_replayed_release_refuse_instead_of_minting_new_authority(board):
    relay, _, authority, target, spec = helpers.setup(board)
    # Ancestor actor remains live and so can actually exercise the duplicate branch.
    assert kb.block_task(board, relay, reason="Fixture release")
    board.execute("UPDATE tasks SET status='todo' WHERE id=?", (relay,))
    kb.link_tasks(board, authority, relay)
    args = dict(target=target, resume_policy=spec, actor_task=authority,
                actor_run_uid=helpers.run_uid(board, authority), summary="Fixture")
    first = lifecycle.contain_local(board, **args)
    with pytest.raises(lifecycle.ContainmentRefused, match="already contained"):
        lifecycle.contain_local(board, **args)
    helpers.release(board, target, first["episode"], authority)
    with pytest.raises(lifecycle.ContainmentRefused, match="stale"):
        helpers.release(board, target, first["episode"], authority)
    assert board.execute("SELECT COUNT(*) FROM containment_attestations").fetchone()[0] == 1
