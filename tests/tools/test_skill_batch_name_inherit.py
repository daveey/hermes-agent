"""skill_manage batch: an op without 'name' inherits the one skill its sibling ops name.

Seen live: a patch on a skill plus the write_file for the reference it links, with 'name' on
only one of them, was refused ``operations[<n>] needs a 'name'`` and cost a full resend.
"""
import importlib
import json

import pytest

SK = ("---\nname: {n}\ndescription: Use when probing batch ops. Behavior.\n---\n"
      "# Probe\nStep 1.\n")


@pytest.fixture
def smt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    (tmp_path / "skills").mkdir()
    import tools.skill_manager_tool as mod
    importlib.reload(mod)
    for n in ("probe", "other"):
        r = json.loads(mod.skill_manage(action="", name="", operations=[
            {"action": "create", "name": n, "content": SK.format(n=n)}]))
        assert r["success"], r
    return mod, tmp_path / "skills"


def _run(mod, ops, name=""):
    return json.loads(mod.skill_manage(action="", name=name, operations=ops))


WRITE = {"action": "write_file", "file_path": "references/a.md", "file_content": "a"}
PATCH = {"action": "patch", "old_string": "Step 1.", "new_string": "Step 1. See references/a.md."}


@pytest.mark.parametrize("ops", [
    [dict(WRITE), dict(PATCH, name="probe")],   # live sample: operations[0] nameless
    [dict(PATCH, name="probe"), dict(WRITE)],   # live sample: operations[1] nameless
], ids=["nameless-first", "nameless-second"])
def test_nameless_op_inherits_the_single_named_skill(smt, ops):
    mod, skills = smt
    r = _run(mod, ops)
    assert r["success"], r
    assert [row["name"] for row in r["results"]] == ["probe", "probe"]
    flagged = [bool(row.get("name_inherited")) for row in r["results"]]
    assert flagged == [not op.get("name") for op in ops]
    assert (skills / "probe" / "references" / "a.md").read_text() == "a"
    assert "See references/a.md." in (skills / "probe" / "SKILL.md").read_text()
    assert not (skills / "other" / "references").exists()


def test_nameless_op_among_several_skills_is_refused_and_nothing_is_written(smt):
    mod, skills = smt
    r = _run(mod, [dict(PATCH, name="probe"), dict(WRITE),
                   {"action": "write_file", "name": "other", "file_path": "references/b.md",
                    "file_content": "b"}])
    assert r["success"] is False
    assert r["error"].startswith("operations[1] needs a 'name' (the skill it targets)")
    assert "other, probe" in r["error"]
    assert "Step 1.\n" in (skills / "probe" / "SKILL.md").read_text()
    assert not (skills / "probe" / "references").exists()
    assert not (skills / "other" / "references").exists()


def test_batch_with_no_name_anywhere_is_still_refused(smt):
    mod, skills = smt
    r = _run(mod, [dict(WRITE), dict(PATCH)])
    assert r["success"] is False
    assert r["error"].startswith("operations[0] needs a 'name' (the skill it targets).")
    assert not (skills / "probe" / "references").exists()


def test_non_string_name_is_not_a_target(smt):
    mod, skills = smt
    r = _run(mod, [dict(WRITE), dict(PATCH, name=["probe"])])
    assert r["success"] is False, r
    assert not (skills / "probe" / "references").exists()


def test_top_level_name_still_wins_and_is_not_flagged_inherited(smt):
    mod, skills = smt
    r = _run(mod, [dict(WRITE), dict(PATCH, name="probe")], name="other")
    assert r["success"], r
    assert [row["name"] for row in r["results"]] == ["other", "probe"]
    assert not any("name_inherited" in row for row in r["results"])
    assert (skills / "other" / "references" / "a.md").exists()


def test_staged_batch_carries_the_inherited_name_for_the_approval_diff(smt, monkeypatch):
    mod, skills = smt
    import tools.write_approval as wa
    importlib.reload(wa)
    staged = {}
    real_gate = mod._run_write_gate

    def gate(build_staging):
        staged["payload"], staged["gist"] = build_staging(wa)
        return json.dumps({"success": True, "staged": True})

    monkeypatch.setattr(mod, "_run_write_gate", gate)
    r = _run(mod, [dict(WRITE), dict(PATCH, name="probe")])
    assert r.get("staged") is True, r
    assert [op["name"] for op in staged["payload"]["operations"]] == ["probe", "probe"]
    assert not (skills / "probe" / "references").exists()  # staged, not applied
    diff = wa.skill_pending_diff({"payload": staged["payload"]})
    assert "+Step 1. See references/a.md." in diff
    monkeypatch.setattr(mod, "_run_write_gate", real_gate)
    replay = json.loads(mod.apply_skill_pending(staged["payload"]))
    assert replay["success"], replay
    assert (skills / "probe" / "references" / "a.md").read_text() == "a"


def test_empty_name_counts_as_nameless_not_as_a_second_target(smt):
    mod, skills = smt
    r = _run(mod, [dict(WRITE, name=""), dict(PATCH, name="probe")])
    assert r["success"], r
    assert (skills / "probe" / "references" / "a.md").exists()


def test_fully_named_batch_rows_are_unchanged(smt):
    mod, _ = smt
    r = _run(mod, [dict(WRITE, name="probe"), dict(PATCH, name="probe")])
    assert r["success"], r
    assert not any("name_inherited" in row for row in r["results"])
