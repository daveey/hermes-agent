"""Cross-scope narrow probes share work without feeding strict publication."""
import pytest

from hermes_cli import kanban_annotation_narrow as narrow
from hermes_cli import kanban_annotation_semantic as semantic
from hermes_cli import kanban_replica_rows as rr
from tests.hermes_cli import test_kanban_annotation_narrow as helpers

hub = helpers.hub


def _key(profile):
    scope = ("home", profile, "kh", "hub", "mail")
    result = narrow.key(scope, ("hubsig", ("board", 1)), [])
    assert result is not None
    return result


def test_eight_scopes_decode_one_inventory(hub, monkeypatch):
    calls = []
    original = semantic.task_token

    def counted(task):
        calls.append(task["id"])
        return original(task)

    monkeypatch.setattr(semantic, "task_token", counted)
    for i in range(8):
        _key(str(i))
    assert len(calls) == 2
    calls.clear()
    hub("UPDATE replica SET data=?,seq=:seq WHERE key=?",
        (helpers._task("t_other", status="done"), helpers.OTHER))
    for i in range(8):
        _key(str(i))
    assert calls == ["t_other"]


def test_skipped_generations_catch_up_without_full_selection(hub):
    before = _key("prof")
    state = narrow._state(helpers.SCOPE)
    scans = state.selector.full_scans
    for status in ("todo", "blocked", "done"):
        hub("UPDATE replica SET data=?,seq=:seq WHERE key=?",
            (helpers._task("t_linked", sessions=[helpers.LINK], status=status), helpers.LINKED))
        _key("other")
    after = _key("prof")
    assert after != before
    assert state.selector.full_scans == scans
    narrow.reset()
    assert _key("prof") == after


def test_catch_up_unions_different_keys_and_removals(hub):
    before = _key("prof")
    state = narrow._state(helpers.SCOPE)
    scans = state.selector.full_scans
    hub("DELETE FROM replica WHERE key=?", (helpers.LINKED,))
    _key("other")
    hub("UPDATE replica SET data=?,seq=:seq WHERE key=?",
        (helpers._task("t_other", sessions=[helpers.LINK]), helpers.OTHER))
    _key("other")
    after = _key("prof")
    assert after != before
    assert state.selector.full_scans == scans
    narrow.reset()
    assert _key("prof") == after


def test_error_discards_shared_inventory_and_reset_rebuilds(hub, monkeypatch):
    _key("prof")
    _key("other")
    hub("UPDATE replica SET data='{broken',seq=:seq WHERE key=?", (helpers.LINKED,))
    assert helpers.key() is None
    hub("UPDATE replica SET data=?,seq=:seq WHERE key=?",
        (helpers._task("t_linked", sessions=[helpers.LINK], status="done"), helpers.LINKED))
    other = _key("other")  # this scope still holds its pre-error cursor
    recovered = _key("prof")
    narrow.reset()
    assert _key("other") == other
    calls = []
    original = semantic.task_token

    def counted(task):
        calls.append(task["id"])
        return original(task)

    monkeypatch.setattr(semantic, "task_token", counted)
    narrow.reset()
    assert _key("prof") == recovered
    assert len(calls) == 2


@pytest.mark.parametrize("generations,full_scans", [(64, 0), (65, 1)])
def test_history_overflow_forces_full_selection(hub, generations, full_scans):
    _key("prof")
    state = narrow._state(helpers.SCOPE)
    scans = state.selector.full_scans
    for index in range(generations):
        hub("UPDATE replica SET data=?,seq=:seq WHERE key=?",
            (helpers._task("t_linked", sessions=[helpers.LINK], title=str(index)), helpers.LINKED))
        _key("other")
    after = _key("prof")
    assert state.selector.full_scans == scans + full_scans
    narrow.reset()
    assert _key("prof") == after


def test_unchanged_shared_reader_skips_inventory_sql(hub):
    from hermes_cli import kanban_annotation_reader as reader
    from hermes_cli import kanban_session_activity as ksa

    _key("prof")
    statements = []
    path = ksa._hub_db_path()
    assert path is not None
    with ksa.cached_ro(path, busy_budget=0) as conn:
        conn.set_trace_callback(statements.append)
    try:
        _key("other")
    finally:
        with ksa.cached_ro(path, busy_budget=0) as conn:
            conn.set_trace_callback(None)
    assert reader._SEQ_PROBE not in statements
    assert reader._PROBE not in statements


def test_snapshot_survives_other_scope_advance_and_reset(hub, monkeypatch):
    from hermes_cli import kanban_annotation_inventory as inventory

    before = _key("prof")
    state = narrow._state(helpers.SCOPE)
    original = state.selector.select
    interleaved = []

    def select(*args, **kwargs):
        if not interleaved:
            interleaved.append(True)
            hub("UPDATE replica SET data=?,seq=:seq WHERE key=?",
                (helpers._task("t_linked", sessions=[helpers.LINK], status="done"), helpers.LINKED))
            _key("other")
            inventory.reset()
        return original(*args, **kwargs)

    monkeypatch.setattr(state.selector, "select", select)
    assert _key("prof") == before  # snapshot captured before the interleaved commit
    after = _key("prof")
    assert after != before
    narrow.reset()
    assert _key("prof") == after


def test_narrow_does_not_populate_strict_rows_cache(hub):
    before = dict(rr._views)
    _key("prof")
    _key("other")
    assert rr._views == before
