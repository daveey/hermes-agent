"""``prompt_surface.render`` gives the same bytes wherever the caller's TMPDIR lives.

Workspace discovery walks up from the render's cwd looking for a ``.git`` or a project marker. When
TMPDIR sat under a real directory with an AGENTS.md (the hub: ``~/.hermes/cache/scratch`` under
``~/AGENTS.md``) the render picked that directory up and rewrote five prompts.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
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


def test_render_is_the_same_when_tmpdir_is_reached_through_a_symlink(tmp_path, monkeypatch):
    """macOS's /var/folders is a symlink to /private/var/folders: the child scrubs the resolved
    HOME, so a home spelled through the link leaked raw temp paths into every prompt."""
    clean = tmp_path / "clean"
    assert prompt_surface.render(clean) == 0

    real = tmp_path / "private" / "tmp"
    real.mkdir(parents=True)
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(tempfile, "tempdir", str(link))
    via_link = tmp_path / "via_link"
    assert prompt_surface.render(via_link) == 0

    assert prompt_surface.tree_differences(clean, via_link) == []


def test_render_child_keeps_the_callers_pythonpath_after_the_tree(tmp_path, monkeypatch):
    """pm-managed installs (the Macs) put the dependencies' site-packages on PYTHONPATH."""
    seen = {}

    def fake_run(argv, *, env, **_kw):
        seen.update(env)
        return subprocess.CompletedProcess(argv, 1, "", "stop")

    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(["/deps/site-packages", "/more"]))
    monkeypatch.setattr(prompt_surface.subprocess, "run", fake_run)
    assert prompt_surface.render(tmp_path / "out") == 1
    assert seen["PYTHONPATH"].split(os.pathsep) == [str(prompt_surface.ROOT), "/deps/site-packages", "/more"]


def test_pinned_render_keeps_the_hosts_platform_modules():
    """The render pins sys.platform to "linux"; modules that choose an implementation from it at
    import (sysconfig's data module, psutil's extension) must already be loaded for the real
    host, or on macOS zoneinfo dies on `_sysconfigdata__linux_darwin` and psutil on its Linux
    extension."""
    host = sys.platform
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('ps', {str(REPO / 'scripts/ci/prompt_surface.py')!r})\n"
        "ps = importlib.util.module_from_spec(spec); spec.loader.exec_module(ps)\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "ps._pin_environment()\n"
        "import sysconfig, zoneinfo, psutil\n"
        "assert sys.platform == 'linux'\n"
        "sysconfig.get_config_var('TZPATH')\n"
        "print(psutil._psplatform.__name__)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300,
                          check=False)
    assert proc.returncode == 0, proc.stderr[-3000:]
    expected = {"darwin": "psutil._psosx", "linux": "psutil._pslinux", "win32": "psutil._pswindows"}
    if host in expected:
        assert proc.stdout.strip().splitlines()[-1] == expected[host]
