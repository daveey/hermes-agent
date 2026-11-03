"""``triggers:`` frontmatter on skill index rows (config ``skills.index_triggers``, default on)."""

import json
import re

import pytest

from agent import prompt_builder
from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache
from agent.skill_utils import extract_skill_description, render_skill_triggers

FM = {"description": "Use when the Mac disk is full. Audit, clean safely.",
      "triggers": {"asks": ["my disk is full", "free up space", "what is eating storage", "fourth"],
                   "not_for": "CI runner disk (use ci-runner-disk-recovery)."}}
ROW = ('Use when the Mac disk is full. Audit, clean safely. Asks: "my disk is full"; "free up space"; '
       '"what is eating storage". Not for: CI runner disk (use ci-runner-disk-recovery).')


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    clear_skills_system_prompt_cache(clear_snapshot=True)
    yield tmp_path
    clear_skills_system_prompt_cache(clear_snapshot=True)


def _config(home, value):
    (home / "config.yaml").write_text(f"skills:\n  index_triggers: {value}\n", encoding="utf-8")


def _skill(home, asks, not_for="the CI runner disk"):
    d = home / "skills" / "apple" / "mac-disk-cleanup"
    d.mkdir(parents=True, exist_ok=True)
    asks_yaml = "".join(f"\n    - \"{a}\"" for a in asks)
    (d / "SKILL.md").write_text(
        f"---\nname: mac-disk-cleanup\ndescription: Use when my Mac disk is full.\n"
        f"triggers:\n  asks:{asks_yaml}\n  not_for: \"{not_for}\"\n---\n\nBody.\n", encoding="utf-8")


def _row(prompt):
    return next(line for line in prompt.splitlines() if "mac-disk-cleanup" in line)


def test_triggers_render_after_the_clipped_description_by_default():
    assert extract_skill_description(FM) == ROW


@pytest.mark.parametrize("value", ["false", "'false'", "no"])
def test_config_off_hides_triggers(home, value):
    _config(home, value)
    assert extract_skill_description(FM) == "Use when the Mac disk is full. Audit, clean safely."


def test_explicit_with_triggers_overrides_config(home):
    _config(home, "false")
    assert extract_skill_description(FM, with_triggers=True) == ROW
    _config(home, "true")
    assert extract_skill_description(FM, with_triggers=False) == FM["description"]


def test_malformed_triggers_are_ignored_and_rows_stay_one_line():
    assert extract_skill_description({"description": "d.", "triggers": ["x"]}) == "d."
    assert extract_skill_description({"description": "d.", "triggers": {"not_for": "y"}}) == "d. Not for: y."
    assert render_skill_triggers({"triggers": {"asks": "one ask", "not_for": None}}) == ' Asks: "one ask".'
    assert render_skill_triggers({"triggers": {"asks": [' say "hi"\n  now ', "", 3]}}) == (
        " Asks: \"say 'hi' now\"; \"3\".")
    assert render_skill_triggers({"triggers": {"asks": {"a": 1}, "not_for": "x\ny."}}) == " Not for: x y."


def test_index_row_carries_triggers_and_a_frontmatter_edit_refreshes_it(home):
    _skill(home, ["my disk is full"])
    first = build_skills_system_prompt()
    assert _row(first).endswith(': Use when my Mac disk is full. Asks: "my disk is full". Not for: the CI runner disk.')
    assert json.loads((home / ".skills_prompt_snapshot.json").read_text())["index_triggers"] is True

    _skill(home, ["free up space"])  # same description: only triggers: changed
    assert '"free up space"' in _row(build_skills_system_prompt())  # in-process LRU missed

    clear_skills_system_prompt_cache()  # cold start: only the disk snapshot is left
    assert '"free up space"' in _row(build_skills_system_prompt())


def test_toggling_the_config_rebuilds_cache_and_snapshot(home):
    _skill(home, ["my disk is full"])
    assert "Asks:" in _row(build_skills_system_prompt())
    _config(home, "false")
    assert _row(build_skills_system_prompt()).endswith(": Use when my Mac disk is full.")  # LRU key
    snapshot = json.loads((home / ".skills_prompt_snapshot.json").read_text())
    assert snapshot["index_triggers"] is False and snapshot["version"] == prompt_builder._SKILLS_SNAPSHOT_VERSION
    _config(home, "true")
    clear_skills_system_prompt_cache()  # the disk snapshot built with triggers off must not be reused
    assert "Asks:" in _row(build_skills_system_prompt())


def test_pre_triggers_snapshot_is_rebuilt(home):
    _skill(home, ["my disk is full"])
    build_skills_system_prompt()
    path = home / ".skills_prompt_snapshot.json"
    old = json.loads(path.read_text())
    old.pop("index_triggers")
    old["version"] = 5
    for entry in old["skills"]:
        entry["description"] = re.sub(r" Asks:.*", "", entry["description"])
    path.write_text(json.dumps(old))
    clear_skills_system_prompt_cache()
    assert "Asks:" in _row(build_skills_system_prompt())
