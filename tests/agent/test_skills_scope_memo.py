"""[daveey] Card J (t_01a1d563324d7e8e): ``skills.changed`` walks each skill root once per watch pass.

``published_scopes_signature`` signs the ambient scope plus every profile/repo scope a session resolved commands
in. Most scopes share their roots (the install's bundled and external skill dirs), and each scope walked them
again: on the hub that was most of the ``skills.changed`` watch. The walk of a root is now shared within one call;
a change under any root still moves the next call's signature.
"""
import os

import pytest

from agent import skill_commands
from tools import skills_tool


@pytest.fixture
def scopes(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home
    shared = tmp_path / "shared-skills"
    (shared / "alpha").mkdir(parents=True)
    homes = [tmp_path / f"home{n}" for n in range(3)]
    for home in homes:
        (home / "skills").mkdir(parents=True)
    monkeypatch.setattr(skills_tool, "skill_search_dirs",
                        lambda: ([("external", shared), ("profile", get_hermes_home() / "skills")], None))
    monkeypatch.setattr(skills_tool, "_get_disabled_skill_names", lambda: set())
    monkeypatch.setattr(skill_commands, "_skill_commands_by_key",
                        {("tui", str(home), None): {} for home in homes})
    walks: list = []
    real = skills_tool._dir_tree_signature

    def counted(root, sig, *args, **kwargs):
        walks.append(str(root))
        return real(root, sig, *args, **kwargs)

    monkeypatch.setattr(skills_tool, "_dir_tree_signature", counted)
    return shared, homes, walks


def test_a_shared_root_is_walked_once_per_call(scopes):
    shared, homes, walks = scopes
    skill_commands.published_scopes_signature()
    assert walks.count(str(shared)) == 1, walks
    for home in homes:
        assert walks.count(str(home / "skills")) == 1


def test_a_change_under_a_shared_or_scoped_root_moves_the_next_signature(scopes):
    shared, homes, _walks = scopes
    before = skill_commands.published_scopes_signature()
    (shared / "beta").mkdir()
    moved = skill_commands.published_scopes_signature()
    assert moved != before
    (homes[2] / "skills" / "gamma").mkdir()
    assert skill_commands.published_scopes_signature() != moved
    os.rmdir(homes[2] / "skills" / "gamma")
    os.rmdir(shared / "beta")


def test_scope_rows_are_unchanged_by_the_memo(scopes):
    """Each scope's row is the same signature an unshared walk gives."""
    shared, homes, _walks = scopes
    rows = skill_commands.published_scopes_signature()
    fresh = skills_tool._skills_scan_signature([shared, homes[0] / "skills"], set())
    assert any(row[2] == fresh for row in rows if row[0] == str(homes[0]))
