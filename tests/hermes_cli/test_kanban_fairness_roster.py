"""Fleet coverage cannot be inferred from the sources that happened to answer."""
from copy import deepcopy

import pytest

from hermes_cli import kanban_fairness_roster as roster


def source(gateway="worker-a"):
    return {"source_gateway": gateway, "profile": "default", "board_uid": "board-uid",
            "database_uid": "db-" + gateway, "process_epoch": "boot-1:pid-2:start-3",
            "core_revision": "a" * 40, "config_sha256": "b" * 64}


def evidence():
    row = source()
    snapshot = {"complete": True, "provenance": "registry-and-executor-receipt",
                "observed_ms": 100, "sources": [row], "unknown_sources": []}
    return {"version": 1, "window": {"start_ms": 100, "end_ms": 200},
            "expected_sources": [row], "opening": snapshot,
            "closing": {**deepcopy(snapshot), "observed_ms": 200},
            "membership": {"complete": True, "start_ms": 100, "end_ms": 200,
                           "provenance": "continuous-membership-journal", "events": []},
            "coverage": [{**row, "status": "ok", "provenance": "executor-receipt",
                          "start_ms": 100, "end_ms": 200,
                          "opening_cut": "epoch:0", "closing_cut": "epoch:9"}]}


def codes(value):
    return {p["code"] for p in roster.validate(value)["problems"]}


def test_complete_roster_keeps_stable_identity_and_provenance():
    result = roster.validate(evidence())
    assert result["complete"]
    assert result["sources"][0]["identity"] == ["worker-a", "default", "board-uid", "db-worker-a"]
    assert result["sources"][0]["coverage"]["provenance"] == "executor-receipt"


@pytest.mark.parametrize("section", ["opening", "closing"])
def test_omitted_source_never_becomes_empty_success(section):
    value = evidence()
    value[section]["sources"] = []
    assert "source_missing" in codes(value)


@pytest.mark.parametrize("section", ["expected_sources", "coverage", "opening", "closing"])
def test_duplicate_identity_is_rejected(section):
    value = evidence()
    rows = value[section] if isinstance(value[section], list) else value[section]["sources"]
    rows.append(deepcopy(rows[0]))
    assert "duplicate_identity" in codes(value)


def test_offline_source_remains_visible():
    value = evidence()
    value["coverage"][0]["status"] = "offline"
    result = roster.validate(value)
    assert not result["complete"]
    assert result["sources"][0]["coverage"]["status"] == "offline"
    assert "source_unavailable" in codes(value)


def test_changing_membership_and_transient_join_are_not_silently_ignored():
    value = evidence()
    value["closing"]["sources"].append(source("new-worker"))
    assert "membership_changed" in codes(value)
    assert "source_missing" in codes(value)
    value = evidence()
    value["membership"]["events"] = [{"kind": "join", "source": source("transient"), "at_ms": 150}]
    assert "membership_changed" in codes(value)


@pytest.mark.parametrize("mutation,code", [
    (lambda v: v["coverage"].clear(), "source_missing"),
    (lambda v: v["coverage"][0].update(start_ms=101), "coverage_gap"),
    (lambda v: v["coverage"][0].update(closing_cut=None), "missing_cut"),
    (lambda v: v["closing"]["sources"][0].update(core_revision="c" * 40), "source_changed"),
    (lambda v: v["opening"].update(complete=False), "discovery_incomplete"),
    (lambda v: v["membership"].update(complete=False), "membership_incomplete"),
    (lambda v: v["expected_sources"][0].update(database_uid=None), "invalid_identity"),
    (lambda v: v["coverage"][0].update(provenance=None), "missing_provenance"),
])
def test_unknown_coverage_fails_closed(mutation, code):
    value = evidence()
    mutation(value)
    assert code in codes(value)


@pytest.mark.parametrize("value", [None, [], {}, {"version": 1}, {"version": 1, "coverage": None}])
def test_incomplete_documents_return_evidence_not_tool_errors(value):
    assert not roster.validate(value)["complete"]


def test_same_gateway_different_board_profile_and_database_are_distinct():
    value = evidence()
    for key in ("board_uid", "profile", "database_uid"):
        row = {**source(), key: "another"}
        value["expected_sources"].append(row)
        value["opening"]["sources"].append(row)
        value["closing"]["sources"].append(row)
        value["coverage"].append({**value["coverage"][0], **row})
    assert roster.validate(value)["complete"]
    value["coverage"][0]["process_epoch"] = "different-process"
    assert "source_changed" in codes(value)


def test_unknown_discovery_is_retained_even_when_complete_was_claimed():
    value = evidence()
    unknown = {"source_gateway": "unreachable", "reason": "offline; profile/board unknown"}
    value["opening"]["unknown_sources"] = [unknown]
    result = roster.validate(value)
    assert not result["complete"]
    assert result["unknown_sources"]["opening"] == [unknown]


def test_cli_roster_reads_manifest_without_arming_capture(tmp_path, capsys):
    import json
    from scripts.kanban_fairness import main

    out_dir = tmp_path / "evidence"
    out_dir.mkdir()
    manifest, output = out_dir / "manifest.json", out_dir / "validation.json"
    manifest.write_text(json.dumps({"roster": evidence()}))
    args = ["inventory", "roster", "--manifest", str(manifest), "--output", str(output)]
    assert main(args) == 0
    assert json.loads(output.read_text())["complete"]
    value = evidence()
    value["coverage"] = []
    manifest.write_text(json.dumps({"roster": value}))
    assert main(args) == 2
    assert not json.loads(output.read_text())["complete"]
    assert sorted(p.name for p in out_dir.iterdir()) == ["manifest.json", "validation.json"]

