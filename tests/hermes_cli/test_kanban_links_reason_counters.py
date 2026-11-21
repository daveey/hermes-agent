"""Diagnostic attribution must not change link-cache admission or read custody."""
from types import SimpleNamespace
import threading

import pytest

from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_sync_client as sync
from hermes_cli import kanban_links_chat_key as chat
from hermes_cli import loop_accounting as la
from tests.hermes_cli import test_kanban_links_refresh_accounting as refresh

harness = refresh.harness


def runs():
    return refresh.runs()


def delta(before, name):
    return refresh.delta(before, name)


def test_strict_unchanged_skip_without_publish(harness):
    state, scope = harness
    activity._links_lookup(scope, False)
    before = runs()
    activity._links_lookup(scope, False)
    assert state["builds"] == 1
    assert delta(before, "links_strict.hit") == 1
    assert delta(before, "links_strict.publish") == 0
    assert delta(before, "links_strict.fingerprint") == 1


def test_strict_same_profile_inventory_insertion_publishes(harness):
    state, scope = harness
    activity._links_lookup(scope, False)
    before = runs()
    state["fingerprint"] += (("/same-profile-new-board.db", (1,)),)
    activity._links_lookup(scope, False)
    assert state["builds"] == 2
    assert delta(before, "links_strict.miss.inputs") == 1
    assert delta(before, "links_strict.publish") == 1
    assert delta(before, "links_strict.projection") == 1


def test_strict_expiry_is_age(harness):
    state, scope = harness
    activity._links_lookup(scope, False)
    state["now"] += activity._LINKS_MAX_AGE_S
    before = runs()
    activity._links_lookup(scope, False)
    assert delta(before, "links_strict.miss.age") == 1
    assert delta(before, "links_strict.publish") == 1


def test_cold_and_combined_age_inputs_and_background_projection(harness):
    state, scope = harness
    before = runs()
    activity._links_lookup(scope, False)
    assert delta(before, "links_strict.miss.missing") == 1
    state["now"] += activity._LINKS_MAX_AGE_S
    state["fingerprint"] = (("/board.db", (2,)),)
    activity._links_lookup(scope, False)
    assert delta(before, "links_strict.miss.age_inputs") == 1
    state["fingerprint"] = (("/board.db", (3,)),)
    activity._refresh_links_scope(scope)
    assert delta(before, "links_refresh.projection") == 1
    assert delta(before, "links_refresh.fingerprint") == 1
    assert delta(before, "links_strict.projection") == 2


def test_display_stale_still_avoids_fingerprint_and_keeps_separate_lane(harness, monkeypatch):
    state, scope = harness
    activity._links_lookup(scope, False)
    submitted = []
    monkeypatch.setattr(activity, "_refresh_links_in_background", submitted.append)
    before = runs()
    activity._links_lookup(scope, True)
    assert submitted == [scope] and state["builds"] == 1
    assert delta(before, "links_display.stale") == 1
    assert delta(before, "links_display.enter") == delta(before, "links_display.exit") == 1
    assert delta(before, "links_strict.fingerprint") == 0


def test_failed_strict_projection_retries_without_publish(harness):
    state, scope = harness
    state["fail"] = True
    before = runs()
    with pytest.raises(ValueError):
        activity._links_lookup(scope, False)
    assert scope not in activity._links_cache
    assert delta(before, "links_strict.publish") == 0
    assert delta(before, "links_strict.projection") == 1
    assert delta(before, "links_strict.enter") == delta(before, "links_strict.exit") == 1
    state["fail"] = False
    activity._links_lookup(scope, False)
    assert delta(before, "links_strict.publish") == 1


@pytest.mark.parametrize("case,reason,component", [
    ("missing", "missing", None), ("inputs", "degraded", None),
    ("changed", "changed", "ledger"), ("age", "age", None),
    ("chat", "chat-replay", None), ("degraded", "degraded", None),
    ("skip", "skip", None),
])
def test_board_reason_from_original_decision(monkeypatch, case, reason, component):
    inputs = ((1, 2), (3,), ("absent",), frozenset(), "on", ("config",))
    done = (inputs, 10.0, {} if case not in ("age",) else None)
    monkeypatch.setattr(sync, "_LINKS_DONE", {} if case == "missing" else {"key": done})
    monkeypatch.setattr(sync, "_links_clock", lambda: 10.0 + sync.LINKS_MAX_AGE_S)
    if case == "changed":
        inputs = (*inputs[:3], frozenset({"new"}), *inputs[4:])
    if case == "inputs":
        inputs = None
    reads = SimpleNamespace(unchanged=lambda key: case not in ("chat", "degraded"),
                            degraded=case == "degraded")
    before = runs()
    skipped, _ = sync._links_unchanged("key", inputs, "reason-test", 0, reads)
    assert skipped == (case == "skip")
    prefix = "sync.links.board.reason-test."
    assert delta(before, prefix + "reason." + reason) == 1
    if component:
        assert delta(before, prefix + "changed." + component) == 1
    assert ("key" in sync._LINKS_DONE) == skipped


