"""skill_manage patch: an edit that leaves the file unchanged is a success-shaped no-op.

Agents send old_string == new_string on purpose to probe that a skill is patchable (after a split,
or after a fix to the patch path), and re-send edits that already landed. Both used to fail with
"old_string and new_string are identical" and, because operations[] is the only call shape, abort
and roll back every other op in the batch. The file patch tool already treats both as
``no_change`` successes; skill_manage now does the same.
"""
import json

import pytest

SKILL = (
    "---\nname: probe\ndescription: Use when probing no-op patches. Behavior.\n---\n"
    "# Probe\n\nEach failed connection adds about 10 s to every BB worker job.\n"
)


@pytest.fixture
def skill_md(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "skills" / "probe").mkdir(parents=True)
    (home / "skills" / "probe" / "SKILL.md").write_text(SKILL, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    import importlib

    import tools.skill_manager_tool as mod
    importlib.reload(mod)
    return home / "skills" / "probe" / "SKILL.md"


def _dispatch(ops):
    from tools.registry import registry
    return json.loads(registry.dispatch("skill_manage", {"operations": ops}))


@pytest.mark.parametrize("anchor", [
    "Each failed connection adds about 10 s to every BB worker job.",  # session 150532_ef6e95
    "---\n",  # session 180340_6b9349: short anchors are fine when nothing changes
])
def test_identical_probe_on_present_text_is_no_change_success(skill_md, anchor):
    r = _dispatch([{"action": "patch", "name": "probe", "old_string": anchor, "new_string": anchor}])
    assert r["success"] is True, r
    assert r["results"][0]["no_change"] is True
    assert "no write" in r["results"][0]["note"]
    assert skill_md.read_text(encoding="utf-8") == SKILL


def test_no_op_does_not_abort_the_rest_of_the_batch(skill_md):
    line = "Each failed connection adds about 10 s to every BB worker job."
    r = _dispatch([
        {"action": "patch", "name": "probe", "old_string": line, "new_string": line},
        {"action": "patch", "name": "probe", "old_string": "# Probe\n", "new_string": "# Probe v2\n"},
    ])
    assert r["success"] is True, r
    assert r["operations_applied"] == 2
    assert "# Probe v2\n" in skill_md.read_text(encoding="utf-8")


def test_resent_edit_that_already_landed_is_no_change(skill_md):
    op = {"action": "patch", "name": "probe", "old_string": "about 10 s to every BB worker job",
          "new_string": "about 12 s to every BB worker job"}
    assert _dispatch([dict(op)])["success"] is True
    after = skill_md.read_text(encoding="utf-8")
    r = _dispatch([dict(op)])
    assert r["success"] is True, r
    assert r["results"][0]["no_change"] is True
    assert skill_md.read_text(encoding="utf-8") == after


def test_identical_strings_absent_from_file_still_fail(skill_md):
    r = _dispatch([{"action": "patch", "name": "probe", "old_string": "not in the skill at all",
                    "new_string": "not in the skill at all"}])
    assert r["success"] is False
    assert "identical" in r["error"]


def test_background_review_summary_does_not_report_a_no_op_as_patched():
    from agent.background_review import summarize_background_review_actions
    payload = {"success": True, "operations_applied": 1, "results": [
        {"name": "probe", "action": "patch", "file_path": None, "success": True, "no_change": True}]}
    review = [
        {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "skill_manage", "arguments": json.dumps({"operations": [{"action": "patch"}]})}}]},
        {"role": "tool", "tool_call_id": "c1", "content": json.dumps(payload)},
    ]
    assert summarize_background_review_actions(review, prior_snapshot=[]) == []
