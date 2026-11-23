"""Strict shadow parity checks against pinned published views. [daveey]"""

import json

from hermes_cli.fact_contract import Fact, Scope
from hermes_cli.fact_store import FactStore, view_key


def test_parity_uses_pinned_generation_and_distinguishes_json_types():
    from hermes_cli.fact_parity import ExpectedView, compare_views

    scope = Scope("install", "profile")
    store = FactStore()
    key = view_key(scope, "sessions", {"limit": 20})
    store.publish([Fact(key, {"encoded": b'{"total":true,"sessions":[]}'}, health="fresh")])
    pinned = store.snapshot(scope)
    store.publish([Fact(key, {"encoded": b'{"total":1,"sessions":[]}'}, health="fresh")])
    expected = [ExpectedView("sessions", {"limit": 20}, json.dumps({"total": 1, "sessions": []}).encode())]
    report = compare_views(pinned, expected)
    assert not report.passed
    assert report.checked == 1
    assert report.epoch == pinned.epoch and report.rev == pinned.rev
    assert report.failures[0].reason == "json_type"
    assert compare_views(store.snapshot(scope), expected).passed


def test_invalid_json_is_a_failed_case_not_an_aborted_or_false_pass():
    from hermes_cli.fact_parity import ExpectedView, compare_views

    scope = Scope("i", "p")
    store = FactStore()
    for bad in (b'{"x":1,"x":2}', b'{"x":NaN}', b'not json'):
        store.publish([Fact(view_key(scope, "board", {}), {"encoded": bad}, health="fresh")])
        report = compare_views(store.snapshot(scope), [ExpectedView("board", {}, bad)])
        assert not report.passed
        assert report.failures[0].reason == "invalid_json"


def test_nested_selector_remains_immutable_and_can_be_compared():
    from hermes_cli.fact_parity import ExpectedView, compare_views

    scope = Scope("i", "p")
    selector = {"filter": {"states": ["ready"]}}
    case = ExpectedView("board", selector, b'{}')
    store = FactStore()
    store.publish([Fact(view_key(scope, "board", selector), {"encoded": b'{}'}, health="fresh")])
    selector["filter"]["states"].append("done")
    assert compare_views(store.snapshot(scope), [case]).passed


def test_structural_parity_and_not_ready_are_not_ignored():
    from hermes_cli.fact_parity import ExpectedView, compare_views

    scope = Scope("i", "p")
    store = FactStore()
    assert not compare_views(store.snapshot(scope), []).passed
    expected = [ExpectedView("board", {}, b'{"a":null,"items":[1,2]}')]
    assert compare_views(store.snapshot(scope), expected).failures[0].reason == "view_not_ready"
    for body, reason in ((b'{"items":[1,2]}', "json_fields"),
                         (b'{"a":null,"items":[2,1]}', "json_value"),
                         (b'{"a":null,"items":[1]}', "json_length"),
                         (b'{"a":null,"items":[1.0,2]}', "json_type")):
        store.publish([Fact(view_key(scope, "board", {}), {"encoded": body}, health="fresh")])
        assert compare_views(store.snapshot(scope), expected).failures[0].reason == reason
    store.publish([Fact(view_key(scope, "board", {}), {"encoded": expected[0].encoded}, health="stale")])
    assert compare_views(store.snapshot(scope), expected).failures[0].reason == "view_not_ready"
