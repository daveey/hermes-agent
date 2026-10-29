"""``prompt_surface.render`` gives the same bytes wherever the caller's TMPDIR lives.

Workspace discovery walks up from the render's cwd looking for a ``.git`` or a project marker. When
TMPDIR sat under a real directory with an AGENTS.md (the hub: ``~/.hermes/cache/scratch`` under
``~/AGENTS.md``) the render picked that directory up and rewrote five prompts.
"""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("prompt_surface", REPO / "scripts" / "ci" / "prompt_surface.py")
assert _spec is not None and _spec.loader is not None
prompt_surface = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prompt_surface)


def test_render_ignores_project_markers_above_tmpdir(tmp_path, monkeypatch):
    outer = tmp_path / "outer"
    scratch = outer / "cache" / "scratch"
    scratch.mkdir(parents=True)
    (outer / "AGENTS.md").write_text("# Outer rules\nUse the outer conventions.\n", encoding="utf-8")
    (outer / "Makefile").write_text("test:\n\ttrue\n", encoding="utf-8")
    (outer / ".git").mkdir()

    clean = tmp_path / "clean"
    assert prompt_surface.render(clean) == 0

    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    polluted = tmp_path / "polluted"
    assert prompt_surface.render(polluted) == 0

    assert prompt_surface.tree_differences(clean, polluted) == []


def test_check_reports_stale_files(tmp_path):
    stale = tmp_path / "stale"
    stale.mkdir()
    (stale / "surfaces.json").write_text("{}\n", encoding="utf-8")
    assert prompt_surface.check(stale) == 1
