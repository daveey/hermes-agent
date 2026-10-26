"""Observation-only retention contracts on real temporary trees."""
import base64
import copy
import errno
import os
import subprocess

import pytest

from hermes_cli import retention_plan as rp

pytestmark = pytest.mark.platforms("linux")


def paths(result):
    return {base64.b64decode(r["path_b64"]): r for r in result["records"]}


def codes(result):
    return {e["code"] for e in result["errors"]}


def test_inventory_metadata_hardlinks_raw_paths_and_no_mutation(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    root.mkdir()
    file = root / "data"
    file.write_bytes(b"x" * 8192)
    os.link(file, root / "alias")
    raw = os.fsencode(root) + b"/bad-\xff\n"
    fd = os.open(raw, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    before = {p: os.stat(p, follow_symlinks=False) for p in (root, file, root / "alias", raw)}

    def forbidden(*args, **kwargs):
        raise AssertionError("inventory must not execute Git or any subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    result = rp.inventory(tmp_path, "cache")
    records = paths(result)
    assert result["scan_complete"]
    assert set(records) == {b"cache", b"cache/data", b"cache/alias", b"cache/bad-\xff\n"}
    assert result["totals"]["allocated_bytes"] == sum(before[p].st_blocks * 512 for p in (root, file, raw))
    assert result["totals"]["observed_entries"] == 4
    assert result["totals"]["unique_observed_inodes"] == 3
    for record in records.values():
        assert record["decision"] == "retain"
        assert record["deletion_authorized"] is False
        for field in ("device", "inode", "mtime_ns", "ctime_ns"):
            assert isinstance(record[field], str)
    for path, prior in before.items():
        after = os.stat(path, follow_symlinks=False)
        assert rp._identity(prior) == rp._identity(after)
    assert file.read_bytes() == b"x" * 8192
    assert not result["apply_eligible"]
    assert result["mode"] == "observation-only"


@pytest.mark.parametrize("relative", ["", ".", "..", "../cache", "/cache", "cache/", "a//b", "a/./b", "a/../b", "a\\b", "a\x00b"])
def test_strict_relative_path(tmp_path, relative):
    with pytest.raises(ValueError):
        rp.inventory(tmp_path, relative)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, 100001])
def test_invalid_limits(tmp_path, limit):
    with pytest.raises(ValueError):
        rp.inventory(tmp_path, "cache", limit)


def test_missing_root_is_unknown_not_zero(tmp_path):
    result = rp.inventory(tmp_path, "missing")
    assert not result["scan_complete"]
    assert result["totals"]["allocated_bytes"] is None
    assert result["totals"]["measured_partial_allocated_bytes"] == 0
    assert result["records"] == []
    assert result["errors"][0]["errno"] == errno.ENOENT


def test_limit_is_partial_and_exact_empty_root_is_complete(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    assert rp.inventory(tmp_path, "cache", 1)["scan_complete"]
    for i in range(8):
        (root / str(i)).write_bytes(b"payload")
    result = rp.inventory(tmp_path, "cache", 3)
    assert len(result["records"]) == 3
    assert "entry_limit" in codes(result)
    assert result["totals"]["allocated_bytes"] is None
    assert result["totals"]["measured_partial_allocated_bytes"] > 0


def test_symlinks_are_never_followed_even_in_workspace_ancestors(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("keep")
    root = tmp_path / "cache"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    result = rp.inventory(tmp_path, "cache")
    assert set(paths(result)) == {b"cache", b"cache/link"}
    assert "symlink_retained" in codes(result)
    assert not rp.inventory(tmp_path, "cache/link")["scan_complete"]
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    result = rp.inventory(alias, "cache")
    assert not result["scan_complete"] and not result["records"]
    assert (outside / "sentinel").read_text() == "keep"


@pytest.mark.parametrize("markers", [(".git",), ("HEAD", "objects", "refs")])
def test_git_and_bare_roots_are_retained_untraversed(tmp_path, markers):
    root = tmp_path / "cache"
    root.mkdir()
    for marker in markers:
        (root / marker).mkdir()
    (root / "precious").write_text("source")
    result = rp.inventory(tmp_path, "cache")
    assert not result["scan_complete"]
    assert set(paths(result)) == {b"cache"}
    assert codes(result) & {"git_metadata", "possible_bare_repository"}


def test_nested_git_and_git_ancestor_are_excluded(tmp_path):
    root = tmp_path / "cache"
    (root / "repo" / ".git").mkdir(parents=True)
    (root / "repo" / "subtree").mkdir()
    result = rp.inventory(tmp_path, "cache")
    assert set(paths(result)) == {b"cache", b"cache/repo"}
    assert "git_metadata" in codes(result)
    assert not rp.inventory(tmp_path, "cache/repo/subtree")["scan_complete"]
    (tmp_path / ".git").mkdir()
    (tmp_path / "ordinary" / "nested").mkdir(parents=True)
    assert rp.inventory(tmp_path, "ordinary/nested")["scan_complete"]


def test_mount_uncertainty_retains(tmp_path, monkeypatch):
    (tmp_path / "cache").mkdir()

    def unavailable(fd):
        raise OSError(errno.EACCES, "unavailable")

    monkeypatch.setattr(rp, "_mount", unavailable)
    result = rp.inventory(tmp_path, "cache")
    assert not result["scan_complete"]
    assert result["totals"]["allocated_bytes"] is None


def test_same_device_mount_boundary_is_not_crossed(tmp_path, monkeypatch):
    (tmp_path / "cache" / "mounted").mkdir(parents=True)
    (tmp_path / "cache" / "mounted" / "sentinel").write_text("keep")
    mounted_inode = (tmp_path / "cache" / "mounted").stat().st_ino
    monkeypatch.setattr(rp, "_mount", lambda fd: "2" if os.fstat(fd).st_ino == mounted_inode else "1")
    result = rp.inventory(tmp_path, "cache")
    assert "mount_boundary" in codes(result)
    assert b"cache/mounted/sentinel" not in paths(result)


def test_replacement_between_stat_and_open_is_not_followed(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("keep")
    original = os.open
    changed = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal changed
        if path == b"cache" and not changed:
            changed = True
            root.rename(tmp_path / "old")
            root.symlink_to(outside, target_is_directory=True)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    result = rp.inventory(tmp_path, "cache")
    assert changed and not result["scan_complete"]
    assert b"cache/secret" not in paths(result)
    assert (outside / "secret").read_text() == "keep"


def test_failures_count_toward_bound(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    root.mkdir()
    for i in range(10):
        (root / str(i)).touch()
    original = os.stat
    attempts = []

    def denied(path, *args, **kwargs):
        if isinstance(path, bytes) and path.isdigit():
            attempts.append(path)
            raise PermissionError(errno.EACCES, "denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", denied)
    result = rp.inventory(tmp_path, "cache", 3)
    assert len(attempts) == 2
    assert "entry_limit" in codes(result)
    assert result["totals"]["allocated_bytes"] is None


def test_depth_bound(tmp_path, monkeypatch):
    (tmp_path / "cache" / "a" / "b" / "c").mkdir(parents=True)
    monkeypatch.setattr(rp, "MAX_DEPTH", 2)
    result = rp.inventory(tmp_path, "cache")
    assert "depth_limit" in codes(result)
    assert b"cache/a/b/c" not in paths(result)


def test_plan_deduplicates_across_disjoint_roots_and_rejects_overlap(tmp_path):
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    (tmp_path / "a" / "file").write_bytes(b"x" * 8192)
    os.link(tmp_path / "a" / "file", tmp_path / "b" / "link")
    a, b = (rp.inventory(tmp_path, p) for p in ("a", "b"))
    result = rp.plan([a, b])
    assert result["totals"]["allocated_bytes"] == sum(p.stat().st_blocks * 512 for p in (tmp_path / "a", tmp_path / "b", tmp_path / "a" / "file"))
    assert not result["apply_eligible"] and not result["deletion_authorized"]
    with pytest.raises(ValueError, match="overlapping"):
        rp.plan([a, a])
    (tmp_path / "a" / "nested").mkdir()
    with pytest.raises(ValueError, match="overlapping"):
        rp.plan([a, rp.inventory(tmp_path, "a/nested")])
    alias = copy.deepcopy(a)
    alias["workspace_b64"] = base64.b64encode(b"/alias").decode()
    alias["workspace_identity"] = None
    with pytest.raises(ValueError, match="duplicate"):
        rp.plan([a, alias])


def test_plan_partial_and_live_schema_rejection(tmp_path):
    result = rp.plan([rp.inventory(tmp_path, "missing")])
    assert result["totals"]["allocated_bytes"] is None
    assert not result["scan_complete"]
    with pytest.raises(ValueError):
        rp.plan([{"schema_version": 1, "mode": "live-plan"}])


def test_unknown_allocation_never_becomes_zero(tmp_path, monkeypatch):
    (tmp_path / "cache").mkdir()
    original = rp._record

    def unknown(path, s):
        result = original(path, s)
        result["allocated_bytes"] = None
        return result

    monkeypatch.setattr(rp, "_record", unknown)
    result = rp.inventory(tmp_path, "cache")
    assert result["scan_complete"]  # Enumeration and measurement are separate.
    assert result["totals"]["allocated_bytes"] is None
    assert result["totals"]["unknown_bytes"] is None
    assert rp.plan([result])["totals"]["allocated_bytes"] is None


def test_file_bind_mount_is_detected_without_reading_contents(tmp_path, monkeypatch):
    (tmp_path / "cache").mkdir()
    file = tmp_path / "cache" / "file"
    file.write_text("keep")
    inode = file.stat().st_ino
    monkeypatch.setattr(rp, "_mount", lambda fd: "2" if os.fstat(fd).st_ino == inode else "1")
    result = rp.inventory(tmp_path, "cache")
    assert "mount_boundary" in codes(result)
    assert result["totals"]["allocated_bytes"] is None


def test_git_metadata_cannot_be_reframed_as_workspace(tmp_path):
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    result = rp.inventory(tmp_path / ".git", "objects")
    assert not result["scan_complete"] and not result["records"]


def test_same_workspace_alias_overlap_is_rejected(tmp_path):
    (tmp_path / "cache" / "nested").mkdir(parents=True)
    outer = rp.inventory(tmp_path, "cache")
    nested = rp.inventory(tmp_path, "cache/nested")
    nested["workspace_b64"] = base64.b64encode(b"/different-workspace-alias").decode()
    with pytest.raises(ValueError, match="overlapping"):
        rp.plan([outer, nested])


def test_directory_substitution_is_detected(tmp_path, monkeypatch):
    (tmp_path / "cache").mkdir()
    (tmp_path / "replacement").mkdir()
    (tmp_path / "replacement" / "secret").write_text("keep")
    original = os.open
    changed = False

    def substitute(path, flags, *args, **kwargs):
        nonlocal changed
        if path == b"cache" and not changed:
            changed = True
            (tmp_path / "cache").rename(tmp_path / "old")
            (tmp_path / "replacement").rename(tmp_path / "cache")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", substitute)
    result = rp.inventory(tmp_path, "cache")
    assert "identity_changed" in codes(result)
    assert b"cache/secret" not in paths(result)


def test_scandir_error_closes_descriptors(tmp_path, monkeypatch):
    (tmp_path / "cache").mkdir()
    original_open, original_close = os.open, os.close
    descriptors = set()

    def tracking_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        descriptors.add(fd)
        return fd

    def tracking_close(fd):
        descriptors.remove(fd)
        return original_close(fd)

    def denied(fd):
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "close", tracking_close)
    monkeypatch.setattr(os, "scandir", denied)
    result = rp.inventory(tmp_path, "cache")
    assert not result["scan_complete"]
    assert result["totals"]["allocated_bytes"] is None
    assert not descriptors
