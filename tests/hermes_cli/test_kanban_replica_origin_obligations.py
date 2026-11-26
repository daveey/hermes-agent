"""A readable empty Board replica cannot erase an adopted origin obligation."""
import time

import pytest

from hermes_cli import kanban_session_activity as activity
from tests.hermes_cli import test_kanban_chat_links_parity as parity

env = parity.env


def obligation(**changes):
    row = dict(id="t_adopted", gateway="hub", profile="worker", board="build",
               key="hub/worker/build/t_adopted", session="chat", origin_gateway="macbook",
               origin_profile="default", via="origin", status="running", unfinished=True,
               released=False, stale=False, synced_at=time.time(), title="Adopted work")
    return {**row, **changes}


@pytest.mark.parametrize("status,via", [("running", "origin"), ("ready", "shepherd"),
                                      ("unknown", "origin")])
def test_readable_replica_missing_adopted_link_keeps_count_and_task(env, status, via):
    parity.replica(env, [], [])
    env["ledger"].append(obligation(status=status, via=via))
    row, cards = parity.assert_parity("chat", expect=1)
    assert cards[0]["key"] == "hub/worker/build/t_adopted"
    assert cards[0]["status"] == status
    assert cards[0]["synced_at"] == env["ledger"][0]["synced_at"]
    assert row.get("kanban_active") == 1


@pytest.mark.parametrize("change", [{"released": True, "status": "done", "unfinished": False},
                                   {"origin_gateway": "foreign"}, {"origin_profile": "other"},
                                   {"session": "other"}, {"profile": ""}, {"gateway": "macbook"},
                                   {"unfinished": False}, {"status": "done"}, {"status": "archived"}])
def test_no_foreign_or_released_origin_obligation(env, change):
    parity.replica(env, [], [])
    env["ledger"].append(obligation(**change))
    parity.assert_parity("chat", expect=0)


@pytest.mark.parametrize("session,source", [("20261027_011805_fe895a", "kanban"), ("cron_abc_1", None),
                                            ("20261027_011805_fe895b", "subagent")])
def test_machine_session_released_replica_link_is_not_readded_from_ledger(env, session, source):
    # Live shape: a worker transcript whose replica link is released, with a stale unreleased ledger row.
    from hermes_state import SessionDB
    if source is not None:
        with SessionDB(db_path=env["home"] / "state.db") as db:
            db.create_session(session, source=source)
    parity.replica(env, [("t_adopted", "archived", "hub")],
                   [("t_adopted", session, "created", 1)], board="build")
    env["ledger"].append(obligation(session=session, status="unknown", stale=True, synced_at=1000))
    parity.assert_parity(session, expect=0)
    assert activity.session_tasks([session], stale_ok=True) == []


def test_interactive_session_with_same_shape_keeps_obligation(env):
    from hermes_state import SessionDB
    with SessionDB(db_path=env["home"] / "state.db") as db:
        db.create_session("20261027_011805_fe895c", source="desktop")
    parity.replica(env, [], [])
    env["ledger"].append(obligation(session="20261027_011805_fe895c"))
    parity.assert_parity("20261027_011805_fe895c", expect=1)


def test_placeholder_session_obligation_is_not_added(env, monkeypatch):
    from hermes_cli import kanban_placement
    monkeypatch.setattr(kanban_placement, "is_placeholder_session", lambda s, **kwargs: s == "placeholder")
    parity.replica(env, [], [])
    env["ledger"].append(obligation(session="placeholder"))
    parity.assert_parity("placeholder", expect=0)


def test_replica_and_ledger_same_qualified_card_not_double_counted(env):
    parity.replica(env, [("t_adopted", "running", "hub")],
                   [("t_adopted", "chat", "created", 0)], board="build")
    env["ledger"].append(obligation())
    parity.assert_parity("chat", expect=1)


def test_missing_link_keeps_source_uncertainty(env):
    parity.replica(env, [], [])
    env["ledger"].append(obligation(status="unknown", stale=True, synced_at=1000))
    row, cards = parity.assert_parity("chat", expect=1)
    assert row["kanban_remote_stale"] == 1
    assert cards[0]["synced_at"] == 1000
    assert cards[0]["status"] == "unknown"


def test_task_strip_stale_ok_keeps_origin_obligation(env):
    parity.replica(env, [], [])
    env["ledger"].append(obligation())
    cards = activity.session_tasks(["chat"], stale_ok=True)
    assert len(cards) == 1
    assert cards[0]["key"] == "hub/worker/build/t_adopted"


