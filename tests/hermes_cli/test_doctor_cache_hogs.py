"""``hermes doctor`` names cache-root dirs that no pruner covers once they are big enough."""

from hermes_cli.doctor_state import unpruned_cache_hogs


def test_unpruned_cache_hogs_skips_pruned_dirs_and_small_entries(tmp_path):
    cache = tmp_path / "cache"
    for name in ("scratch", "terminal", "campaign-x", "web"):
        (cache / name).mkdir(parents=True)
        with open(cache / name / "blob", "wb") as fh:
            fh.truncate(2048)
            fh.write(b"x" * 2048)
    hogs = unpruned_cache_hogs(tmp_path, min_bytes=1024)
    assert {name for name, _ in hogs} == {"campaign-x", "web"}
    assert not unpruned_cache_hogs(tmp_path, min_bytes=1 << 20)
    assert all(size >= 2048 for _, size in hogs)


def test_unpruned_cache_hogs_skips_the_noindex_scratch_dir_and_symlinks(tmp_path):
    """[daveey] cache/scratch.noindex is the reaped scratch dir on macOS (cache/scratch links to it)."""
    import os

    cache = tmp_path / "cache"
    (cache / "scratch.noindex").mkdir(parents=True)
    os.symlink("scratch.noindex", cache / "scratch")
    with open(cache / "scratch.noindex" / "blob", "wb") as fh:
        fh.write(b"x" * 2048)
    assert unpruned_cache_hogs(tmp_path, min_bytes=1024) == []
