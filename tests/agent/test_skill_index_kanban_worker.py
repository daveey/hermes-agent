"""Leaner skills index for dispatcher-owned Kanban workers (``skills.index_in_kanban_worker``). [daveey]

A worker re-reads the whole system prompt on every API call; the ``triggers:`` text (user phrasings) earns
its bytes only in a chat. No mode may hide a skill name."""

import json
from types import SimpleNamespace

import pytest

from agent import prompt_builder
from agent import system_prompt
from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache
from agent.skill_utils import kanban_worker_skill_index_mode

DESC = "Use when my Mac disk is full."
TRIGGERS = ' Asks: "my disk is full". Not for: the CI runner disk.'


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    clear_skills_system_prompt_cache(clear_snapshot=True)
    _skill(tmp_path, "apple", "mac-disk-cleanup", DESC, triggers=True)
    _skill(tmp_path, "apple", "apple-notes", "Use when reading Apple Notes.")
    _skill(tmp_path, "email-triage", None, "Triage an inbox.")  # a one-skill top-level category
    yield tmp_path
    clear_skills_system_prompt_cache(clear_snapshot=True)


def _skill(home, category, name, desc, triggers=False):
    d = home / "skills" / category / name if name else home / "skills" / category
    d.mkdir(parents=True, exist_ok=True)
    trig = '\ntriggers:\n  asks:\n    - "my disk is full"\n  not_for: "the CI runner disk"' if triggers else ""
    (d / "SKILL.md").write_text(
        f"---\nname: {name or category}\ndescription: {desc}{trig}\n---\n\nBody.\n", encoding="utf-8")


def _config(home, body):
    (home / "config.yaml").write_text(f"skills:\n{body}\n", encoding="utf-8")


def _row(prompt, name="mac-disk-cleanup"):
    return next(line for line in prompt.splitlines() if name in line)


def test_full_mode_is_the_unchanged_chat_index():
    assert _row(build_skills_system_prompt()).endswith(f"- mac-disk-cleanup: {DESC}{TRIGGERS}")
    assert build_skills_system_prompt(index_mode="full") == build_skills_system_prompt()


def test_compact_mode_drops_trigger_text_but_keeps_descriptions(home):
    full = build_skills_system_prompt()
    compact = build_skills_system_prompt(index_mode="compact")
    assert _row(compact).endswith(f"- mac-disk-cleanup: {DESC}")
    assert "Asks:" not in compact and "Not for:" not in compact
    assert "- apple-notes: Use when reading Apple Notes." in compact
    assert len(compact) < len(full)
    # Cold start: the disk snapshot alone must carry what compact strips.
    clear_skills_system_prompt_cache()
    assert json.loads((home / ".skills_prompt_snapshot.json").read_text())["version"] == 7
    assert build_skills_system_prompt(index_mode="compact") == compact
    assert build_skills_system_prompt() == full  # and full still renders them (separate LRU keys)


def test_compact_strips_only_the_exact_trigger_suffix(home):
    # A description that merely mentions "Asks:" is not trigger text.
    _skill(home, "apple", "apple-notes", "Use when David Asks: notes.")
    assert "- apple-notes: Use when David Asks: notes." in build_skills_system_prompt(index_mode="compact")


def test_plugin_skill_rows_follow_the_index_mode(monkeypatch):
    # Plugin (ctx.register_skill) rows join the index after the on-disk rows are labelled: compact must
    # strip their triggers too, full keeps them, names lists the name only. No mode hides the name.
    from hermes_cli import plugins
    meta = {"name": "demo:disk-helper", "description": DESC, "frontmatter": {
        "name": "disk-helper", "description": DESC,
        "triggers": {"asks": ["my disk is full"], "not_for": "the CI runner disk"}}}
    monkeypatch.setattr(plugins, "discover_plugins", lambda *a, **k: None)
    monkeypatch.setattr(plugins, "get_plugin_manager",
                        lambda: SimpleNamespace(list_plugin_skill_metadata=lambda: [meta]))
    assert _row(build_skills_system_prompt(), "demo:disk-helper").endswith(f"- demo:disk-helper: {DESC}{TRIGGERS}")
    compact = build_skills_system_prompt(index_mode="compact")
    assert _row(compact, "demo:disk-helper").endswith(f"- demo:disk-helper: {DESC}")
    assert "Asks:" not in compact and "Not for:" not in compact
    names = build_skills_system_prompt(index_mode="names")
    assert "demo:disk-helper" in names and DESC not in names and "Asks:" not in names


def test_names_mode_lists_every_skill_name_and_no_description():
    names = build_skills_system_prompt(index_mode="names")
    for name in ("mac-disk-cleanup", "apple-notes", "email-triage"):
        assert name in names
    assert "  apple: apple-notes, mac-disk-cleanup" in names
    assert "  - email-triage\n" in names  # not "email-triage: email-triage"
    assert DESC not in names and "Asks:" not in names and "[names only]" not in names
    assert "skill_view(name)" in names and "skills_list shows descriptions" in names


@pytest.mark.parametrize(("body", "mode"), [
    ("  index_triggers: true", "compact"),  # unset -> compact
    ("  index_in_kanban_worker: names", "names"),
    ("  index_in_kanban_worker: FULL", "full"),
    ("  index_in_kanban_worker: everything", "compact"),  # unknown -> compact
])
def test_config_knob(home, body, mode):
    _config(home, body)
    assert kanban_worker_skill_index_mode() == mode


@pytest.mark.parametrize(("owned", "config", "mode"), [
    ("", "  index_in_kanban_worker: names", "full"),  # a chat never gets the lean index
    ("t_worker", "  index_triggers: true", "compact"),
    ("t_worker", "  index_in_kanban_worker: names", "names"),
])
def test_skills_prompt_picks_the_mode_from_worker_ownership(home, monkeypatch, owned, config, mode):
    _config(home, config)
    seen = {}
    monkeypatch.setattr(system_prompt, "owned_kanban_task", lambda: owned)
    monkeypatch.setattr(prompt_builder, "build_skills_system_prompt", lambda **kw: seen.update(kw) or "")
    agent = SimpleNamespace(valid_tool_names={"skill_view"}, platform="cli")
    system_prompt._skills_prompt(agent)
    assert seen["index_mode"] == mode
