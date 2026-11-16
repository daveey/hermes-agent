"""Actual archive bytes, rather than prose or a matching tarball hash, carry state."""
import hashlib
import json
import subprocess
import tarfile

import pytest

from hermes_cli import kanban_continue_archive as archive
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_move_pool as kmp
from tests.hermes_cli import test_kanban_pool_queue as helpers

svc = helpers.svc
w2 = helpers.w2


@pytest.mark.parametrize("case", ["valid", "diagnostic", "stale", "missing-file", "outside-tree",
                                  "sha-mismatch", "linked-git", "symlink", "head-change", "mode-change"])
def test_content_bound_continue(w2, monkeypatch, tmp_path, case):
    from tools import kanban_tools as kt

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "source.py").write_text("current uncommitted work")
    tid = kb.create_task(w2.conn, title="content archive", assignee="default", runs_on=[helpers.POOL])
    helpers._run(w2, monkeypatch, tid)
    with kb.write_txn(w2.conn):
        w2.conn.execute("UPDATE tasks SET workspace_path = ? WHERE id = ?", (str(root), tid))
    trees = [str(tmp_path / "external")] if case == "outside-tree" else [str(root)]
    monkeypatch.setattr(kmp, "card_trees_state", lambda *a, **kw: (trees, f"dirty worktree {trees[0]}"))
    target = tmp_path / tid / "full.tar.gz"
    target.parent.mkdir()
    if case == "linked-git":
        (root / ".git").write_text("gitdir: /external/worktrees/example")
    if case == "symlink":
        (root / "link").symlink_to("source.py")
    if case == "head-change":
        (root / ".git").mkdir()
        (root / ".git" / "HEAD").write_text("old HEAD")
    with tarfile.open(target, "w:gz") as tar:
        tar.add(root, arcname=".")
    if case == "head-change":
        (root / ".git" / "HEAD").write_text("new HEAD")
    if case == "mode-change":
        (root / "source.py").chmod(0o755)
    if case == "stale":
        (root / "source.py").write_text("newer uncommitted work")
    if case == "missing-file":
        (root / "new.py").write_text("not carried")
    digest = '0' * 64 if case == "sha-mismatch" else hashlib.sha256(target.read_bytes()).hexdigest()
    marker = "Raw diagnostic archive" if case == "diagnostic" else "portable-workspace:"
    text = f"{marker} hub-admin:{target} SHA256 {digest}\nUNPUBLISHED_WORK_REASON=archive custody fixture"
    real_run = subprocess.run

    def hub(argv, **kwargs):
        if argv[0] == "hub-admin":
            return real_run(["bash", "-c", argv[1]], **kwargs)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(archive.subprocess, "run", hub)
    out = json.loads(kt._handle_continue({"checkpoint": text}))
    assert out.get("ok"), out
    assert helpers._runs_on(w2.conn, tid) == ([helpers.POOL] if case == "valid" else ["machine:worker-2"])
    assert bool(helpers._placement_events(w2.conn, tid)) is (case != "valid")