def test_post_turn_outcome_is_waiting_without_declared_containment(env):
    import hermes_state_outcome as outcome
    from hermes_state import SessionDB

    parity.replica(env, [], [])
    env["ledger"].append(obligation(status="ready", via="shepherd"))
    outcome._reset_hooks_for_tests()
    with SessionDB(db_path=env["tmp"] / "state.db") as db:
        db.create_session("chat", source="desktop")
        db.append_message("chat", role="user", content="Build the agreed suite")
        derived = outcome.derive_outcome("chat", db=db, ignore_live_turn=True,
                                        pending_reply="No. The requested suite is not built yet.")
        assert derived["kind"] == "waiting", derived
    outcome._reset_hooks_for_tests()


def test_origin_ledger_change_invalidates_count_fingerprint(env):
    parity.replica(env, [], [])
    before = activity._activity_fingerprint()
    path = env["home"] / "kanban" / "origin-ledger.db"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"synthetic revision marker")
    assert activity._activity_fingerprint() != before
    before = activity._activity_fingerprint()
    path.with_name(path.name + "-wal").write_bytes(b"synthetic WAL revision")
    assert activity._activity_fingerprint() != before


def test_ledger_failure_preserves_replica_rows_and_marks_partial_scope(env, monkeypatch):
    parity.replica(env, [("t_visible", "running", "hub")], [("t_visible", "chat", "created", 0)])
    def failed(stale_ok=False):
        raise RuntimeError("ledger unavailable")
    monkeypatch.setattr(activity, "_remote_links", failed)
    _, row = parity.counted("chat")
    assert row["kanban_unknown"] is True
    cards = parity.listed("chat")
    assert len(cards) == 1
    assert cards[0]["partial_source_unknown"] == "origin_ledger_unavailable"
    # A different chat's readable row must not conceal this chat's missing custody.
    _, absent = parity.counted("other-chat")
    assert absent["kanban_unknown"] is True
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        activity.session_tasks(["other-chat"])


def test_ledger_failure_without_replica_rows_is_not_empty_success(env, monkeypatch):
    parity.replica(env, [], [])
    def failed(stale_ok=False):
        raise RuntimeError("ledger unavailable")
    monkeypatch.setattr(activity, "_remote_links", failed)
    _, row = parity.counted("chat")
    assert row["kanban_unknown"] is True
    with pytest.raises(RuntimeError, match="ledger unavailable"):
        activity.session_tasks(["chat"])


def test_replica_release_alone_cannot_release_unverified_origin_custody(env):
    # The origin receipt guard, not a replicated link bit, owns delivery acceptance.
    parity.replica(env, [("t_adopted", "done", "hub")],
                   [("t_adopted", "chat", "created", 1)], board="build")
    env["ledger"].append(obligation(status="unknown", reported_status="done", stale=True))
    _, cards = parity.assert_parity("chat", expect=1)
    assert cards[0]["status"] == "unknown"
    env["ledger"][0].update(status="done", released=True, unfinished=False)
    activity._reset_cache_for_tests()
    parity.assert_parity("chat", expect=0)


def test_compressed_alias_deduplicates_qualified_origin_and_replica(env):
    parity.replica(env, [("t_adopted", "running", "hub")],
                   [("t_adopted", "root", "created", 0)], board="build")
    env["ledger"].append(obligation(session="tip", origin_session="root"))
    parity.assert_parity("root", "tip", expect=1)


def test_obligation_placeholder_policy_loaded_once_per_call(env, monkeypatch):
    from hermes_cli import config, kanban_origin_obligations as obligations

    loads = []
    placeholder = ['hidden']

    def read_policy():
        loads.append(1)
        return {'kanban': {'placement': {'creator_session': placeholder[0]}}}

    monkeypatch.setattr(config, 'load_config_readonly', read_policy)
    rows = [obligation(id=f't_{i}', session='hidden' if i == 0 else 'chat') for i in range(100)]

    def read(inventory):
        return obligations.preserve_origin_obligations([], iter(inventory), gateway='macbook',
            profile='default', ids=None, statuses=('running',))

    result = read(rows)
    assert len(result) == 99
    assert len(loads) == 1, 'one placement policy snapshot, not one full config load per obligation'
    placeholder[0] = 'chat'
    assert len(read(rows)) == 1, 'a subsequent call observes changed placeholder policy'
    assert len(loads) == 2
    assert read([obligation(released=True), obligation(origin_gateway='elsewhere')]) == []
    assert len(loads) == 2, 'filtered-only inventory must not load placement policy'
