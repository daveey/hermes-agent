"""Catalog refresh rechecks policy and bytes without reparsing unchanged YAML."""
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent import skill_utils
from tools import skills_tool as st


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(st, "skill_search_dirs", lambda: ([(1, root)], root))
    monkeypatch.setattr(st, "_get_category_from_path", lambda p: None)
    monkeypatch.setattr(st, "_get_disabled_skill_names", lambda: set())
    monkeypatch.setattr(st, "skill_matches_platform", lambda fm: True)
    monkeypatch.setattr(st, "skill_matches_environment", lambda fm: True)
    monkeypatch.setattr(st, "skill_matches_apps", lambda fm: True)
    st.clear_skills_cache()
    yield root
    st.clear_skills_cache()


def put(root: Path, name: str, description: str):
    folder = root / name
    folder.mkdir(exist_ok=True)
    path = folder / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\nBody\n", encoding="utf-8")
    return path


def test_expired_catalog_reuses_only_unchanged_parse(inventory, monkeypatch):
    path = put(inventory, "alpha", "first")
    parser = Mock(wraps=skill_utils.yaml_load)
    monkeypatch.setattr(skill_utils, "yaml_load", parser)
    assert st.find_all_skills()[0]["description"] == "first"
    assert parser.call_count == 1
    st.clear_skills_cache()  # same parsing work as TTL expiry
    assert st.find_all_skills()[0]["description"] == "first"
    assert parser.call_count == 1
    path.write_text(path.read_text().replace("first", "other"), encoding="utf-8")
    st.clear_skills_cache()
    assert st.find_all_skills()[0]["description"] == "other"
    assert parser.call_count == 2
    path.unlink()
    st.clear_skills_cache()
    assert st.find_all_skills() == []


def test_due_refresh_rechecks_policy_and_parser(inventory, monkeypatch):
    put(inventory, "alpha", "first")
    assert st.find_all_skills()
    monkeypatch.setattr(st, "skill_matches_environment", lambda fm: False)
    st.clear_skills_cache()
    assert st.find_all_skills() == []
    monkeypatch.setattr(st, "skill_matches_environment", lambda fm: True)
    monkeypatch.setattr(st, "_parse_frontmatter", lambda text: ({"name": "beta", "description": "new parser"}, ""))
    st.clear_skills_cache()
    assert st.find_all_skills()[0]["name"] == "beta"


def test_body_fallback_and_nested_values_are_private(inventory, monkeypatch):
    from tools.skill_catalog_parse import parse_catalog_text
    text = "---\nname: sample\nmetadata:\n  tags: [original]\n---\n# Heading\nFallback body\n"
    first, body = parse_catalog_text(text)
    first["metadata"]["tags"].append("mutation")
    second, again = parse_catalog_text(text)
    assert second["metadata"]["tags"] == ["original"]
    assert again == body
    folder = inventory / "sample"
    folder.mkdir()
    (folder / "SKILL.md").write_text(text, encoding="utf-8")
    assert st.find_all_skills()[0]["description"] == "Fallback body"


def test_underlying_custom_parser_is_never_memoized(monkeypatch):
    from tools.skill_catalog_parse import parse_catalog_text
    parser = Mock(side_effect=[({"name": "one"}, ""), ({"name": "two"}, "")])
    monkeypatch.setattr(skill_utils, "parse_frontmatter", parser)
    assert parse_catalog_text("same")[0]["name"] == "one"
    assert parse_catalog_text("same")[0]["name"] == "two"


def test_real_ttl_preserves_same_stat_edit_deadline(inventory, monkeypatch):
    import os
    from types import SimpleNamespace
    path = put(inventory, "alpha", "first")
    now = [100.0]
    monkeypatch.setattr(st, "time", SimpleNamespace(monotonic=lambda: now[0]))
    assert st.find_all_skills()[0]["description"] == "first"
    stat = path.stat()
    path.write_text(path.read_text().replace("first", "other"), encoding="utf-8")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    now[0] += st._SKILLS_CACHE_TTL_SECONDS - 1
    assert st.find_all_skills()[0]["description"] == "first"
    now[0] += 1
    assert st.find_all_skills()[0]["description"] == "other"


def test_parse_retention_is_bounded_and_errors_retry(monkeypatch):
    from tools.skill_catalog_parse import _parse, parse_catalog_text
    _parse.cache_clear()
    for index in range(513):
        assert parse_catalog_text(f"---\nname: skill-{index}\n---\n")[0]["name"] == f"skill-{index}"
    assert _parse.cache_info().currsize == 512
    parser = Mock(side_effect=[ValueError("broken"), ({"name": "recovered"}, "")])
    monkeypatch.setattr(st, "_parse_frontmatter", parser)
    with pytest.raises(ValueError, match="broken"):
        parse_catalog_text("---\nname: recovery\n---\n")
    assert parse_catalog_text("---\nname: recovery\n---\n")[0]["name"] == "recovered"


@pytest.mark.parametrize("text", [
    "plain body", "\ufeff---\nname: bom\n---\nBody", "---\ndescription: null\n---\nBody",
    "---\nname: [broken\ndescription: fallback\n---\nBody",
    "---\nname: sample\nmetadata: &anchor {tags: [one]}\nalias: *anchor\n---\nBody",
])
def test_parse_matches_literal_including_fallback(text):
    from tools.skill_catalog_parse import parse_catalog_text
    expected = st._parse_frontmatter(text)
    assert parse_catalog_text(text) == expected
    assert parse_catalog_text(text) == expected
