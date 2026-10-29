"""The cross-process SKILL.md frontmatter index returns exactly what a fresh parse returns. [daveey]"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tools import skill_frontmatter_index as idx
from tools import skills_tool_plugin as stp

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    idx._reset_for_tests()
    stp._FRONTMATTER_CACHE.clear()
    yield h
    idx._reset_for_tests()
    stp._FRONTMATTER_CACHE.clear()


def _skill(root: Path, name: str, extra: str = "") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    md = d / "SKILL.md"
    md.write_text(f"---\nname: {name}\ndescription: d\n{extra}---\nbody\n")
    return md


def _new_process(home):
    """Forget everything in memory, as a fresh process would (the index file stays)."""
    idx.flush()
    idx._reset_for_tests()
    stp._FRONTMATTER_CACHE.clear()


def _parses(monkeypatch):
    """Count YAML loads behind the stock parser (patching yaml_load's loader keeps the token)."""
    from agent import skill_utils
    from hermes_yaml import safe_load
    calls = []

    def counting(text):
        calls.append(1)
        return safe_load(text)
    monkeypatch.setattr(skill_utils, "_yaml_load_fn", counting)
    return calls


def test_warm_index_serves_a_new_process_without_parsing(home, tmp_path, monkeypatch):
    md = _skill(tmp_path / "skills", "alpha", "metadata:\n  hermes:\n    requires_toolsets: [terminal]\n")
    calls = _parses(monkeypatch)
    first = stp._safe_frontmatter(md)
    assert len(calls) == 1
    _new_process(home)
    again = stp._safe_frontmatter(md)
    assert again == first and len(calls) == 1
    assert oct((home / "cache" / "skill_frontmatter_index.json").stat().st_mode & 0o777) == "0o600"
    again["name"] = "mutated"  # a private object
    assert stp._safe_frontmatter(md)["name"] == "alpha"


def test_edit_and_same_size_rewrite_are_reparsed(home, tmp_path, monkeypatch):
    md = _skill(tmp_path / "skills", "alpha")
    assert stp._safe_frontmatter(md)["name"] == "alpha"
    _new_process(home)
    md.write_text(md.read_text().replace("alpha", "gamma"))  # same size, new mtime/ctime
    st = md.stat()
    os.utime(md, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert stp._safe_frontmatter(md)["name"] == "gamma"


def test_deleted_file_is_not_served(home, tmp_path):
    md = _skill(tmp_path / "skills", "alpha")
    stp._safe_frontmatter(md)
    _new_process(home)
    md.unlink()
    assert stp._safe_frontmatter(md) == {}


def test_symlink_retarget_reads_the_new_target(home, tmp_path):
    a = _skill(tmp_path / "a", "first")
    b = _skill(tmp_path / "b", "second")
    link = tmp_path / "link.md"
    link.symlink_to(a)
    assert stp._safe_frontmatter(link)["name"] == "first"
    _new_process(home)
    link.unlink()
    link.symlink_to(b)
    assert stp._safe_frontmatter(link)["name"] == "second"


def test_aliases_of_one_file_agree(home, tmp_path):
    md = _skill(tmp_path / "skills", "alpha")
    alias = tmp_path / "alias.md"
    alias.symlink_to(md)
    assert stp._safe_frontmatter(md) == stp._safe_frontmatter(alias)
    _new_process(home)
    md.write_text(md.read_text().replace("description: d", "description: changed"))
    assert stp._safe_frontmatter(alias)["description"] == "changed"
    assert stp._safe_frontmatter(md)["description"] == "changed"


@pytest.mark.parametrize("blob", ["", "{oops", "[]", json.dumps({"version": 1, "token": "x", "entries": {}}),
                                  json.dumps({"version": 99, "entries": {}})])
def test_corrupt_or_foreign_index_is_ignored(home, tmp_path, blob):
    md = _skill(tmp_path / "skills", "alpha")
    (home / "cache").mkdir()
    (home / "cache" / "skill_frontmatter_index.json").write_text(blob)
    assert stp._safe_frontmatter(md)["name"] == "alpha"
    idx.flush()
    data = json.loads((home / "cache" / "skill_frontmatter_index.json").read_text())
    assert data["token"] == idx.parser_token() and str(md) in data["entries"]


def test_tampered_entry_with_other_identity_is_ignored(home, tmp_path):
    md = _skill(tmp_path / "skills", "alpha")
    st = md.stat()
    (home / "cache").mkdir()
    entry = [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns - 1, st.st_ctime_ns, {"name": "forged"}]
    (home / "cache" / "skill_frontmatter_index.json").write_text(json.dumps(
        {"version": idx.INDEX_VERSION, "token": idx.parser_token(), "entries": {str(md): entry}}))
    assert stp._safe_frontmatter(md)["name"] == "alpha"


def test_patched_parser_bypasses_the_index(home, tmp_path, monkeypatch):
    from tools import skills_tool
    md = _skill(tmp_path / "skills", "alpha")
    stp._safe_frontmatter(md)
    _new_process(home)
    monkeypatch.setattr(skills_tool, "_parse_frontmatter", lambda text: ({"name": "patched"}, ""))
    assert stp._safe_frontmatter(md) == {"name": "patched"}
    monkeypatch.undo()
    _new_process(home)
    from agent import skill_utils
    monkeypatch.setattr(skill_utils, "parse_frontmatter", lambda text: ({"name": "deep"}, ""))
    assert stp._safe_frontmatter(md) == {"name": "deep"}


def test_patched_yaml_loader_bypasses_the_index(home, tmp_path, monkeypatch):
    """A YAML loader rebound in this process is not what the token names: parse fresh."""
    from agent import skill_utils
    md = _skill(tmp_path / "skills", "alpha")
    stp._safe_frontmatter(md)
    _new_process(home)
    monkeypatch.setattr(skill_utils, "yaml_load", lambda content: {"name": "yaml-patched"})
    assert stp._safe_frontmatter(md) == {"name": "yaml-patched"}


def test_parser_code_change_discards_the_index(home, tmp_path, monkeypatch):
    md = _skill(tmp_path / "skills", "alpha")
    stp._safe_frontmatter(md)
    _new_process(home)
    monkeypatch.setattr(idx, "parser_token", lambda: "another-parser")
    calls = _parses(monkeypatch)
    assert stp._safe_frontmatter(md)["name"] == "alpha" and len(calls) == 1


def test_values_json_cannot_hold_exactly_are_always_parsed_fresh(home, tmp_path, monkeypatch):
    md = _skill(tmp_path / "skills", "alpha", "created: 2024-01-02\n")  # YAML date
    first = stp._safe_frontmatter(md)
    assert type(first["created"]).__name__ == "date"
    _new_process(home)
    calls = _parses(monkeypatch)
    assert stp._safe_frontmatter(md) == first and len(calls) == 1


def test_content_parses_never_touch_the_index(home):
    assert stp._safe_frontmatter(content="---\nname: x\n---\n") == {"name": "x"}
    idx.flush()
    assert not (home / "cache" / "skill_frontmatter_index.json").exists()


def test_cold_process_skill_lookup_reads_no_skill_frontmatter_twice(tmp_path):
    """Work bound across processes: a second cold `_locate_skill` over 40 skills parses none."""
    home = tmp_path / "hermes"
    skills = home / "skills"
    for i in range(40):
        _skill(skills / "cat", f"s{i:02d}")
    script = textwrap.dedent("""
        import json
        from agent import skill_utils
        from hermes_yaml import safe_load
        n = []
        skill_utils._yaml_load_fn = lambda t: (n.append(1), safe_load(t))[1]
        from tools.skills_tool import _locate_skill, _skill_search_dirs
        roots, _ = _skill_search_dirs()
        err, d, md = _locate_skill("s07", None, roots)
        print("RESULT " + json.dumps({"err": err, "md": str(md), "parses": len(n)}))
    """)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "PYTEST"))}
    # The runner may provide dependencies through sys.path rather than site-packages.
    env.update(HERMES_HOME=str(home), HOME=str(tmp_path),
               PYTHONPATH=os.pathsep.join([str(REPO), *(os.path.abspath(p) for p in sys.path if p)]))

    def run():
        p = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120, check=False)
        line = next(ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")) if "RESULT " in p.stdout else None
        assert line, p.stderr[-3000:]
        return json.loads(line[7:])
    cold = run()
    warm = run()
    assert cold["md"].endswith("s07/SKILL.md") and warm == {**cold, "parses": 0}
    assert cold["parses"] >= 39  # every other SKILL.md is parsed for its frontmatter name
