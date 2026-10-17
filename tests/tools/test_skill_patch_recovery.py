"""Stale anchors must recover without overwriting concurrent skill additions."""
import importlib
import json

import pytest

PREFIX = "See references/integrations.md for per-integration notes "
CURRENT = PREFIX + "(Hatch, Eight Sleep, Porsche Connect, Comelit intercom: its local web admin, backup/restore and face recognition)."
BODY = "---\nname: probe\ndescription: Use when testing patch recovery.\n---\n# Probe\n" + ("Context.\n" * 90) + CURRENT + "\n"


@pytest.fixture
def skill(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    root = home / "skills" / "probe"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(BODY)
    (root / "references").mkdir()
    (root / "references" / "notes.md").write_text(BODY)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    import tools.skill_manager_tool as mod
    importlib.reload(mod)
    return root


def dispatch(ops):
    from tools.registry import registry
    return json.loads(registry.dispatch("skill_manage", {"operations": ops}))


@pytest.mark.parametrize("old", [PREFIX + "(Hatch).", PREFIX + "(Hatch, Eight Sleep, Porsche Connect)."])
@pytest.mark.parametrize("file_path", [None, "references/notes.md"])
def test_stale_anchor_reports_reload_and_whole_batch_retry(skill, old, file_path):
    first = {"action": "write_file", "name": "probe", "file_path": "references/new.md", "file_content": "New lesson.\n"}
    edit = {"action": "patch", "name": "probe", "old_string": old, "new_string": old + "\nNew lesson."}
    if file_path:
        edit["file_path"] = file_path
    result = dispatch([first, edit])
    assert result["success"] is False
    assert not (skill / "references/new.md").exists()
    target = skill / (file_path or "SKILL.md")
    assert target.read_text() == BODY
    recovery = result["recovery"]
    assert recovery["tool"] == "skill_view"
    assert recovery["arguments"] == ({"name": "probe", "file_path": file_path} if file_path else {"name": "probe"})
    assert "preserve" in recovery["instruction"]
    assert result["rollback_complete"] is True
    assert result["operations_applied"] == 0
    assert "entire batch" in result["retry_instruction"]
    # Recovery uses the current anchor, preserves the concurrent addition, and retries BOTH ops.
    edit.update(old_string=CURRENT, new_string=CURRENT + "\nNew lesson.")
    assert dispatch([first, edit])["success"] is True
    assert (skill / "references/new.md").read_text() == "New lesson.\n"
    assert target.read_text() == BODY.replace(CURRENT, CURRENT + "\nNew lesson.")


def test_failed_rollback_does_not_recommend_blind_batch_retry(skill, monkeypatch):
    import tools.skill_manager_batch as batch
    monkeypatch.setattr(batch, "_rollback", lambda *args: ("rollback failed", True))
    result = dispatch([{"action": "patch", "name": "probe", "old_string": "absent anchor",
                        "new_string": "new lesson"}])
    assert result["success"] is False
    assert result["rollback_complete"] is False
    assert "operations_applied" not in result
    assert "inspect every affected file" in result["retry_instruction"]


def test_ambiguous_anchor_requires_unique_fresh_anchor(skill):
    result = dispatch([{"action": "patch", "name": "probe", "old_string": "Context.",
                        "new_string": "Updated context."}])
    assert result["success"] is False
    assert "unique anchor" in result["recovery"]["instruction"]
    assert (skill / "SKILL.md").read_text() == BODY
