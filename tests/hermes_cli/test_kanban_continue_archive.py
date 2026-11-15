"""Portable continuation archives override only automatic pool pins."""
import hashlib
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_move_pool as kmp
from tests.hermes_cli import test_kanban_pool_queue as helpers

svc = helpers.svc
w2 = helpers.w2


@pytest.mark.parametrize("source", ["checkpoint", "comment"])
@pytest.mark.parametrize("verified", [True, False])
def test_continue_archive(w2, monkeypatch, source, verified):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_continue_archive as archive

    tid = kb.create_task(w2.conn, title="portable archive", assignee="default", runs_on=[helpers.POOL])
    helpers._run(w2, monkeypatch, tid)
    monkeypatch.setattr(kmp, "card_trees_state", lambda *a, **kw: (["/work"], "unchecked worktree /work (timeout)"))
    digest = hashlib.sha256(b"workspace").hexdigest()
    text = f"Full workspace archive hub-admin:/srv/kanban/evidence/{tid}/run.tar.gz SHA256 {digest}"
    import subprocess
    path = f"/srv/kanban/evidence/{tid}/run.tar.gz"
    actual_digest = digest if verified else 'f' * 64
    monkeypatch.setattr(archive.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a[0], 0, f"{actual_digest}  {path}\n"))
    if source == "comment":
        kb.add_comment(w2.conn, tid, author="default", body=text)
        text = "Resume implementation"
    out = json.loads(kt._handle_continue({"checkpoint": text}))
    assert out.get("ok"), out
    assert helpers._runs_on(w2.conn, tid) == ([helpers.POOL] if verified else ["machine:worker-2"])
    assert bool(helpers._placement_events(w2.conn, tid)) is not verified


def test_no_archive_still_pins(w2, monkeypatch):
    from tools import kanban_tools as kt
    tid = kb.create_task(w2.conn, title="no archive", assignee="default", runs_on=[helpers.POOL])
    helpers._run(w2, monkeypatch, tid)
    monkeypatch.setattr(kmp, "card_trees_state", lambda *a, **kw: (["/work"], "dirty worktree /work"))
    out = json.loads(kt._handle_continue({"checkpoint": "Work remains local"}))
    assert out.get("ok"), out
    assert helpers._runs_on(w2.conn, tid) == ["machine:worker-2"]


@pytest.mark.parametrize("mutation", ["valid", "foreign", "traversal", "short-sha", "non-hub"])
def test_reference_scope(w2, monkeypatch, mutation):
    from hermes_cli import kanban_continue_archive as archive
    tid = kb.create_task(w2.conn, title="archive scope", assignee="default")
    text = f"hub-admin:/srv/{tid}/full.tar.gz SHA256 {'a' * 64}"
    text = {
        "valid": text,
        "foreign": text.replace(tid, "t_other"),
        "traversal": text.replace("full.tar", "../full.tar"),
        "short-sha": text[:-1],
        "non-hub": text.replace("hub-admin:", "worker-2:"),
    }[mutation]
    monkeypatch.setattr(archive, "verify", lambda *args: True)
    assert archive.portable(w2.conn, tid, text) is (mutation == "valid")


def test_comments_after_move_and_no_old_fallback(w2, monkeypatch):
    from hermes_cli import kanban_continue_archive as archive
    tid = kb.create_task(w2.conn, title="archive age", assignee="default")
    text = f"hub-admin:/srv/{tid}/full.tar.gz SHA256 {'a' * 64}"
    kb.add_comment(w2.conn, tid, author="default", body=text)
    with kb.write_txn(w2.conn):
        kb._append_event(w2.conn, tid, "moved", {})
    monkeypatch.setattr(archive, "verify", lambda *args: True)
    assert not archive.portable(w2.conn, tid, "resume")  # same-second is conservatively stale
    with kb.write_txn(w2.conn):
        w2.conn.execute("UPDATE task_comments SET created_at = created_at + 1 WHERE task_id = ?", (tid,))
    assert archive.portable(w2.conn, tid, "resume")
    monkeypatch.setattr(archive, "verify", lambda path, sha: sha == 'a' * 64)
    assert not archive.portable(w2.conn, tid, text.replace('a' * 64, 'b' * 64))


@pytest.mark.parametrize("mode", ["match", "mismatch", "missing", "timeout", "error", "extra-output"])
def test_hub_hash_verification(monkeypatch, mode):
    import subprocess
    from hermes_cli import kanban_continue_archive as archive
    digest = hashlib.sha256(b"workspace").hexdigest()
    path = "/srv/t_example/full.tar.gz"

    def run(argv, **kwargs):
        assert argv == ["hub-admin", f"sha256sum -- {path}"]
        assert kwargs["timeout"] == 20 and kwargs["encoding"] == "utf-8"
        if mode == "timeout":
            raise subprocess.TimeoutExpired(argv, 20)
        if mode == "error":
            raise OSError("no hub")
        sha = 'f' * 64 if mode == "mismatch" else digest
        output = f"{sha}  {path}\n" + ("noise" if mode == "extra-output" else "")
        return subprocess.CompletedProcess(argv, 1 if mode == "missing" else 0, output)

    monkeypatch.setattr(archive.subprocess, "run", run)
    assert archive.verify(path, digest) is (mode == "match")