def test_concurrent_strict_and_background_attribution(harness, monkeypatch):
    state, scope = harness
    entered, release = threading.Event(), threading.Event()
    original = activity._project_remote_links
    errors = []

    def blocked():
        entered.set()
        assert release.wait(5)
        return original()

    def background():
        try:
            activity._refresh_links_scope(scope)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(activity, "_project_remote_links", blocked)
    before = runs()
    thread = threading.Thread(target=background)
    thread.start()
    try:
        assert entered.wait(5)
        assert delta(before, "links_refresh.enter") == 1
        assert delta(before, "links_refresh.exit") == 0
    except BaseException:
        release.set()
        thread.join(5)
        raise
    strict_entered = threading.Event()
    fingerprint = activity._links_fingerprint

    def strict_fingerprint():
        strict_entered.set()
        return fingerprint()

    def strict():
        try:
            activity._links_lookup(scope, False)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(activity, "_links_fingerprint", strict_fingerprint)
    strict_thread = threading.Thread(target=strict)
    strict_thread.start()
    try:
        assert strict_entered.wait(5)
        assert delta(before, "links_strict.enter") == 1
        assert delta(before, "links_strict.exit") == 0
    finally:
        release.set()
    strict_thread.join(5)
    thread.join(5)
    assert not strict_thread.is_alive() and not thread.is_alive() and not errors
    assert state["builds"] == 1
    assert delta(before, "links_refresh.build") == 1
    assert delta(before, "links_refresh.publish") == 1
    assert delta(before, "links_strict.publish") == 0
    assert delta(before, "links_strict.hit") == 1
    assert delta(before, "links_refresh.enter") == delta(before, "links_refresh.exit") == 1


def test_pass_local_reuse_keeps_one_read():
    calls = []
    with chat.pass_reads() as reads, reads.board():
        assert reads.answer(("source", "path", "id"), lambda: calls.append(1) or "desktop") == "desktop"
        assert reads.answer(("source", "path", "id"), lambda: calls.append(2)) == "desktop"
        chat.degrade()
    assert calls == [1] and reads.key() is None


@pytest.mark.parametrize("index,component", enumerate(("identity", "revision", "replica", "ledger", "mode", "signature")))
def test_each_changed_component_is_named(index, component):
    from hermes_cli import kanban_links_diagnostics as diag
    old = (1, 2, 3, 4, 5, 6)
    new = list(old)
    new[index] = "changed"
    before = runs()
    diag.board_reason("components", "changed", old, tuple(new))
    assert delta(before, "sync.links.board.components.changed." + component) == 1
    for other in diag._COMPONENTS:
        if other != component:
            assert delta(before, "sync.links.board.components.changed." + other) == 0


def test_board_names_are_bounded_without_evicting_admitted_boards(monkeypatch):
    from hermes_cli import kanban_links_diagnostics as diag
    monkeypatch.setattr(diag, "_boards", {})
    first = diag.board_name("first")
    for i in range(100):
        diag.board_name(str(i))
    assert len(diag._boards) == 16
    assert diag.board_name("first") == first
    assert diag.board_name("new") == "sync.links.board.other"
    assert diag.board_name("x" * 81) == "sync.links.board.other"
    assert len(diag._boards) == 16


def test_nested_source_context_is_restored_after_error():
    from hermes_cli import kanban_links_diagnostics as diag
    before = runs()
    with diag.source("links_strict"):
        with pytest.raises(ValueError), diag.source("links_refresh"):
            raise ValueError("read failed")
        diag.published()
    assert delta(before, "links_strict.publish") == 1
    assert delta(before, "links_refresh.publish") == 0
    assert diag._lane.get() == "links_other"


def test_replay_exception_is_degraded_not_value_change(monkeypatch):
    import sqlite3
    from hermes_cli import kanban_links_diagnostics as diag

    def failed(*args):
        raise sqlite3.OperationalError("required read failed")

    monkeypatch.setattr(chat, "_replay", failed)
    with chat.pass_reads() as reads:
        assert diag.board_decision(((1,), 0, {}), (1,), 0, 300, reads) == "degraded"
        assert reads.used is None and reads.degraded
