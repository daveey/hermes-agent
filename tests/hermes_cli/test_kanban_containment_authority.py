"""Authority admission and explicit retention, separate from review verdicts."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_containment_lifecycle as lifecycle
from tests.hermes_cli import test_kanban_containment_lifecycle as local
from tests.hermes_cli import test_kanban_containment_remote as remote

board = local.board


@pytest.mark.parametrize("status", ["done", "archived"])
def test_terminal_authority_cannot_be_nominated(board, status):
    relay, _, authority, target, spec = local.setup(board)
    board.execute("UPDATE tasks SET status=? WHERE id=?", (status, authority))
    with pytest.raises(lifecycle.ContainmentRefused) as refused:
        local.contain(board, relay, target, spec)
    assert refused.value.code == "authority_terminal"
    assert "kanban_request_review" in str(refused.value)
    assert kb.get_task(board, relay).status == "running"


@pytest.mark.parametrize("status", ["done", "archived"])
def test_remote_terminal_authority_refused_before_staging(board, monkeypatch, status):
    from gateway.session_context import scoped_current_session_id
    from hermes_cli import kanban_containment_service as service
    target, authority, session, hub = remote.setup(board, monkeypatch)
    hub.rows[("tasks", authority)]["status"] = status
    with scoped_current_session_id(session), pytest.raises(lifecycle.ContainmentRefused) as refused:
        service.contain(board, authority=authority, summary="Unsafe fixture run")
    assert refused.value.code == "authority_terminal"
    assert hub.posts == []
    assert kb.get_task(board, target).status == "running"


def test_remote_missing_status_preserves_compatible_context(board, monkeypatch):
    from gateway.session_context import scoped_current_session_id
    from hermes_cli import kanban_containment_service as service
    target, authority, session, hub = remote.setup(board, monkeypatch)
    original = hub.containment_context
    def without_status(*args):
        data = original(*args)
        data.pop("status")
        return data
    monkeypatch.setattr(hub, "containment_context", without_status)
    with scoped_current_session_id(session):
        result = service.contain(board, authority=authority, summary="Unsafe fixture")
    assert result["status"] == "triage"
    assert kb.get_task(board, target).containment_episode == result["episode"]


@pytest.mark.parametrize("retained", [None, "wrong", ["wrong"], [{}], []])
def test_malformed_or_stale_retention_does_not_acknowledge(board, retained):
    from hermes_cli import kanban_containment_authority as authority_gate
    relay, _, authority, target, spec = local.setup(board)
    local.contain(board, relay, target, spec)
    with pytest.raises(authority_gate.AuthorityOpen):
        authority_gate.require_acknowledgement(board, authority, {"containment_retained": retained})
    assert authority_gate.open_episodes(board, relay) == []
    assert authority_gate.render_context(board, relay) == ""


def test_authority_must_release_or_explicitly_retain(board):
    from hermes_cli import kanban_containment_authority as authority_gate
    relay, _, authority, target, spec = local.setup(board)
    episode = local.contain(board, relay, target, spec)["episode"]
    episodes = authority_gate.open_episodes(board, authority)
    assert episodes == [{"task_id": relay, "episode": episode, "title": kb.get_task(board, relay).title}]
    context = kb.build_worker_context(board, authority)
    assert "## Containment authority" in context
    assert episode in context and "kanban_resume_contained" in context
    with pytest.raises(authority_gate.AuthorityOpen) as refused:
        kb.complete_task(board, authority, summary="Review finished")
    assert refused.value.code == "containment_authority_open"
    assert refused.value.episodes == episodes
    assert kb.get_task(board, authority).status == "running"
    local.release(board, target, episode, authority)
    assert authority_gate.open_episodes(board, authority) == []
    assert kb.complete_task(board, authority, summary="Released unsafe run")


def test_retained_episode_is_explicit_and_persisted(board):
    from hermes_cli import kanban_containment_authority as authority_gate
    relay, _, authority, target, spec = local.setup(board)
    episode = local.contain(board, relay, target, spec)["episode"]
    with pytest.raises(authority_gate.AuthorityOpen):
        kb.complete_task(board, authority, summary="Keep hold", metadata={"containment_retained": ["wrong"]})
    metadata = {"verdict": "approve", "containment_retained": [episode]}
    assert kb.complete_task(board, authority, summary="Retain hold: unsafe fixture remains", metadata=metadata)
    assert kb.latest_run(board, authority).metadata["containment_retained"] == [episode]
    assert kb.get_task(board, relay).status == "triage"


def test_replica_only_episode_and_local_release_override(board, monkeypatch, tmp_path):
    from hermes_cli import kanban_containment_authority as authority_gate
    from hermes_cli import kanban_foreign_card as foreign
    relay, _, authority, target, spec = local.setup(board)
    episode = local.contain(board, relay, target, spec)["episode"]
    import sqlite3
    replica = sqlite3.connect(tmp_path / "replica.db", isolation_level=None)
    replica.row_factory = sqlite3.Row
    board.backup(replica)
    local.release(board, target, episode, authority)
    @contextmanager
    def open_replica(path):
        yield replica
    monkeypatch.setattr(foreign, "open_replica", open_replica)
    assert authority_gate.open_episodes(board, authority) == []
    # A separate gateway has no target row or history; its replica carries it.
    with sqlite3.connect(":memory:") as peer:
        peer.execute("CREATE TABLE tasks (id TEXT,title TEXT,containment_episode TEXT)")
        peer.execute("CREATE TABLE task_events (uid TEXT,task_id TEXT,kind TEXT,payload TEXT)")
        assert authority_gate.open_episodes(peer, authority)[0]["episode"] == episode
    replica.close()


def test_lookup_error_fails_open(board, monkeypatch, caplog):
    from hermes_cli import kanban_containment_authority as authority_gate
    import sqlite3
    _, _, authority, _, _ = local.setup(board)
    def broken(*args):
        raise sqlite3.OperationalError("fixture lookup failed")
    monkeypatch.setattr(authority_gate, "open_episodes", broken)
    assert kb.complete_task(board, authority, summary="Review complete despite advisory lookup failure")
    assert "fixture lookup failed" in caplog.text


def test_producer_release_still_works_with_done_authority(board):
    from hermes_cli.kanban_db_sync import uuid7
    relay, _, authority, target, spec = local.setup(board)
    producer = kb.create_task(board, title="New evidence fixture", assignee="default")
    spec["producer"] = {**target, "task_id": producer}
    episode = local.contain(board, relay, target, spec)["episode"]
    assert kb.complete_task(board, authority, summary="Retain pending producer evidence",
                            metadata={"containment_retained": [episode]})
    assert kb.claim_task(board, producer, claimer="default")
    board.execute("UPDATE task_runs SET uid=? WHERE task_id=?", (uuid7(), producer))
    assert kb.complete_task(board, producer, summary="New evidence ready")
    board.execute("UPDATE task_events SET uid=? WHERE task_id=? AND kind='completed'", (uuid7(), producer))
    actor = kb.create_task(board, title="Release executor fixture", assignee="default")
    assert kb.claim_task(board, actor, claimer="default")
    board.execute("UPDATE task_runs SET uid=? WHERE task_id=?", (uuid7(), actor))
    result = lifecycle.resume_local(board, target=target, episode=episode, actor_task=actor,
                                    actor_run_uid=local.run_uid(board, actor), kind="producer")
    assert result["status"] == "ready"


def test_placement_settle_is_not_gated_by_open_episodes(board):
    relay, _, authority, target, spec = local.setup(board)
    local.contain(board, relay, target, spec)
    assert kb.complete_task(board, authority, summary="remote settled", placement_settled=True)


def test_context_has_no_blank_line_without_episodes(board):
    relay = local.setup(board)[0]
    kb.add_comment(board, relay, "reviewer", "Later section keeps internal spacing observable")
    context = kb.build_worker_context(board, relay)
    assert "Later section keeps internal spacing observable" in context
    assert "\n\n\n" not in context


def test_containment_committed_after_precheck_still_refuses(board, monkeypatch):
    from hermes_cli import kanban_containment_authority as authority_gate
    from hermes_cli import kanban_completion_receipt as receipt
    relay, _, authority, target, spec = local.setup(board)
    real_prepare = receipt.prepare

    def prepare(conn, task_id, metadata, **kwargs):
        # Commit a real episode after the advisory precheck, before the write transaction.
        episode = local.contain(board, relay, target, spec)["episode"]
        assert kb.get_task(board, relay).containment_episode == episode
        return real_prepare(conn, task_id, metadata, **kwargs)

    monkeypatch.setattr(receipt, "prepare", prepare)
    with pytest.raises(authority_gate.AuthorityOpen) as refused:
        kb.complete_task(board, authority, summary="Review finished")
    assert refused.value.code == "containment_authority_open"
    assert refused.value.episodes[0]["task_id"] == relay
    assert kb.get_task(board, authority).status == "running"
