"""Tests for skill content size limits.

Agent writes (create/edit/patch/write_file) are constrained to
MAX_SKILL_CONTENT_CHARS (100k) and MAX_SKILL_FILE_BYTES (1 MiB).
Hand-placed and hub-installed skills have no hard limit.
"""

import json

import pytest

from tools.skill_manager_tool import (
    MAX_SKILL_CONTENT_CHARS,
    _validate_content_size,
    skill_manage,
)


@pytest.fixture(autouse=True)
def isolate_skills(tmp_path, monkeypatch):
    """Redirect SKILLS_DIR to a temp directory."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr("tools.skill_manager_tool.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", skills_dir)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return skills_dir


def _make_skill_content(body_chars: int) -> str:
    """Generate valid SKILL.md content with a body of the given character count."""
    frontmatter = (
        "---\n"
        "name: test-skill\n"
        "description: A test skill\n"
        "---\n"
    )
    body = "# Test Skill\n\n" + ("x" * max(0, body_chars - 15))
    return frontmatter + body


class TestValidateContentSize:
    """Unit tests for _validate_content_size."""

    def test_within_limit(self):
        assert _validate_content_size("a" * 1000) is None


    def test_custom_label(self):
        err = _validate_content_size("a" * (MAX_SKILL_CONTENT_CHARS + 1), label="references/api.md")
        assert "references/api.md" in err


class TestCreateSkillSizeLimit:
    """create action rejects oversized content."""

    def test_create_within_limit(self, isolate_skills):
        content = _make_skill_content(5000)
        result = json.loads(skill_manage(action="create", name="small-skill", content=content))
        assert result["success"] is True


    def test_create_at_limit(self, isolate_skills):
        # Content at exactly the limit should succeed
        frontmatter = "---\nname: edge-skill\ndescription: Edge case\n---\n# Edge\n\n"
        body_budget = MAX_SKILL_CONTENT_CHARS - len(frontmatter)
        content = frontmatter + ("x" * body_budget)
        assert len(content) == MAX_SKILL_CONTENT_CHARS
        result = json.loads(skill_manage(action="create", name="edge-skill", content=content))
        assert result["success"] is True


class TestEditSkillSizeLimit:
    """edit action rejects oversized content."""

    def test_edit_over_limit(self, isolate_skills):
        # Create a small skill first
        small = _make_skill_content(1000)
        json.loads(skill_manage(action="create", name="grow-me", content=small))

        # Try to edit it to be oversized
        big = _make_skill_content(MAX_SKILL_CONTENT_CHARS + 100)
        # Fix the name in frontmatter
        big = big.replace("name: test-skill", "name: grow-me")
        result = json.loads(skill_manage(action="edit", name="grow-me", content=big))
        assert result["success"] is False


class TestPatchSkillSizeLimit:
    """patch action checks resulting size, not just the new_string."""

    def test_patch_that_would_exceed_limit(self, isolate_skills):
        # Create a skill near the limit
        near_limit = _make_skill_content(MAX_SKILL_CONTENT_CHARS - 50)
        json.loads(skill_manage(action="create", name="near-limit", content=near_limit))

        # Patch that adds enough to go over
        result = json.loads(skill_manage(
            action="patch",
            name="near-limit",
            old_string="# Test Skill",
            new_string="# Test Skill\n" + ("y" * 200),
        ))
        assert result["success"] is False


    def test_patch_supporting_file_size_limit(self, isolate_skills):
        """Patch on a supporting file also checks size."""
        small = _make_skill_content(1000)
        json.loads(skill_manage(action="create", name="with-ref", content=small))
        # Create a supporting file
        json.loads(skill_manage(
            action="write_file",
            name="with-ref",
            file_path="references/data.md",
            file_content="# Data\n\nSmall content.",
        ))
        # Try to patch it to be oversized
        result = json.loads(skill_manage(
            action="patch",
            name="with-ref",
            old_string="Small content.",
            new_string="x" * (MAX_SKILL_CONTENT_CHARS + 100),
            file_path="references/data.md",
        ))
        assert result["success"] is False
        assert "references/data.md" in result["error"]


class TestWriteFileSizeLimit:
    """write_file action enforces both char and byte limits."""

    def test_write_file_over_char_limit(self, isolate_skills):
        small = _make_skill_content(1000)
        json.loads(skill_manage(action="create", name="file-test", content=small))

        result = json.loads(skill_manage(
            action="write_file",
            name="file-test",
            file_path="references/huge.md",
            file_content="x" * (MAX_SKILL_CONTENT_CHARS + 1),
        ))
        assert result["success"] is False

    def test_write_file_within_limit(self, isolate_skills):
        small = _make_skill_content(1000)
        json.loads(skill_manage(action="create", name="file-ok", content=small))

        result = json.loads(skill_manage(
            action="write_file",
            name="file-ok",
            file_path="references/normal.md",
            file_content="# Normal\n\n" + ("x" * 5000),
        ))
        assert result["success"] is True


class TestHandPlacedSkillsNoLimit:
    """Skills dropped directly on disk are not constrained."""

    def test_oversized_handplaced_skill_loads(self, isolate_skills, tmp_path):
        """A hand-placed 200k skill can still be read via skill_view."""
        from tools.skills_tool import skill_view

        skill_dir = tmp_path / "skills" / "manual-giant"
        skill_dir.mkdir(parents=True)
        huge = _make_skill_content(200_000)
        huge = huge.replace("name: test-skill", "name: manual-giant")
        (skill_dir / "SKILL.md").write_text(huge, encoding="utf-8")

        result = json.loads(skill_view("manual-giant"))
        assert "content" in result
        # The full content is returned — no truncation at the storage layer
        assert len(result["content"]) > MAX_SKILL_CONTENT_CHARS


def _sectioned_skill(name: str, sections: dict[str, int]) -> str:
    body = "".join(f"## {h}\n\n" + ("z" * n) + "\n\n" for h, n in sections.items())
    return f"---\nname: {name}\ndescription: A test skill.\n---\n# T\n\n## When to Use\n\nAlways.\n\n{body}"


class TestSizePressure:
    """Regression: babybear-development grew silently to the 100k cap (the linter's oversized-body
    finding fires only on the write that first crosses 24k), then every patch failed with a bare
    'consider splitting' error. Growing writes past half the cap now warn every time, and the
    cap error names the sections to move and the in-batch order that frees room."""

    def test_growing_patch_past_half_cap_warns_with_sections(self, isolate_skills):
        content = _sectioned_skill("big", {"Huge Section": 45_000, "Small": 500, "Medium": 9_000})
        assert json.loads(skill_manage(action="create", name="big", content=content))["success"]
        r = json.loads(skill_manage(action="patch", name="big", old_string="## Small\n",
                                    new_string="## Small\n\nOne more lesson.\n"))
        assert r["success"] is True
        w = r["size_warning"]
        assert "% of the 100,000-char hard cap" in w and "'## Huge Section'" in w
        assert w.index("Huge Section") < w.index("Medium")  # largest first
        assert "references/" in w

    def test_every_growing_patch_warns_not_just_the_crossing_one(self, isolate_skills):
        content = _sectioned_skill("big2", {"A": 60_000})
        skill_manage(action="create", name="big2", content=content)
        for i in range(3):
            r = json.loads(skill_manage(action="patch", name="big2", old_string="## A\n",
                                        new_string=f"## A\nlesson {i}\n"))
            assert r["success"] and "size_warning" in r

    def test_shrinking_or_small_patch_stays_quiet(self, isolate_skills):
        content = _sectioned_skill("big3", {"A": 60_000, "B": 100})
        skill_manage(action="create", name="big3", content=content)
        r = json.loads(skill_manage(action="patch", name="big3", old_string="## B\n\n" + "z" * 100,
                                    new_string="## B\n\nshort"))  # shrinks
        assert r["success"] and "size_warning" not in r
        small = _sectioned_skill("tiny", {"A": 1_000})
        skill_manage(action="create", name="tiny", content=small)
        r = json.loads(skill_manage(action="patch", name="tiny", old_string="## A\n",
                                    new_string="## A\nmore\n"))
        assert r["success"] and "size_warning" not in r

    def test_batch_surfaces_size_warning(self, isolate_skills):
        content = _sectioned_skill("big4", {"A": 60_000})
        skill_manage(action="create", name="big4", content=content)
        r = json.loads(skill_manage(action="", name="", operations=[
            {"action": "patch", "name": "big4", "old_string": "## A\n", "new_string": "## A\nx\n"}]))
        assert r["success"] is True
        assert "size_warning" in r["results"][0]

    def test_cap_error_names_sections_and_recipe(self, isolate_skills):
        content = _sectioned_skill("full", {"Giant": 80_000, "Other": 19_000})
        assert len(content) < MAX_SKILL_CONTENT_CHARS
        skill_manage(action="create", name="full", content=content)
        r = json.loads(skill_manage(action="patch", name="full", old_string="## Other\n",
                                    new_string="## Other\n" + "y" * 2_000 + "\n"))
        assert r["success"] is False
        err = r["error"]
        assert err.startswith("SKILL.md content is")  # coach signature prefix unchanged
        assert "chars over" in err and "'## Giant'" in err and "come before the growing one" in err

    def test_shrink_then_grow_in_one_batch_succeeds_at_cap(self, isolate_skills):
        """The recipe the error prescribes actually works on a skill sitting at the cap."""
        content = _sectioned_skill("full2", {"Giant": 80_000, "Other": 19_000})
        skill_manage(action="create", name="full2", content=content)
        giant = "## Giant\n\n" + "z" * 80_000 + "\n\n"
        r = json.loads(skill_manage(action="", name="", operations=[
            {"action": "write_file", "name": "full2", "file_path": "references/giant.md",
             "file_content": giant},
            {"action": "patch", "name": "full2", "old_string": giant,
             "new_string": "## Giant\n\nSee references/giant.md.\n\n"},
            {"action": "patch", "name": "full2", "old_string": "## Other\n",
             "new_string": "## Other\n" + "y" * 2_000 + "\n"},
        ]))
        assert r["success"] is True, r

    @pytest.mark.parametrize("action", ["patch", "write_file"])
    def test_reference_growth_warns_and_shrink_stays_quiet(self, isolate_skills, action):
        skill_manage(action="create", name="ref-size", content=_make_skill_content(100))
        path = "references/triage.md"
        text = "## Large\n" + "x" * 60_000
        first = json.loads(skill_manage(action="write_file", name="ref-size",
                                       file_path=path, file_content=text))
        assert path in first["size_warning"]
        for suffix in ("more", "more again"):
            grown = text + suffix
            args = ({"old_string": text, "new_string": grown} if action == "patch"
                    else {"file_content": grown})
            result = json.loads(skill_manage(action=action, name="ref-size", file_path=path, **args))
            assert result["success"] and path in result["size_warning"]
            assert "## Large" in result["size_warning"]
            text = grown
        args = ({"old_string": text, "new_string": "small"} if action == "patch"
                else {"file_content": "small"})
        result = json.loads(skill_manage(action=action, name="ref-size", file_path=path, **args))
        assert result["success"] and "size_warning" not in result

    def test_reference_cap_recovery_batch_preserves_content(self, isolate_skills):
        name, path = "ref-recover", "references/triage.md"
        skill_manage(action="create", name=name, content=_make_skill_content(100))
        section = "## Large\n" + "x" * 80_000 + "\n"
        rest = "## Other\n" + "y" * 19_000
        original = section + rest
        skill_manage(action="write_file", name=name, file_path=path, file_content=original)
        growth = {"action": "patch", "name": name, "file_path": path,
                  "old_string": "## Other\n", "new_string": "## Other\n" + "z" * 2_000}
        failed = json.loads(skill_manage(action="", name="", operations=[
            {"action": "write_file", "name": name, "file_path": "references/rolled-back.md",
             "file_content": "must be rolled back"}, growth]))
        assert not failed["success"]
        assert "chars over" in failed["error"] and "## Large" in failed["error"]
        assert "patch that section in references/triage.md" in failed["error"]
        assert "smaller SKILL.md" not in failed["error"]
        source = isolate_skills / name / path
        assert source.read_text() == original
        assert not (source.parent / "rolled-back.md").exists()
        result = json.loads(skill_manage(action="", name="", operations=[
            {"action": "write_file", "name": name, "file_path": "references/large.md",
             "file_content": section},
            {"action": "patch", "name": name, "file_path": path,
             "old_string": section, "new_string": "See large.md.\n"}, growth]))
        assert result["success"], result
        assert (isolate_skills / name / "references/large.md").read_text() == section
        assert source.read_text() == "See large.md.\n" + rest.replace("## Other\n", growth["new_string"])


    def test_reference_equal_size_rewrite_is_quiet(self, isolate_skills):
        skill_manage(action="create", name="equal-size", content=_make_skill_content(100))
        for char in ("x", "y"):
            result = json.loads(skill_manage(action="write_file", name="equal-size",
                                            file_path="references/a.md", file_content=char * 60_000))
        assert result["success"] and "size_warning" not in result

    def test_section_sizes_ignore_fenced_headings(self):
        from tools.skill_manager_tool import _section_sizes
        text = "## Real\n```\n## fake\n```\nbody\n## Two\nx\n"
        assert [h for h, _ in _section_sizes(text)] == ["## Real", "## Two"]
