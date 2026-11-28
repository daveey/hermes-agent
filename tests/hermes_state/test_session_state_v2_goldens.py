"""P0 cross-consumer goldens from the single session-state-v2 scenario source.

The snapshots are a requested wire contract, not a catalog/config change detector.
All classification runs in production collectors; only source IO is substituted.
"""
import json

import pytest

from tests.hermes_state.session_state_v2_fixtures import (
    FIXTURES, load_scenarios, main, materialize_case, run_all, run_case,
)

DATA = load_scenarios()
SCENARIOS = DATA["scenarios"]


@pytest.fixture(scope="module")
def collected():
    return {case["id"]: case for case in run_all()["cases"]}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda case: case["id"])
def test_published_core_golden(scenario, collected):
    goldens = json.loads((FIXTURES / "core-goldens.json").read_text())
    expected = {case["id"]: case for case in goldens["cases"]}
    assert collected[scenario["id"]] == expected[scenario["id"]]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda case: case["id"])
def test_collectors_are_deterministic_and_keep_qualified_source_contract(scenario, collected):
    actual = collected[scenario["id"]]
    assert run_case(scenario) == actual
    status = actual["outcome"]["status"]
    assert status["identity"] == {**DATA["identity"], "owner_epoch": "lineage:" + DATA["identity"]["session_id"]}
    assert status["generation"] == 1
    assert set(status["source_revisions"]) == {"cards", "controls", "turn", "subagents", "processes", "goals"}
    assert actual["session_row"]["outcome"] == actual["outcome"]
    assert actual["input"] == scenario
    assert status["coverage"]["state"] == ("unknown" if scenario["id"] == "stale_replica" else "complete")
    if scenario.get("runtime"):
        assert status["kind"] != "done"
        assert any(item["kind"] == scenario["runtime"]["kind"] for item in status["on"])
    if (scenario.get("card") or {}).get("awaiting"):
        assert status["kind"] == "waiting"
        assert not actual["session_row"].get("needs_you")


def test_every_shared_scenario_has_exactly_one_golden(collected):
    golden = json.loads((FIXTURES / "core-goldens.json").read_text())
    golden_ids = [case["id"] for case in golden["cases"]]
    source_ids = [case["id"] for case in SCENARIOS]
    assert len(set(source_ids)) == len(source_ids)
    assert golden_ids == source_ids
    assert set(collected) == set(source_ids)
    assert golden["schema_version"] == DATA["schema_version"]
    assert golden["now"] == DATA["now"]


@pytest.mark.parametrize("case_id", ["unblocked_after_block", "reblocked"])
def test_lifecycle_is_persisted_and_each_episode_changes_revision(case_id, collected):
    case = collected[case_id]
    episodes = case["input"]["episodes"]
    assert [out["status"]["kind"] for out in case["history"]] == [
        "decision" if phase == "blocked" else "waiting" for phase in episodes]
    assert [out["status"]["revision"] for out in case["history"]] == list(range(1, len(episodes) + 1))
    assert case["cards"][0]["activity_kind"] == episodes[-1]
    assert bool(case["session_row"].get("needs_you")) == (episodes[-1] == "blocked")


def test_triage_strict_boundary_is_operational_attention_not_human_ask(collected):
    for scenario in SCENARIOS:
        if "triage_age_s" not in scenario:
            continue
        case = collected[scenario["id"]]
        overdue = scenario["triage_age_s"] > 600
        assert case["outcome"]["status"]["presentation"]["stripe"] == ("stuck" if overdue else "waiting")
        assert bool(case["session_row"].get("kanban_triage_attention")) == overdue
        assert not case["session_row"].get("needs_you")
        assert case["cards"][0]["triage_entered_at"] == DATA["now"] - scenario["triage_age_s"]


def test_producer_wait_is_not_card_execution(collected):
    case = collected["waiting_card"]
    assert case["cards"][0]["wait"]["producer_wait"]["id"] == case["input"]["producer"]["id"]
    status = case["outcome"]["status"]
    assert status["presentation"]["stripe"] == "external"
    assert status["on"][0]["state"] == "producer_wait"


def test_human_brief_reference_uses_shared_input_and_unbriefed_ask_is_still_human(collected):
    brief = collected["human_brief"]
    assert collected["reblocked"]["card"]["human_request"] == brief["card"]["human_request"]
    for case_id in ("human_brief", "human_no_brief", "reblocked"):
        case = collected[case_id]
        assert case["outcome"]["status"]["kind"] == "decision"
        assert case["session_row"]["kanban_blocked"] == len(case["session_row"]["needs_you"]) == 1
    assert collected["done"]["outcome"]["status"]["kind"] == "done"
    assert collected["turn_failed"]["outcome"]["status"]["presentation"]["stripe"] == "attention"


def test_cli_stdout_and_output_file_are_same_consumer_contract(monkeypatch, capsys, tmp_path, collected):
    monkeypatch.setattr("sys.argv", ["session_state_v2_fixtures", "--case", "waiting_card"])
    main()
    actual = json.loads(capsys.readouterr().out)
    assert actual["cases"] == [collected["waiting_card"]]
    path = tmp_path / "consumer.json"
    monkeypatch.setattr("sys.argv", ["session_state_v2_fixtures", "--case", "waiting_card", "--output", str(path)])
    main()
    assert json.loads(path.read_text()) == actual
    assert capsys.readouterr().out == ""


def test_unknown_case_is_rejected_and_materialization_does_not_mutate_source():
    original = json.loads(json.dumps(DATA))
    for scenario in SCENARIOS:
        materialize_case(scenario, DATA)
    assert DATA == original
    with pytest.raises(ValueError, match="unknown scenario"):
        run_all("not-a-fixture")
