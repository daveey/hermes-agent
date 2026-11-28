"""Missing reference paths may denote an inline skill section, not a real file."""
import json

import pytest

from tools import skills_tool as st
from tools import skills_tool_plugin as sp


@pytest.fixture
def skill(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = home / "skills" / "inline-guide"
    root.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    (root / "SKILL.md").write_text(
        "---\nname: inline-guide\ndescription: An inline guide.\n---\n"
        "# Guide\n\n**Card mechanics for this user**. Keep every dependency.\n"
        "\n## Other guidance\nRetain the full instructions.\n"
    )
    return root


def test_missing_reference_recovers_inline_label_through_registered_handler(skill):
    result = json.loads(st._skill_view_with_bump(
        {"name": "inline-guide", "file_path": "references/card-mechanics.md"}))
    assert result["success"] is True
    assert result["file"] == "SKILL.md"
    assert result["requested_file"] == "references/card-mechanics.md"
    assert result["inline_section"] == "Card mechanics for this user"
    assert result["content"] == (skill / "SKILL.md").read_text()


@pytest.mark.parametrize("label", ["## Card mechanics", "**Card mechanics**. Use links."])
def test_shared_plugin_file_server_recovers_exact_section(skill, label):
    (skill / "SKILL.md").write_text("# Guide\n" + label + "\nPreserve this.\n")
    result = json.loads(sp._serve_skill_file(skill, "references/card-mechanics.md", "addon:guide"))
    assert result["success"] is True
    assert result["name"] == "addon:guide"
    assert result["inline_section"] == "Card mechanics"


def test_plugin_registry_dispatch_recovers_inline_section(skill, monkeypatch):
    from hermes_cli import plugins

    manager = plugins.PluginManager()
    manager._plugin_skills["addon:guide"] = {
        "path": skill / "SKILL.md", "plugin": "addon", "bare_name": "guide", "description": "Guide"}
    monkeypatch.setattr(plugins, "_plugin_manager", manager)
    result = json.loads(st._skill_view_with_bump(
        {"name": "addon:guide", "file_path": "references/card-mechanics.md"}))
    assert result["success"] is True
    assert result["name"] == "addon:guide"
    assert result["file"] == "SKILL.md"
    assert result["content"] == (skill / "SKILL.md").read_text()


def test_existing_reference_wins(skill):
    target = skill / "references" / "card-mechanics.md"
    target.parent.mkdir()
    target.write_text("Real reference.")
    result = json.loads(st.skill_view("inline-guide", "references/card-mechanics.md"))
    assert result["file"] == "references/card-mechanics.md"
    assert result["content"] == "Real reference."
    assert "requested_file" not in result


@pytest.mark.parametrize("body", [
    "# Guide\nUnrelated text.",
    "## Card mechanics\nA\n## Card mechanics for teams\nB",
    "```md\n## Card mechanics\n```\n",
    "~~~md\n**Card mechanics**. Example only.\n~~~\n",
    "---\ndescription: |\n  ## Card mechanics\n---\n# Guide\n",
    "Some prose mentions **Card mechanics** but defines no section.",
    "## Card mechanics-extra\nNot an exact section or qualifier.",
])
def test_absent_ambiguous_or_example_labels_remain_errors(skill, body):
    (skill / "SKILL.md").write_text(body)
    result = json.loads(st.skill_view("inline-guide", "references/card-mechanics.md"))
    assert result["success"] is False
    assert "not found" in result["error"]


@pytest.mark.parametrize("path", [
    "../references/card-mechanics.md", "references/../card-mechanics.md",
    "assets/card-mechanics.md", "references/card-mechanics.py",
    "references/nested/card-mechanics.md", "references/card.md",
])
def test_recovery_is_restricted_to_reference_slug_paths(skill, path):
    result = json.loads(sp._serve_skill_file(skill, path, "guide"))
    assert result["success"] is False


def test_directory_is_not_an_inline_reference(skill):
    (skill / "references" / "card-mechanics.md").mkdir(parents=True)
    result = json.loads(sp._serve_skill_file(skill, "references/card-mechanics.md", "guide"))
    assert result["success"] is False


def test_recovery_marks_actual_source_as_read(skill, monkeypatch):
    reads = []
    monkeypatch.setattr(sp, "_mark_background_review_read", reads.append)
    result = json.loads(sp._serve_skill_file(
        skill, "references/card-mechanics.md", "guide", mark_read=True))
    assert result["success"] is True
    assert reads == [skill / "SKILL.md"]


def test_recovery_read_error_preserves_original_missing_file_error(skill, monkeypatch):
    def denied(path):
        raise PermissionError("denied")

    monkeypatch.setattr(sp, "_read_skill_text", denied)
    result = json.loads(sp._serve_skill_file(skill, "references/card-mechanics.md", "guide"))
    assert result["success"] is False
    assert "not found" in result["error"]


def test_recovery_does_not_read_outside_skill(skill, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("## Card mechanics\nSecret instructions.")
    (skill / "SKILL.md").unlink()
    (skill / "SKILL.md").symlink_to(outside)
    result = json.loads(sp._serve_skill_file(skill, "references/card-mechanics.md", "guide"))
    assert result["success"] is False
    assert "Secret instructions" not in json.dumps(result)
