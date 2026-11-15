"""M3: a refused source attach settles the local observation without erasing evidence of an attach that may apply.

Real Git/journal; only the Board transport is the fake. Board refusal codes follow
hermes-hub ``worker/src/workspace_source.ts`` ``sourceAttach``: codes raised after its same-run attachment
lookup (or request-deterministic before it) prove nothing is attached for the run; ``lease_lost``,
``source_attach_conflict`` and unknown codes do not.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_source_board as board
from hermes_cli import kanban_source_launch as launch
from hermes_cli import kanban_source_registration as reg
from hermes_cli import kanban_workspace_source as ws
from tests.hermes_cli import test_kanban_source_registration as fixtures
from tests.hermes_cli.test_kanban_workspace_source import manifest

setup = fixtures.setup


def _row(conn, tid):
    return board.attached_run(conn, kb.get_task(conn, tid).current_run_id)


@pytest.mark.parametrize("code", ["legacy_adopt_refused", "initial_attach_refused", "source_client_required"])
def test_definite_initial_refusal_is_typed_and_kept_as_refused(setup, code):
    conn, b, tid, source, *_ = setup
    b.refuse["source_attach"] = code
    with pytest.raises(launch.AttachRefused) as info:
        reg.register_initial(conn, tid, str(source))
    assert info.value.code == code and not info.value.uncertain
    run = _row(conn, tid)
    assert run is not None and run.state == "refused" and run.path == str(source)


def test_old_board_initial_is_typed_board_lacks_source(setup):
    conn, b, tid, source, *_ = setup
    b.old = True
    with pytest.raises(launch.AttachRefused) as info:
        reg.register_initial(conn, tid, str(source))
    assert info.value.code == "board_lacks_source" and not info.value.uncertain
    assert _row(conn, tid).state == "refused"


@pytest.mark.parametrize("code", ["lease_lost", "source_attach_conflict", "something_new"])
def test_uncertain_initial_refusal_keeps_pending(setup, code):
    conn, b, tid, source, *_ = setup
    b.refuse["source_attach"] = code
    with pytest.raises(launch.AttachRefused) as info:
        reg.register_initial(conn, tid, str(source))
    assert info.value.code == code and info.value.uncertain
    assert _row(conn, tid).state == "pending"


def test_refused_replay_reopens_pending_before_rpc_and_attaches(setup):
    conn, b, tid, source, *_ = setup
    b.refuse["source_attach"] = "legacy_adopt_refused"
    with pytest.raises(launch.AttachRefused):
        reg.register_initial(conn, tid, str(source))
    del b.refuse["source_attach"]
    answer = b.answer

    def inspect(body):
        assert _row(conn, tid).state == "pending"  # an unanswered replay must not look refused
        return answer(body)

    b.answer = inspect
    assert reg.register_initial(conn, tid, str(source)).state == "attached"


def test_refusal_never_overwrites_a_concurrent_settlement(setup):
    conn, b, tid, source, *_ = setup
    answer = b.answer

    def concurrent(body):
        board.update_run(conn, kb.get_task(conn, tid).current_run_id, state="attached")
        return {"status": "rejected", "reason": "legacy_adopt_refused", "detail": "late answer"}

    b.answer = concurrent
    with pytest.raises(launch.AttachRefused):
        reg.register_initial(conn, tid, str(source))
    b.answer = answer
    assert _row(conn, tid).state == "attached"


def _published_attach(setup):
    conn, _b, tid, source, scratch, _home, _clone = setup
    raw = {**manifest(), "version": 2, "source_kind": "scratch_external",
           "repository": {"id": 1, "full_name": "example/source", "url": "https://github.com/example/source"}}
    snapshot = ws.SourceSnapshot(raw, ws.Custody("other", "published", 1, source_protocol=2))
    resolved = SimpleNamespace(path=str(source), branch=f"wt/{tid}", mode="materialized", manifest_revision=1)
    return launch.attach(conn, kb.get_task(conn, tid), str(scratch), resolved, snapshot)


def test_published_attach_definite_refusal_forgets_observation(setup):
    conn, b, tid, *_ = setup
    b.refuse["source_attach"] = "custody_attached"
    with pytest.raises(launch.AttachRefused) as info:
        _published_attach(setup)
    assert info.value.code == "custody_attached" and not info.value.uncertain
    assert _row(conn, tid) is None


@pytest.mark.parametrize("code", ["lease_lost", "source_attach_conflict", "something_new"])
def test_published_attach_uncertain_refusal_keeps_pending(setup, code):
    conn, b, tid, *_ = setup
    b.refuse["source_attach"] = code
    with pytest.raises(launch.AttachRefused) as info:
        _published_attach(setup)
    assert info.value.code == code and info.value.uncertain
    assert _row(conn, tid).state == "pending"


def test_published_attach_refusal_never_deletes_a_concurrent_settlement(setup):
    conn, b, tid, *_ = setup
    answer = b.answer

    def concurrent(body):
        board.update_run(conn, kb.get_task(conn, tid).current_run_id, state="attached")
        return {"status": "rejected", "reason": "custody_attached", "detail": "late answer"}

    b.answer = concurrent
    with pytest.raises(launch.AttachRefused):
        _published_attach(setup)
    b.answer = answer
    assert _row(conn, tid).state == "attached"
