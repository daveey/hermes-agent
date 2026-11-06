"""Oversize files a project .gitignore re-includes never reach git add's clean filter."""
from __future__ import annotations

import pytest

from tools import checkpoint_staging as staging
from tools.checkpoint_manager import (
    CheckpointManager, _init_store, _project_hash, _ref_name, _run_git, _store_path,
)


@pytest.mark.parametrize("name", ["weights.bin", "we[i]ghts *.bin", " lead\\trail .bin", ":(top)colon.bin"])
def test_max_file_size_mb_reincluded_by_gitignore_never_runs_clean_filter(tmp_path, monkeypatch, name):
    """A project ``!`` re-include beats core.excludesFile; the literal pathspec still holds."""
    checkpoint_base = tmp_path / "checkpoints"
    monkeypatch.setattr("tools.checkpoint_manager.CHECKPOINT_BASE", checkpoint_base)
    wd = tmp_path / "proj"
    wd.mkdir()
    (wd / "small.py").write_text("tiny\n")
    (wd / name).write_bytes(b"\0" * (2 * 1024 * 1024))  # 2 MB
    # An oversize file the project ignores: an exclude pathspec naming it would fail git add.
    (wd / "ignored.bin").write_bytes(b"\0" * (2 * 1024 * 1024))
    escaped = staging.gitignore_literal(name)
    (wd / ".gitignore").write_text(f"*.bin\n!{escaped}\n")
    (wd / ".gitattributes").write_text("* filter=must-not-run\n*.py -filter\n.git* -filter\n")

    store = _store_path(checkpoint_base)
    assert _init_store(store, str(wd)) is None
    assert _run_git(
        ["config", "filter.must-not-run.clean", "hermes-filter-must-not-run"],
        store, str(wd),
    )[0]
    assert _run_git(
        ["config", "filter.must-not-run.required", "true"],
        store, str(wd),
    )[0]
    # The re-include defeats the excludes file alone: git would open the file.
    ok, ignored, _ = _run_git(
        ["ls-files", "-z", "--others", "--ignored", "--exclude-standard"], store, str(wd),
    )
    assert ok and name not in ignored.split("\x00")
    assert "ignored.bin" in ignored.split("\x00")

    m = CheckpointManager(enabled=True, max_snapshots=5, max_file_size_mb=1)
    assert m.ensure_checkpoint(str(wd), "initial") is True

    ok, files, _ = _run_git(
        ["ls-tree", "-r", "-z", "--name-only", _ref_name(_project_hash(str(wd)))],
        store, str(wd),
    )
    assert ok
    names = set(files.split("\x00"))
    assert "small.py" in names
    assert name not in names
    assert "ignored.bin" not in names
