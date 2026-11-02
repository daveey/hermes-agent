"""Signature walks stat unchanged directories instead of listing them on every watcher tick."""
import os
import time

from tools.skills_tool import _skills_scan_signature


def _age(path):
    old = time.time_ns() - 10_000_000_000
    os.utime(path, ns=(old, old))


def test_unchanged_tree_no_listing_nested_install_is_seen(tmp_path, monkeypatch):
    tmp_path = tmp_path / "skills"
    tmp_path.mkdir()
    category = tmp_path / "category"
    category.mkdir()
    _age(category)
    _age(tmp_path)
    before = _skills_scan_signature([tmp_path], [])
    calls, real = [], os.scandir
    monkeypatch.setattr(os, "scandir", lambda path: calls.append(path) or real(path))
    assert _skills_scan_signature([tmp_path], []) == before
    assert calls == []
    (category / "new-skill").mkdir()
    assert _skills_scan_signature([tmp_path], []) != before
    assert str(category) in calls


def test_symlink_target_type_change_is_seen(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    target = tmp_path / "target"
    target.write_text("file")
    (root / "linked").symlink_to(target)
    _age(root)
    before = _skills_scan_signature([root], [])
    target.unlink()
    target.mkdir()
    assert _skills_scan_signature([root], []) != before


def test_racy_timestamp_cannot_hide_membership_change(tmp_path, monkeypatch):
    # Model a coarse clock: both directory stats stay equal inside the racy window.
    real, frozen = os.stat, os.stat(tmp_path)
    monkeypatch.setattr(os, "stat", lambda path, *a, **kw: frozen if str(path) == str(tmp_path) else real(path, *a, **kw))
    before = _skills_scan_signature([tmp_path], [])
    (tmp_path / "new").mkdir()
    assert _skills_scan_signature([tmp_path], []) != before
