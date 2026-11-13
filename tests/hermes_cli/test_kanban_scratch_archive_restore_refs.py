"""Scratch archive restore drops only remote-tracking refs the remote reported gone. [daveey]

`_check_refs` deletes a missing ``refs/remotes/*`` ref only when its tip is in
the set of oids the remote refused as gone; any other unresolved ref, and a
detached HEAD naming a missing object, refuses the restore. A partial clone
skips ``fsck``, so the HEAD check is the only guard there.
"""
import pytest

from tests.hermes_cli import test_kanban_scratch_gc_archive as base

_fresh_probe_map = base._fresh_probe_map  # autouse: each test starts with an empty probe URL map
board = base.board
_run = base._run
_gc = base._gc
_archive = base._archive
FAKE_URL = base.FAKE_URL


def _side_commits(tmp_path, repo, bare, count):
    """Push `side` (``count`` empty commits over main) and fetch it as origin/side; return its commits, oldest first."""
    other = tmp_path / "side-pusher"
    _run("clone", "-q", str(bare), str(other))
    _run("checkout", "-q", "-b", "side", cwd=other)
    for i in range(count):  # empty: side's trees are main's, so every blob stays on the remote
        _run("commit", "-q", "--allow-empty", "-m", f"side {i}", cwd=other)
    _run("push", "-q", "origin", "side", cwd=other)
    _run("fetch", "-q", str(bare), "+refs/heads/side:refs/remotes/origin/side", cwd=repo)
    return _run("rev-list", "--reverse", "main..side", cwd=other).split()


def _restore(tid, bare, tmp_path):
    from hermes_cli import kanban_scratch_archive_omit as om
    base._delete_on_remote(bare, "side")
    return om.restore(_archive(tid), tmp_path / "restored", url_map={FAKE_URL: str(bare)})


def test_missing_remote_tracking_ref_the_remote_never_reported_gone_refuses(board, tmp_path):
    # A refs/remotes/ ref of a remote the record does not name: its tip was never fetched, so never "gone".
    from hermes_cli import kanban_scratch_archive as sa
    tid, _path, repo, bare = base._network_clone_task(tmp_path)
    first, tip = _side_commits(tmp_path, repo, bare, 2)
    _run("update-ref", "refs/remotes/upstream/old", first, cwd=repo)  # no `upstream` remote configured
    assert _gc() == 0
    [record] = sa.verify_complete_archive(_archive(tid))["omitted_repos"]
    assert record["remote_refs"]["refs/remotes/origin/side"] == tip
    assert "refs/remotes/upstream/old" not in record["remote_refs"]
    with pytest.raises(sa.ArchiveRefused, match=r"do not all resolve: refs/remotes/upstream/old$"):
        _restore(tid, bare, tmp_path)


def test_partial_clone_restore_refuses_a_detached_head_naming_a_missing_object(board, tmp_path):
    # fsck is skipped in a partial clone, so the detached-HEAD check is what refuses.
    from hermes_cli import kanban_scratch_archive as sa
    tid, _path, repo, bare = base._network_clone_task(tmp_path)
    [tip] = _side_commits(tmp_path, repo, bare, 1)
    _run("config", "remote.origin.promisor", "true", cwd=repo)
    _run("config", "extensions.partialClone", "origin", cwd=repo)
    _run("checkout", "-q", "--detach", tip, cwd=repo)
    assert _gc() == 0
    [record] = sa.verify_complete_archive(_archive(tid))["omitted_repos"]
    assert record["remote_refs"]["refs/remotes/origin/side"] == tip
    with pytest.raises(sa.ArchiveRefused, match=r"do not all resolve: HEAD$"):
        _restore(tid, bare, tmp_path)
