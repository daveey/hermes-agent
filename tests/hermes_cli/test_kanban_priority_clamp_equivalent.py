"""A sync pull's cap clamp must not strand the priority migration's value CAS.

Incident 2026-11-12 (op c314f72a, run 2): the global manifest captured raw Board
values (old=59000), but the hub's local copy of the same rows had been clamped
to PRIORITY_CAP by ``kanban_db_sync._local_fields`` on an equal-version re-send,
with no event and no outbox op. Apply and the guarded restore then both refused
with "priorities changed; capture a new manifest", and a re-capture could never
succeed because the Board keeps the raw value.
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_priority import PRIORITY_CAP
from tests.hermes_cli import test_kanban_priority_executor as plan_helpers
from tests.hermes_cli import test_kanban_priority_migration as helpers
from tests.hermes_cli import test_kanban_sync_client as sync_helpers

kanban_home = sync_helpers.kanban_home
conn = sync_helpers.live


def resend_from_board(conn, task_id, priority):
    """The Board re-sends an unchanged full row (equal version) carrying its raw priority."""
    from hermes_cli.kanban_db_sync import RemoteChange, apply_remote_changes
    version = conn.execute("SELECT COALESCE(sync_version, 1) FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    conn.execute("UPDATE tasks SET sync_version=? WHERE id=?", (version, task_id))
    conn.commit()
    change = RemoteChange(seq=1, table="tasks", uid=task_id, op="upsert", fields={"priority": priority},
                          version=version, hlc=f"0000000000002-0000-{sync_helpers.OTHER}",
                          origin_gateway=sync_helpers.OTHER)
    apply_remote_changes(conn, [change], new_cursor=1, observers=False)


def staged_then_clamped(conn):
    ids, attachment, digest = plan_helpers.setup_plan(conn)  # manifest old = [10000, 10000, 50000]
    resend_from_board(conn, ids[2], 50000)
    return ids, attachment, digest


def reprioritized(conn, task_id):
    return conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='reprioritized'",
                        (task_id,)).fetchone()[0]


def test_sync_resend_clamps_the_local_row_silently(conn):
    """The precondition the migration must tolerate: clamped locally, raw on the Board, no event."""
    ids, _, _ = staged_then_clamped(conn)
    assert helpers.priorities(conn, ids) == [10000, 10000, PRIORITY_CAP]
    assert reprioritized(conn, ids[2]) == 0


def test_apply_after_sync_clamp_does_not_refuse(conn):
    from hermes_cli import kanban_priority_executor as executor
    ids, attachment, digest = staged_then_clamped(conn)
    result = executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME)
    assert result["changed"] == 2
    assert helpers.priorities(conn, ids) == [450, 10000, 900]
    event = kb.list_events(conn, ids[2])[-1]
    assert event.kind == "reprioritized"
    assert event.payload["previous_priority"] == PRIORITY_CAP  # the value actually replaced, not the manifest's
    assert executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME)["changed"] == 0


def test_guarded_restore_never_raises_above_cap_without_legacy_flag(conn):
    from hermes_cli import kanban_priority_executor as executor
    ids, attachment, digest = staged_then_clamped(conn)
    executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME)
    result = executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME, undo=True)
    assert result["changed"] == 2
    assert helpers.priorities(conn, ids) == [PRIORITY_CAP, 10000, PRIORITY_CAP]
    assert executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME, undo=True)["changed"] == 0
    # A capped restore is the clamp-equivalent "old" state: apply can run again.
    assert executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME)["changed"] == 2
    assert helpers.priorities(conn, ids) == [450, 10000, 900]


def test_legacy_flag_restores_exact_originals_only_for_rows_apply_wrote(conn):
    from hermes_cli import kanban_priority_executor as executor
    ids, attachment, digest = staged_then_clamped(conn)
    executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME)
    executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME, undo=True, allow_legacy_rollback=True)
    assert helpers.priorities(conn, ids) == [10000, 10000, 50000]


@pytest.mark.parametrize("allow_legacy_rollback", [False, True])
def test_restore_of_a_never_applied_clamped_origin_is_a_noop(conn, allow_legacy_rollback):
    """Run 2's guarded restore: apply refused, so restore must not refuse nor raise the clamped row."""
    from hermes_cli import kanban_priority_executor as executor
    ids, attachment, digest = staged_then_clamped(conn)
    result = executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME, undo=True,
                                     allow_legacy_rollback=allow_legacy_rollback)
    assert result["changed"] == 0
    assert helpers.priorities(conn, ids) == [10000, 10000, PRIORITY_CAP]


@pytest.mark.parametrize("undo", [False, True])
def test_a_real_edit_still_refuses(conn, undo):
    """Clamp equivalence is exactly PRIORITY_CAP for an above-cap original; any other drift still refuses."""
    from hermes_cli import kanban_priority_executor as executor
    ids, attachment, digest = staged_then_clamped(conn)
    if undo:
        executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME)
    conn.execute("UPDATE tasks SET priority=? WHERE id=?", (PRIORITY_CAP - 1, ids[2]))
    conn.commit()
    before = helpers.priorities(conn, ids)
    with pytest.raises(ValueError, match="priorities changed"):
        executor.apply_attached(conn, attachment, digest, writer=sync_helpers.ME, undo=undo)
    assert helpers.priorities(conn, ids) == before


def test_clamp_equivalence_is_only_for_above_cap_originals():
    from hermes_cli.kanban_priority import at_value
    assert at_value(PRIORITY_CAP, 59000) and at_value(59000, 59000) and at_value(PRIORITY_CAP, PRIORITY_CAP)
    assert not at_value(PRIORITY_CAP, 950)  # a raise of an in-band value is an edit
    assert not at_value(PRIORITY_CAP - 1, 59000) and not at_value(900, 59000)


def test_canonical_verify_restore_accepts_the_capped_restore():
    from hermes_cli import kanban_priority_migration as migration
    from hermes_cli import kanban_priority_prepare as prepare
    from tests.hermes_cli.test_kanban_priority_verify import Canonical
    rows = [{"id": "a", "priority": 59000, "created_on": "hub", "status": "ready"},
            {"id": "b", "priority": 300, "created_on": "hub", "status": "ready"}]
    manifest = migration.plan_global(rows, board_id="board", exceptions=[])
    capped = [dict(rows[0], priority=PRIORITY_CAP), rows[1]]
    assert prepare.verify(Canonical(capped), manifest, undo=True)["verified"] == 2
    assert prepare.verify(Canonical(rows), manifest, undo=True)["verified"] == 2
    with pytest.raises(ValueError, match="canonical"):
        prepare.verify(Canonical(capped), manifest)  # apply direction stays exact
    with pytest.raises(ValueError, match="canonical"):
        prepare.verify(Canonical([rows[0], dict(rows[1], priority=PRIORITY_CAP)]), manifest, undo=True)
