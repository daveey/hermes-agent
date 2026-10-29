"""_safe_frontmatter memoizes path parses without hiding edits, deletions or collisions."""
from unittest.mock import patch

from tools import skills_tool as st
from tools import skills_tool_plugin as sp


def _skill(d, name, desc="x"):
    d.mkdir(parents=True, exist_ok=True)
    f = d / "SKILL.md"
    f.write_text(f"---\nname: {name}\ndescription: {desc}\n---\nbody\n")
    return f


def test_unchanged_file_parsed_once(tmp_path):
    f = _skill(tmp_path / "a", "a")
    real = st._parse_frontmatter
    with patch.object(st, "_parse_frontmatter", side_effect=real) as m:
        for _ in range(5):
            assert sp._safe_frontmatter(f)["name"] == "a"
    assert m.call_count == 1


def test_path_read_preserves_universal_newlines(tmp_path):
    for newline in (b"\r", b"\r\n", b"\n"):
        f = tmp_path / "SKILL.md"
        f.write_bytes(newline.join((b"---", b"name: x", b"description: |", b"  a", b"  b", b"---", b"body")))
        expected = st._parse_frontmatter(sp._read_skill_text(f))[0]
        assert expected["name"] == "x"
        assert sp._safe_frontmatter(f) == expected
        assert sp._safe_frontmatter(f) == expected


def test_edit_invalidates(tmp_path):
    f = _skill(tmp_path / "a", "a")
    assert sp._safe_frontmatter(f)["name"] == "a"
    f.write_text("---\nname: renamed\ndescription: x\n---\n")
    assert sp._safe_frontmatter(f)["name"] == "renamed"


def test_deletion_returns_empty(tmp_path):
    f = _skill(tmp_path / "a", "a")
    sp._safe_frontmatter(f)
    f.unlink()
    assert sp._safe_frontmatter(f) == {}


def test_result_is_private_copy(tmp_path):
    f = _skill(tmp_path / "a", "a")
    sp._safe_frontmatter(f)["name"] = "mutated"
    assert sp._safe_frontmatter(f)["name"] == "a"


def test_alias_collision_still_detected_from_cache(tmp_path):
    root = tmp_path / "skills"
    _skill(root / "one", "dup")
    _skill(root / "two", "dup")
    for _ in range(2):  # second pass runs entirely from cache
        assert len(st._collect_skill_candidates("dup", None, [root])) == 2


def test_work_bound_across_repeated_lookups(tmp_path):
    root = tmp_path / "skills"
    for i in range(20):
        _skill(root / f"s{i}", f"alias{i}")
    real = st._parse_frontmatter
    with patch.object(st, "_parse_frontmatter", side_effect=real) as m:
        for _ in range(3):
            for i in range(20):
                st._collect_skill_candidates(f"alias{i}", None, [root])
    assert m.call_count == 20


def test_symlink_retarget_mid_lookup_does_not_poison_old_target(tmp_path):
    a = _skill(tmp_path / "a", "aaa")
    b = _skill(tmp_path / "b", "bbb")
    link = tmp_path / "link.md"
    link.symlink_to(a)
    real_stat = sp.Path.stat
    calls = {"n": 0}

    def flip(self, *args, **kw):  # retarget right after the lookup stat, restore after the read
        out = real_stat(self, *args, **kw)
        if self == link and calls["n"] == 0:
            calls["n"] = 1
            link.unlink()
            link.symlink_to(b)
        return out

    with patch.object(sp.Path, "stat", flip):
        sp._safe_frontmatter(link)
    link.unlink()
    link.symlink_to(a)
    assert sp._safe_frontmatter(link)["name"] == "aaa"


def test_successive_patched_parsers_do_not_share_entries(tmp_path):
    f = _skill(tmp_path / "a", "a")
    seen = []
    for i in range(5):
        with patch.object(st, "_parse_frontmatter", lambda text, i=str(i): ({"v": i}, "")):
            seen.append(sp._safe_frontmatter(f)["v"])
    assert seen == ["0", "1", "2", "3", "4"]
