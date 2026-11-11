"""Shared fixtures are also consumed by the sidebar, panel and idle checker.

The TypeScript projector loads this same file (apps/desktop/src/lib/execution-observations.test.ts);
verdicts live only in the fixture, never in either language's test.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from hermes_cli.execution_observations import project, same_json, select, summarize

FIXTURE = json.loads((Path(__file__).parents[2] / "schemas/fixtures/execution-observations-v1.json").read_text())


def apply(value, patch):
    for path, replacement in patch.get("set", {}).items():
        keys = path.split(".")
        parent = value
        for key in keys[:-1]:
            parent = parent[key]
        parent[keys[-1]] = deepcopy(replacement)
    for path in patch.get("delete", []):
        keys = path.split(".")
        parent = value
        for key in keys[:-1]:
            parent = parent[key]
        del parent[keys[-1]]
    return value


def case_input(case):
    return apply(deepcopy(FIXTURE["base"]), case)


def item_input(item):
    return deepcopy(item["raw"]) if "raw" in item else case_input(item)


def subset(actual, expected):
    """Recursive subset: bool is distinct from number, 7.0 equals 7, missing is not null."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and subset(actual[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(subset(a, e) for a, e in zip(actual, expected)))
    return same_json(actual, expected)


def present(value, path):
    for key in path.split("."):
        if isinstance(value, list) and key.isdecimal():
            if int(key) >= len(value):
                return False
            value = value[int(key)]
            continue
        if not isinstance(value, dict) or key not in value:
            return False
        value = value[key]
    return True


def check(actual, case):
    expected = case["expected"]
    view = {**actual, "states": [o["state"] for o in actual.get("observations", [])]}
    assert subset(view, expected), {"expected": expected, "actual": {k: view.get(k) for k in expected}}
    for path in case.get("expected_absent", []):
        assert not present(actual, path), path


def test_fixture_sections_are_nonempty_and_unique():
    ids = [c["id"] for section in ("cases", "select_cases", "summarize_cases") for c in FIXTURE[section]]
    assert len(ids) == len(set(ids)) and len(FIXTURE["cases"]) > 150 and FIXTURE["select_cases"] and FIXTURE["summarize_cases"]


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda case: case["id"])
def test_shared_semantics(case):
    value = case_input(case)
    before = deepcopy(value)
    actual = project(value, now=case.get("now", 1001), source_class=case.get("source_class", "sidebar"))
    check(actual, case)
    assert value == before  # pure: input (including private probes) is never mutated
    if "delivery_receipt" in value and case["expected"].get("state") == "done":
        assert actual["delivery_receipt"] == value["delivery_receipt"]


@pytest.mark.parametrize("case", FIXTURE["select_cases"], ids=lambda case: case["id"])
def test_shared_select(case):
    values = deepcopy(case["values_raw"]) if "values_raw" in case else [item_input(i) for i in case["items"]]
    before = deepcopy(values)
    if "repeat_count" in case:
        values *= case["repeat_count"]
        before *= case["repeat_count"]
    identity = case.get("identity", FIXTURE["base"]["identity"])
    actual = select(values, identity=deepcopy(identity), now=case["now"], source_class=case.get("source_class", "sidebar"))
    check(actual, case)
    assert values == before
    if isinstance(values, list) and not case.get("values_raw"):  # arrival order never changes the verdict
        again = select(list(reversed(values)), identity=deepcopy(identity), now=case["now"],
                       source_class=case.get("source_class", "sidebar"))
        assert same_json(again, actual)
    if "outstanding" in case:
        for observation in (actual, project(actual, now=case["now"]),
                            select([actual], identity=identity, now=case["now"])):
            assert same_json(observation.get("retained_human_requests"), actual.get("retained_human_requests"))
            assert same_json(observation.get("historical_delivery_receipts"), actual.get("historical_delivery_receipts"))
            assert all(not any(k in request for k in ("resolved_check", "resolved_check_every"))
                       for request in observation["retained_human_requests"])
            summary = summarize([observation], now=case["now"])
            assert summary["outstanding_human_requests"] == case["outstanding"]
            assert summary["counts"]["unknown"] == 1
            assert summary["counts"]["done"] == 0
            repeated = summarize(summary["observations"], now=case["now"])
            assert same_json(repeated, summary)
            doubled = summarize([observation, observation], now=case["now"])
            assert doubled["outstanding_human_requests"] == 2 * case["outstanding"]
            assert doubled["counts"]["unknown"] == 2



@pytest.mark.parametrize("case", FIXTURE["summarize_cases"], ids=lambda case: case["id"])
def test_shared_summarize(case):
    values = deepcopy(case["values_raw"]) if "values_raw" in case else [item_input(i) for i in case["items"]]
    before = deepcopy(values)
    check(summarize(values, now=case["now"]), case)
    assert values == before


def test_human_request_validity_agrees_with_kanban_authority():
    """Visible request semantics match the kanban authority (except BOM, which only the shared projector trims)."""
    from hermes_cli.kanban_human_request import needs_human

    checked = 0
    for case in FIXTURE["cases"]:
        value = case_input(case)
        if (value.get("state") != "needs_user" or "human_request" not in value or "\ufeff" in json.dumps(case, ensure_ascii=False)
                or any(k in (value["human_request"] or {}) for k in ("resolved_check", "resolved_check_every"))
                or not isinstance(value.get("wait"), dict) or value["wait"].get("kind") != "human"):
            continue
        accepted = project(value, now=case.get("now", 1001)).get("unknown_reason") != "missing_human_request"
        assert accepted == needs_human(value["human_request"]), case["id"]
        checked += 1
    assert checked > 25
