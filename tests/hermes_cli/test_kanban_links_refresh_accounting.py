"""Refresh checks are not rebuilds; attribute actual decisions without extra IO."""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_session_activity as activity
from hermes_cli import loop_accounting as la


@pytest.fixture
def harness(monkeypatch):
    scope = ("accounting-scope",)
    state = {"now": 10.0, "fingerprint": (("/board.db", (1,)),), "builds": 0}
    monkeypatch.setattr(activity, "time", SimpleNamespace(monotonic=lambda: state["now"]))
    monkeypatch.setattr(activity, "_origin_scope", lambda: scope)
    monkeypatch.setattr(activity, "_links_cache", {})
    monkeypatch.setattr(activity, "_links_fingerprint", lambda: state["fingerprint"])
    monkeypatch.setattr(activity, "_board_db_paths", list)

    def project():
        state["builds"] += 1
        if state.get("fail"):
            raise ValueError("projection failed")
        return [{"id": "one"}]

    monkeypatch.setattr(activity, "_project_remote_links", project)
    activity._links_narrow.reset()
    yield state, scope
    activity._links_narrow.reset()


def runs():
    return {k: v["runs"] for k, v in la.snapshot()["loops"].items()}


def delta(before, name):
    return runs().get(name, 0) - before.get(name, 0)


def test_background_checks_distinguish_cold_skip_and_age(harness):
    state, scope = harness
    before = runs()
    activity._refresh_links_scope(scope)
    activity._refresh_links_scope(scope)
    state["now"] += activity._LINKS_MAX_AGE_S
    activity._refresh_links_scope(scope)
    assert state["builds"] == 2
    for name in ("cold", "skip", "age"):
        assert delta(before, "links_refresh." + name) == 1, name
    assert delta(before, "links_refresh.build") == 2


@pytest.mark.parametrize("entry,kind", [
    (("/board.db", (2,)), "board"),
    (("/hub.db", None, (("tasks", 1, 2, 2),), "gateway"), "hub"),
    (("/kanban/origin-ledger.db-wal", 1, 2, 3), "ledger"),
])
def test_changed_store_is_counted_at_real_rebuild(harness, entry, kind):
    state, scope = harness
    state["fingerprint"] = (entry,)
    activity._refresh_links_scope(scope)
    before = runs()
    state["fingerprint"] = ()  # deletion also has to name the old store
    activity._refresh_links_scope(scope)
    assert state["builds"] == 2
    assert delta(before, "links_refresh.inputs") == 1
    assert delta(before, "links_refresh.changed." + kind) == 1
    assert delta(before, "links_refresh.age") == 0


def test_expiry_and_changed_inputs_are_both_reported(harness):
    state, scope = harness
    activity._refresh_links_scope(scope)
    before = runs()
    state["now"] += activity._LINKS_MAX_AGE_S
    state["fingerprint"] = (("/board.db", (2,)),)
    activity._refresh_links_scope(scope)
    assert delta(before, "links_refresh.age_inputs") == 1
    assert delta(before, "links_refresh.changed.board") == 1
    assert delta(before, "links_refresh.build") == 1


def test_narrowed_cache_hit_is_not_a_rebuild(harness, monkeypatch):
    state, scope = harness
    activity._refresh_links_scope(scope)
    before = runs()
    state["fingerprint"] = (("/unread.db", (2,)),)
    monkeypatch.setattr(activity._links_narrow, "matches", lambda hit, fingerprint: True)
    activity._refresh_links_scope(scope)
    assert state["builds"] == 1
    assert delta(before, "links_refresh.skip") == 1
    assert delta(before, "links_refresh.build") == 0


def test_projection_error_is_counted_and_not_published(harness):
    state, scope = harness
    state["fail"] = True
    before = runs()
    with pytest.raises(ValueError, match="projection failed"):
        activity._refresh_links_scope(scope)
    assert scope not in activity._links_cache
    assert delta(before, "links_refresh.build") == 1
    state["fail"] = False
    activity._refresh_links_scope(scope)
    assert state["builds"] == 2


def test_multiple_raw_store_changes_count_each_category_once(harness):
    state, scope = harness
    state["fingerprint"] = (("/a.db", (1,)), ("/b.db", (1,)), ("/stable.db", (1,)))
    activity._refresh_links_scope(scope)
    before = runs()
    state["fingerprint"] = (("/a.db", (2,)), ("/b.db", (2,)), ("/stable.db", (1,)),
                            ("/kanban/origin-ledger.db", None), ("/hub.db", None, (), "gw"), "opaque")
    activity._refresh_links_scope(scope)
    for kind in ("board", "ledger", "hub", "unknown"):
        assert delta(before, "links_refresh.changed." + kind) == 1
    assert delta(before, "links_refresh.build") == 1


def test_changed_scope_neither_builds_nor_reports_a_cache_decision(harness):
    state, _scope = harness
    before = runs()
    activity._refresh_links_scope(("different",))
    assert state["builds"] == 0
    assert runs() == before
