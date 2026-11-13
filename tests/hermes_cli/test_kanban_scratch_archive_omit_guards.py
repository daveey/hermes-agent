"""Safety guards of the remote-held-object omission in scratch archives. [daveey]

Post-merge mutation check of PR #2354 (``hermes_cli/kanban_scratch_archive_omit.py``)
found three guards no test pinned: a shallow clone must keep its objects, a
clean tracked file with a second hard link must be archived whole (so restore
keeps the link), and restore must refuse a blob whose bytes disagree with the
omitted record's size or sha256 (``verify_complete_archive`` does not check those
two fields, so this is the only check that sees them).
"""
import json

import pytest

from tests.hermes_cli import test_kanban_scratch_gc_archive as base

board = base.board
_fresh_probe_map = base._fresh_probe_map
_run = base._run
_gc = base._gc
_archive = base._archive
_members = base._members
FAKE_URL = base.FAKE_URL
OLD = base.OLD


def test_shallow_clone_keeps_every_object(board, tmp_path):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_scratch_archive as sa
    from hermes_cli import kanban_scratch_archive_omit as om

    _tid0, _p0, _r0, bare = base._network_clone_task(tmp_path)  # builds the fixture remote (2 commits)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="shallow clone")
        path = kb.workspaces_root() / tid
        path.mkdir(parents=True)
        _run("clone", "-q", "--depth", "1", f"file://{bare}", str(path / "repo"))
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done', workspace_kind='scratch', workspace_path=?, "
                         "completed_at=?, created_at=? WHERE id=?", (str(path), OLD, OLD, tid))
    repo = path / "repo"
    assert (repo / ".git" / "shallow").is_file()
    _run("remote", "set-url", "origin", FAKE_URL, cwd=repo)
    assert om._PROBE_URL_MAP[FAKE_URL] == str(bare)  # the probe could verify the tip: only shallow refuses
    assert _gc() == 0
    assert not path.exists()
    manifest = sa.verify_complete_archive(_archive(tid))
    assert manifest["version"] == 1 and "omitted" not in manifest and "omitted_repos" not in manifest
    objects = [n for n, (m, _) in _members(tid).items() if n.startswith("repo/.git/objects/") and m.isfile()]
    assert objects
    assert "objects_omitted" not in json.loads((_archive(tid) / "repo" / "meta.json").read_text())


def test_hard_linked_clean_file_is_archived_whole(board, tmp_path):
    from hermes_cli import kanban_scratch_archive as sa
    from hermes_cli import kanban_scratch_archive_omit as om

    tid, path, repo, bare = base._network_clone_task(tmp_path)
    big = (repo / "big.txt").read_bytes()
    (repo / "big-link.txt").hardlink_to(repo / "big.txt")  # untracked second name of a clean tracked file
    assert _gc() == 0
    assert not path.exists()
    archive = _archive(tid)
    manifest = sa.verify_complete_archive(archive)
    assert manifest["version"] == 2  # omission still applies to the repo's objects
    assert "repo/big.txt" not in manifest["omitted"]
    # The scan visits big-link.txt first ("-" sorts before "."), so big.txt is its tar hard link.
    assert manifest["entries"]["repo/big-link.txt"]["type"] == "file"
    assert manifest["entries"]["repo/big.txt"]["type"] == "hardlink"
    assert manifest["entries"]["repo/big.txt"]["link"] == "repo/big-link.txt"
    target = tmp_path / "restored"
    om.restore(archive, target, url_map={FAKE_URL: str(bare)})
    restored = target / "repo"
    assert (restored / "big.txt").read_bytes() == big
    assert (restored / "big-link.txt").read_bytes() == big
    assert (restored / "big.txt").stat().st_ino == (restored / "big-link.txt").stat().st_ino


@pytest.mark.parametrize("field", ["sha256", "size"])
def test_restore_refuses_blob_that_disagrees_with_the_omitted_record(board, tmp_path, field):
    from hermes_cli import kanban_scratch_archive as sa
    from hermes_cli import kanban_scratch_archive_omit as om

    tid, _path, _repo, bare = base._network_clone_task(tmp_path)
    assert _gc() == 0
    archive = _archive(tid)
    manifest_path = archive / base.PAYLOAD / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    rec = manifest["omitted"]["repo/big.txt"]
    if field == "sha256":
        rec["sha256"] = "0" * 64
    else:
        rec["size"] += 1
    manifest_path.write_text(json.dumps(manifest))
    sa.verify_complete_archive(archive)  # verify does not read these fields: only restore's own check can
    target = tmp_path / "restored"
    with pytest.raises(sa.ArchiveRefused, match="does not match its recorded hash"):
        om.restore(archive, target, url_map={FAKE_URL: str(bare)})
    assert not (target / "repo" / "big.txt").exists()
