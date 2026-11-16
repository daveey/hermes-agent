"""Full catalog replay across due refreshes; timing is evidence, not a flaky CI gate."""
import json
from pathlib import Path
import shutil
import time
from types import SimpleNamespace

from tui_gateway.command_discovery import command_catalog


def test_catalog_replay(tmp_path, monkeypatch, record_property):
    from tools import skills_tool as st
    from tools import skill_catalog_parse as reuse
    from agent import skill_utils
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    home = tmp_path / "home"
    home.mkdir()
    root = Path(__file__).parents[2]
    shutil.copytree(root / "skills", home / "skills")
    shutil.copytree(root / "optional-skills", home / "external")
    monkeypatch.setattr(skill_utils, "get_external_skills_dirs", lambda: [home / "external"])
    monkeypatch.setattr(skill_utils, "get_project_skills_dir", lambda: None, raising=False)
    clock = [100.0]
    monkeypatch.setattr(st, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    candidate = reuse.parse_catalog_text
    samples = {"before": [], "after": []}
    outputs = {}
    token = set_hermes_home_override(str(home))
    try:
        # Same full builder and inventory. Only the original pure parse is substituted.
        # Alternate order; warm both variants, then retain every measured sample.
        for variant in ("before", "after", "after", "before"):
            monkeypatch.setattr(reuse, "parse_catalog_text",
                                st._parse_frontmatter if variant == "before" else candidate)
            clock[0] += 151.909
            command_catalog()
            for _ in range(4):
                clock[0] += 151.909  # four calls per frozen 607.636s window; accelerated clock
                start = time.process_time()
                result = command_catalog()
                samples[variant].append(time.process_time() - start)
                if variant in outputs:
                    assert result == outputs[variant]
                outputs[variant] = result
        assert outputs["before"] == outputs["after"]
        assert outputs["after"]["skill_count"] > 20
        record_property("catalog_replay", json.dumps({
            "cpu_s": samples, "skill_count": outputs["after"]["skill_count"],
            "parity": True, "clock": "accelerated expiry, not live RPC budget proof",
            "inputs": "repository bundled and optional skills; isolated home, empty plugin registry"}))
    finally:
        reset_hermes_home_override(token)
