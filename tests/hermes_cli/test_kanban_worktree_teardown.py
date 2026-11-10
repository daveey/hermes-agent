"""Tests for worktree workspace teardown at task completion/archive.

Covers the ownership gap where kanban ``worktree`` workspaces were never
reaped by anything: ``_cleanup_workspace`` preserved them by design, the CLI
startup pruner explicitly skips ``t_*`` worktrees ("dispatcher-driven
lifecycle"), and ``kanban gc`` only swept scratch. A completed or archived
task's linked worktree is now removed when — and only when — it provably
holds no work: clean working tree and every commit reachable from a
remote-tracking ref. Any doubt preserves the worktree.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_workspace as kbw
from hermes_cli import kanban_db_connect as kbc


def _git(*args: str, cwd: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Filesystem-only lifecycle fixtures own no external jobs. Keep discovery
    # synthetic as well as HOME; live-writer coverage supplies its own processes.
    monkeypatch.setattr(psutil, "process_iter", lambda *args, **kwargs: iter(()))
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A project repo with a remote whose history is fully pushed."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", str(origin))
    project = tmp_path / "project"
    _git("clone", str(origin), str(project))
    _git("-C", str(project), "config", "user.email", "t@example.com")
    _git("-C", str(project), "config", "user.name", "t")
    (project / "README.md").write_text("hello\n", encoding="utf-8")
    _git("-C", str(project), "add", "README.md")
    _git("-C", str(project), "commit", "-m", "init")
    _git("-C", str(project), "push", "origin", "HEAD")
    return project


def _make_worktree(repo: Path, task_id: str, branch: str | None = None) -> Path:
    target = repo / ".worktrees" / task_id
    kbw._ensure_git_worktree(repo, target, branch or f"wt/{task_id}")
    return target


def _branch_exists(repo: Path, branch: str) -> bool:
    out = _git("-C", str(repo), "branch", "--list", branch)
    return bool(out.strip())


# ---------------------------------------------------------------------------
# _cleanup_worktree_workspace unit behavior
# ---------------------------------------------------------------------------


def test_clean_pushed_worktree_removed(repo: Path) -> None:
    wt = _make_worktree(repo, "t_aaaa1111")
    kbw._cleanup_worktree_workspace("t_aaaa1111", str(wt))
    assert not wt.exists()
    # auto-generated task branch goes with it
    assert not _branch_exists(repo, "wt/t_aaaa1111")
    # main checkout untouched
    assert (repo / "README.md").exists()


def test_cleanup_leaves_a_worktree_cwd_before_removal(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows cannot remove a worktree that is the process's current directory."""
    wt = _make_worktree(repo, "t_cwd112425")
    real_git = kbw._git

    def windows_git(repo_root: Path, *args: str, timeout: int) -> subprocess.CompletedProcess:
        if args[:2] == ("worktree", "remove") and Path.cwd().is_relative_to(wt):
            return subprocess.CompletedProcess(
                ["git", *args], 1, stderr="Permission denied: current directory"
            )
        return real_git(repo_root, *args, timeout=timeout)

    monkeypatch.setattr(kbw, "_git", windows_git)
    monkeypatch.chdir(wt)
    kbw._cleanup_worktree_workspace("t_cwd112425", str(wt))

    assert Path.cwd() == repo
    assert not wt.exists()


def test_cleanup_proceeds_when_cwd_was_deleted(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deferred parent cleanup (#33774) runs after the child's scratch cwd was
    rmtree'd; a dead cwd must not preserve a clean, pushed worktree."""
    wt = _make_worktree(repo, "t_deadcwd113073")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.chdir(scratch)
    scratch.rmdir()

    kbw._cleanup_worktree_workspace("t_deadcwd113073", str(wt))

    assert not wt.exists()
    assert not _branch_exists(repo, "wt/t_deadcwd113073")


def test_cleanup_retries_worktree_removal_once(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brief Windows directory-handle delay gets one safe retry."""
    wt = _make_worktree(repo, "t_retry112425")
    real_git = kbw._git
    attempts = 0

    def delayed_remove(repo_root: Path, *args: str, timeout: int) -> subprocess.CompletedProcess:
        nonlocal attempts
        if args[:2] == ("worktree", "remove"):
            attempts += 1
            if attempts == 1:
                return subprocess.CompletedProcess(
                    ["git", *args], 1, stderr="Permission denied: handle pending"
                )
        return real_git(repo_root, *args, timeout=timeout)

    monkeypatch.setattr(kbw, "_git", delayed_remove)
    monkeypatch.setattr(kbw.time, "sleep", lambda _delay: None)
    kbw._cleanup_worktree_workspace("t_retry112425", str(wt))

    assert attempts == 2
    assert not wt.exists()


def test_dirty_worktree_preserved(repo: Path) -> None:
    wt = _make_worktree(repo, "t_bbbb2222")
    (wt / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
    kbw._cleanup_worktree_workspace("t_bbbb2222", str(wt))
    assert wt.is_dir()
    assert (wt / "wip.txt").exists()


def test_unpushed_commits_preserved(repo: Path) -> None:
    wt = _make_worktree(repo, "t_cccc3333")
    (wt / "work.txt").write_text("committed but not pushed\n", encoding="utf-8")
    _git("-C", str(wt), "add", "work.txt")
    _git("-C", str(wt), "commit", "-m", "local work")
    kbw._cleanup_worktree_workspace("t_cccc3333", str(wt))
    assert wt.is_dir()


def test_gc_reaps_stale_unpushed_worktree(kanban_home: Path, repo: Path) -> None:
    import argparse
    import time
    from hermes_cli.kanban_ops import _cmd_gc

    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        (wt / "local.txt").write_text("recover me\n")
        _git("-C", str(wt), "add", "local.txt")
        _git("-C", str(wt), "commit", "-m", "unpushed")
        head = _git("-C", str(wt), "rev-parse", "HEAD").strip()
        old = int(time.time()) - 9 * 86400
        conn.execute("UPDATE tasks SET status='archived', completed_at=?, created_at=? WHERE id=?",
                     (old, old, tid))
        conn.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (old, tid))
    assert _cmd_gc(argparse.Namespace(event_retention_days=0, log_retention_days=0,
                                      worktree_retention_days=7)) == 0
    assert not wt.exists()
    assert not _branch_exists(repo, f"wt/{tid}")
    assert _git("-C", str(repo), "show", f"refs/hermes/kanban-archive/{tid}/{head}:local.txt") == "recover me\n"


def test_custom_branch_survives_worktree_removal(repo: Path) -> None:
    wt = _make_worktree(repo, "t_dddd4444", branch="feature/custom")
    kbw._cleanup_worktree_workspace("t_dddd4444", str(wt), "feature/custom")
    assert not wt.exists()
    # only auto-generated wt/* branches are deleted
    assert _branch_exists(repo, "feature/custom")


def test_main_checkout_never_removed(repo: Path) -> None:
    kbw._cleanup_worktree_workspace("t_eeee5555", str(repo))
    assert repo.is_dir()
    assert (repo / "README.md").exists()


def test_non_git_dir_preserved(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-worktree"
    plain.mkdir()
    kbw._cleanup_worktree_workspace("t_ffff6666", str(plain))
    assert plain.is_dir()


def test_tree_dirtied_between_check_and_removal_preserved(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TOCTOU: a tree that becomes dirty after the pre-check is NOT removed.

    Simulates the race by making the pre-check see a clean tree while the
    tree is actually dirty when ``git worktree remove`` runs. Without
    ``--force``, git's own dirty guard re-verifies at removal time and the
    removal fails safe.
    """
    wt = _make_worktree(repo, "t_gggg7777")
    (wt / "late-wip.txt").write_text("dirtied after the check\n", encoding="utf-8")
    # Pre-check lies (as if the file appeared just after it ran) — real git
    # must still refuse the removal.
    from hermes_cli import worktree_ops

    monkeypatch.setattr(worktree_ops, "_worktree_is_dirty", lambda _p, *_a, **_k: False)
    kbw._cleanup_worktree_workspace("t_gggg7777", str(wt))
    assert wt.is_dir()
    assert (wt / "late-wip.txt").exists()


# ---------------------------------------------------------------------------
# Lifecycle integration: complete / archive / deferred parents
# ---------------------------------------------------------------------------


def _worktree_task(conn, repo: Path, title: str = "wt-task") -> tuple[str, Path]:
    tid = kb.create_task(conn, title=title, assignee="worker")
    wt = _make_worktree(repo, tid)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workspace_kind='worktree', workspace_path=?, "
            "branch_name=? WHERE id=?",
            (str(wt), f"wt/{tid}", tid),
        )
    return tid, wt


def test_complete_task_reaps_clean_worktree(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")
    assert not wt.exists()
    assert not _branch_exists(repo, f"wt/{tid}")


def test_complete_task_preserves_dirty_worktree(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        (wt / "wip.txt").write_text("unsaved\n", encoding="utf-8")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.complete_task(conn, tid, summary="done")
    assert wt.is_dir()
    assert (wt / "wip.txt").exists()


def test_archive_task_reaps_clean_worktree(kanban_home: Path, repo: Path) -> None:
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        assert kb.archive_task(conn, tid)
    assert not wt.exists()


def test_parent_worktree_deferred_until_children_done(
    kanban_home: Path, repo: Path
) -> None:
    with kbc.connect_closing() as conn:
        parent, parent_wt = _worktree_task(conn, repo, title="parent")
        child = kb.create_task(conn, title="child", assignee="worker")
        kb.link_tasks(conn, parent, child)

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        assert kb.claim_task(conn, parent, claimer="worker") is not None
        assert kb.complete_task(conn, parent, summary="parent done")
        # child still active -> parent worktree must survive for handoff
        assert parent_wt.is_dir()

        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (child,))
        assert kb.claim_task(conn, child, claimer="worker") is not None
        assert kb.complete_task(conn, child, summary="child done")
    # last child terminal -> deferred parent worktree reaped
    assert not parent_wt.exists()


def _age_task(conn, tid, status="done", days=9):
    import time
    old = int(time.time()) - days * 86400
    conn.execute("UPDATE tasks SET status=?, completed_at=?, created_at=? WHERE id=?", (status, old, old, tid))
    conn.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (old, tid))


def _local_commit(wt):
    (wt / "local.txt").write_text("precious\n")
    _git("-C", str(wt), "add", "local.txt")
    _git("-C", str(wt), "commit", "-m", "local")
    return _git("-C", str(wt), "rev-parse", "HEAD").strip()


@pytest.mark.parametrize("status,days,retention,dirty,keep", [
    ("done", 9, 7, False, False), ("failed", 9, 7, False, False),
    ("cancelled", 9, 7, False, False), ("archived", 9, 7, False, False),
    ("running", 9, 7, False, True), ("review", 9, 7, False, True),
    ("blocked", 9, 7, False, True), ("done", 1, 7, False, True),
    ("done", 9, 0, False, True), ("done", 9, 30, False, True),
    ("done", 9, 7, True, True),
])
def test_retention_safety(kanban_home, repo, status, days, retention, dirty, keep):
    from hermes_cli.kanban_worktree_gc import gc_task_worktrees
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        head = _local_commit(wt)
        _age_task(conn, tid, status, days)
        if dirty:
            (wt / "local.txt").write_text("uncommitted\n")
        assert gc_task_worktrees(conn, older_than_days=retention) == int(not keep)
    assert wt.exists() == keep
    if not keep:
        _git("-C", str(repo), "gc", "--prune=now")
        assert _git("-C", str(repo), "show", f"refs/hermes/kanban-archive/{tid}/{head}:local.txt") == "precious\n"


@pytest.mark.parametrize("guard", ["child", "shared", "archive-failure", "locked"])
def test_retention_preserves_guarded_tree(kanban_home, repo, monkeypatch, guard):
    from hermes_cli.kanban_worktree_gc import gc_task_worktrees
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        _local_commit(wt)
        _age_task(conn, tid)
        if guard in {"child", "shared"}:
            other = kb.create_task(conn, title="still working", assignee="worker")
            if guard == "child":
                kb.link_tasks(conn, tid, other)
            else:
                conn.execute("UPDATE tasks SET workspace_path=? WHERE id=?", (str(wt), other))
        elif guard == "locked":
            _git("-C", str(repo), "worktree", "lock", str(wt))
        else:
            real_git = kbw._git
            def fail_archive(root, *args, **kwargs):
                if args[0] == "update-ref":
                    return subprocess.CompletedProcess(args, 1, "", "archive failed")
                return real_git(root, *args, **kwargs)
            monkeypatch.setattr(kbw, "_git", fail_archive)
        assert gc_task_worktrees(conn) == 0
        assert wt.exists()
        assert _branch_exists(repo, f"wt/{tid}")


def test_startup_reaps_archived_and_tombstones_missing_owner(kanban_home, repo, monkeypatch):
    import json
    from hermes_cli import kanban_worktree_gc as gc
    monkeypatch.setattr(gc, "retention_days", lambda: 7)
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        _local_commit(wt)
        _age_task(conn, tid, "archived")
        missing, orphan = _worktree_task(conn, repo)
        _local_commit(orphan)
        conn.execute("DELETE FROM task_events WHERE task_id=?", (missing,))
        conn.execute("DELETE FROM tasks WHERE id=?", (missing,))
    gc.sweep_repo(str(repo))
    assert not wt.exists()
    assert orphan.exists()
    tombstones = list((kanban_home / "archive" / "kanban-orphans").glob("*.json"))
    assert len(tombstones) == 1
    assert json.loads(tombstones[0].read_text())["path"] == str(orphan)
    gc.sweep_repo(str(repo))
    assert len(list(tombstones[0].parent.glob("*.json"))) == 1


def test_retention_config_and_startup_entry(kanban_home, repo, monkeypatch):
    from hermes_cli import kanban_worktree_gc as gc, worktree_ops
    config = kanban_home / "config.yaml"
    config.write_text("kanban:\n  worktree_retention_days: 30\n")
    assert gc.retention_days() == 30
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        _local_commit(wt)
        _age_task(conn, tid)
    monkeypatch.setattr(worktree_ops, "_prune_stale_worktree_trees", lambda *a: None)
    monkeypatch.setattr(worktree_ops, "_reclaim_orphan_install_states", lambda: None)
    worktree_ops._prune_stale_worktrees(str(repo))
    assert wt.exists()
    config.write_text("kanban:\n  worktree_retention_days: 7\n")
    worktree_ops._prune_stale_worktrees(str(repo))
    assert not wt.exists()


def test_startup_unreadable_board_preserves_everything(kanban_home, repo, monkeypatch):
    from hermes_cli import kanban_worktree_gc as gc
    monkeypatch.setattr(gc, "retention_days", lambda: 7)
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        _local_commit(wt)
        _age_task(conn, tid)
    bad = kanban_home / "kanban" / "boards" / "broken" / "kanban.db"
    bad.parent.mkdir(parents=True)
    bad.write_text("not sqlite")
    gc.sweep_repo(str(repo))
    assert wt.exists()
    assert not (kanban_home / "archive" / "kanban-orphans").exists()


def _running_scratch_parent(conn, title: str = "parent") -> tuple[str, Path]:
    tid = kb.create_task(conn, title=title)
    assert kb.claim_task(conn, tid) is not None  # -> running
    ws = Path(kbw.resolve_workspace(kb.get_task(conn, tid)))
    kbw.set_workspace_path(conn, tid, ws)
    return tid, ws


@pytest.mark.parametrize("live_status", ["running", "blocked", "review"])
def test_live_parent_scratch_survives_last_child_archived(
    kanban_home: Path, live_status: str
) -> None:
    """#133501: the deferred sweep must not reap a still-live parent's scratch.

    ``_try_cleanup_parent_workspaces`` (#33774) exists for a parent that is
    already terminal while children still need its handoff files. A parent
    still running/blocked/review owns its workspace until its own terminal
    transition, so archiving its last active child must not delete it.
    """
    with kbc.connect_closing() as conn:
        parent, ws = _running_scratch_parent(conn)
        (ws / "work.txt").write_text("in progress", encoding="utf-8")
        child = kb.create_task(conn, title="probe")
        kb.link_tasks(conn, parent, child)
        if live_status == "blocked":
            assert kb.block_task(conn, parent, reason="waiting")
        elif live_status == "review":
            assert kb.request_review(conn, parent, summary="please review", force=True)
        assert kb.archive_task(conn, child)
        assert kb.get_task(conn, parent).status == live_status
    assert (ws / "work.txt").is_file()


def test_live_parent_worktree_survives_last_child_archived(
    kanban_home: Path, repo: Path
) -> None:
    """#133501, worktree flavour: a running parent's clean linked worktree —
    its worker's cwd — must not be removed because a child got archived."""
    with kbc.connect_closing() as conn:
        parent, parent_wt = _worktree_task(conn, repo, title="live parent")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (parent,))
        assert kb.claim_task(conn, parent, claimer="worker") is not None
        child = kb.create_task(conn, title="probe")
        kb.link_tasks(conn, parent, child)
        assert kb.archive_task(conn, child)
        assert kb.get_task(conn, parent).status == "running"
    assert parent_wt.is_dir()


def test_terminal_scratch_parent_still_swept_after_last_child(
    kanban_home: Path,
) -> None:
    """#33774 unchanged by the #133501 guard: a parent that finished while a
    child was still active still gets its scratch reaped by the last child's
    completion."""
    with kbc.connect_closing() as conn:
        parent, ws = _running_scratch_parent(conn)
        (ws / "handoff.txt").write_text("x", encoding="utf-8")
        child = kb.create_task(conn, title="probe")
        kb.link_tasks(conn, parent, child)
        assert kb.complete_task(conn, parent, summary="parent done")
        assert ws.is_dir()  # live child still needs the handoff files
        assert kb.archive_task(conn, child)
    assert not ws.exists()


# ---------------------------------------------------------------------------
# Non-board folders under boards/ (kanban-e808885f9e): ``boards/_archived/``
# holds archived boards, and a stray glob (``sqlite3 boards/*/kanban.db``)
# created an empty ``_archived/kanban.db`` there. That file is not a board and
# must not stop GC; a REAL board that can't be read still fails closed.
# ---------------------------------------------------------------------------


def _junk_board_files(kanban_home: Path) -> list[Path]:
    made = []
    for name in ("_archived", "_junk", "Upper"):
        db = kanban_home / "kanban" / "boards" / name / "kanban.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.touch()  # 0 bytes: opens as an sqlite db with no tables
        made.append(db)
    return made


def _gc_abort_cards(home: Path) -> list:
    with kbc.connect_closing() as conn:
        return conn.execute(
            "SELECT id, status, body FROM tasks WHERE idempotency_key='kanban-worktree-gc-abort'"
        ).fetchall()


def test_named_board_files_skip_non_slug_folders(kanban_home):
    from hermes_cli.kanban_db_boards import named_board_db_files
    _junk_board_files(kanban_home)
    real = kanban_home / "kanban" / "boards" / "runners" / "kanban.db"
    real.parent.mkdir(parents=True)
    real.touch()
    (kanban_home / "kanban" / "boards" / "nodb").mkdir()
    assert named_board_db_files() == [("runners", real)]


def test_startup_sweep_ignores_non_board_folders(kanban_home, repo, monkeypatch):
    from hermes_cli import kanban_worktree_gc as gc
    monkeypatch.setattr(gc, "retention_days", lambda: 7)
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        _local_commit(wt)
        _age_task(conn, tid, "archived")
        missing, orphan = _worktree_task(conn, repo)
        conn.execute("DELETE FROM task_events WHERE task_id=?", (missing,))
        conn.execute("DELETE FROM tasks WHERE id=?", (missing,))
    _junk_board_files(kanban_home)
    gc.sweep_repo(str(repo))
    assert not wt.exists()  # reaped as normal
    assert orphan.exists()
    assert len(list((kanban_home / "archive" / "kanban-orphans").glob("*.json"))) == 1
    assert _gc_abort_cards(kanban_home) == []


def test_workspace_sharing_ignores_non_board_folders(kanban_home, tmp_path):
    _junk_board_files(kanban_home)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t")
        assert kbw._workspace_in_use_by_other(conn, tid, tmp_path / "ws") is None


def test_workspace_sharing_real_board_without_tables_fails_closed(kanban_home, tmp_path):
    bad = kanban_home / "kanban" / "boards" / "broken" / "kanban.db"
    bad.parent.mkdir(parents=True)
    bad.touch()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="t")
        assert kbw._workspace_in_use_by_other(conn, tid, tmp_path / "ws") == "unknown"


def test_startup_real_board_without_tables_aborts_and_opens_one_card(kanban_home, repo, monkeypatch):
    from hermes_cli import kanban_worktree_gc as gc
    monkeypatch.setattr(gc, "retention_days", lambda: 7)
    with kbc.connect_closing() as conn:
        tid, wt = _worktree_task(conn, repo)
        _local_commit(wt)
        _age_task(conn, tid, "archived")
    bad = kanban_home / "kanban" / "boards" / "broken" / "kanban.db"
    bad.parent.mkdir(parents=True)
    bad.touch()
    gc.sweep_repo(str(repo))
    assert wt.exists()  # fail-closed: nothing reaped
    gc.sweep_repo(str(repo))
    cards = _gc_abort_cards(kanban_home)
    assert len(cards) == 1
    assert str(bad.resolve()) in cards[0]["body"]


def test_unknown_cleanup_deferral_opens_one_card(kanban_home):
    bad = kanban_home / "kanban" / "boards" / "broken" / "kanban.db"
    bad.parent.mkdir(parents=True)
    bad.touch()
    with kbc.connect_closing() as conn:
        tid, ws = _running_scratch_parent(conn)
        assert kb.complete_task(conn, tid, summary="done")
        assert ws.is_dir()  # deferred: the broken board might share it
        other, _ws2 = _running_scratch_parent(conn, title="second")
        assert kb.complete_task(conn, other, summary="done")
    cards = _gc_abort_cards(kanban_home)
    assert len(cards) == 1
    assert str(bad.resolve()) in cards[0]["body"]


def test_gc_abort_card_comments_once_then_reopens_after_done(kanban_home):
    from hermes_cli import kanban_gc_alert
    first = kanban_gc_alert.report(None, "/x/kanban.db", "boom")
    assert kanban_gc_alert.report(None, "/x/kanban.db", "boom") == first  # within the hour: no new comment
    assert kanban_gc_alert.report(None, "/y/kanban.db", "boom") == first  # another path: one comment
    with kbc.connect_closing() as conn:
        bodies = [r[0] for r in conn.execute("SELECT body FROM task_comments WHERE task_id=?", (first,))]
        assert len(bodies) == 1 and "/y/kanban.db" in bodies[0]
        assert kb.complete_task(conn, first, summary="fixed")
    again = kanban_gc_alert.report(None, "/x/kanban.db", "boom")
    assert again != first  # a recurrence after the fix is a new, visible card
    assert [c["id"] for c in _gc_abort_cards(kanban_home) if c["status"] != "done"] == [again]


def _busy_board_and_other(kanban_home, repo, monkeypatch):
    """Default board write-locked by another connection; a named board holds an aged done tree."""
    import sqlite3
    from hermes_cli import kanban_worktree_gc as gc
    monkeypatch.setattr(gc, "retention_days", lambda: 7)
    monkeypatch.setattr(kbc, "_BUSY_MAX_RETRIES", 0)
    with kbc.connect_closing() as conn:
        busy_tid, busy_wt = _worktree_task(conn, repo, "busy-board tree")
        _age_task(conn, busy_tid)
    other_db = kanban_home / "kanban" / "boards" / "other" / "kanban.db"
    other_db.parent.mkdir(parents=True)
    kb.init_db(other_db)
    with kbc.connect_closing(other_db) as conn:
        other_tid, other_wt = _worktree_task(conn, repo, "other-board tree")
        _age_task(conn, other_tid)
    holder = sqlite3.connect(str(kanban_home / "kanban.db"), timeout=0, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    return gc, holder, busy_wt, other_wt


def test_startup_sweep_skips_a_busy_board_and_cleans_the_rest(kanban_home, repo, monkeypatch, caplog):
    import logging
    gc, holder, busy_wt, other_wt = _busy_board_and_other(kanban_home, repo, monkeypatch)
    try:
        with caplog.at_level(logging.INFO, logger=gc.log.name):
            gc.sweep_repo(str(repo))
    finally:
        holder.rollback()
        holder.close()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], caplog.text
    assert "skipped busy board" in caplog.text
    assert busy_wt.exists()  # the locked board is left alone this pass
    assert not other_wt.exists()  # the other board is still cleaned
    gc.sweep_repo(str(repo))  # lock released: next pass cleans it and resets the streak
    assert not busy_wt.exists()
    assert json.loads((kanban_home / "gc-busy-skips.json").read_text()) == {}


def test_startup_sweep_alerts_once_after_three_busy_skips(kanban_home, repo, monkeypatch):
    from hermes_cli import kanban_gc_alert
    calls = []
    monkeypatch.setattr(kanban_gc_alert, "report", lambda conn, db, detail: calls.append((db, detail)))
    gc, holder, busy_wt, _other = _busy_board_and_other(kanban_home, repo, monkeypatch)
    (kanban_home / "gc-busy-skips.json").write_text("[1, 2]")  # unreadable shape: restart the count
    try:
        for _ in range(2):
            gc.sweep_repo(str(repo))
        assert calls == []
        gc.sweep_repo(str(repo))
    finally:
        holder.rollback()
        holder.close()
    assert len(calls) == 1
    assert calls[0][0] == str((kanban_home / "kanban.db").resolve())
    assert "3" in calls[0][1] and "busy" in calls[0][1]
    assert busy_wt.exists()


def test_startup_sweep_non_busy_mutation_error_still_opens_the_abort_card(kanban_home, repo, monkeypatch):
    import sqlite3
    from hermes_cli import kanban_gc_alert
    from hermes_cli import kanban_worktree_gc as gc
    monkeypatch.setattr(gc, "retention_days", lambda: 7)
    calls = []
    monkeypatch.setattr(kanban_gc_alert, "report", lambda conn, db, detail: calls.append((db, detail)))
    def broken(conn, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(gc, "gc_task_worktrees", broken)
    gc.sweep_repo(str(repo))
    assert len(calls) == 1 and "disk I/O error" in calls[0][1]
    assert not (kanban_home / "gc-busy-skips.json").exists()


def test_startup_sweep_clean_pass_writes_no_busy_state(kanban_home, repo, monkeypatch):
    from hermes_cli import kanban_worktree_gc as gc
    monkeypatch.setattr(gc, "retention_days", lambda: 7)
    (kanban_home / "gc-busy-skips.json").write_text("[1, 2]")  # unreadable shape: treated as empty
    gc.sweep_repo(str(repo))
    assert (kanban_home / "gc-busy-skips.json").read_text() == "[1, 2]"
    (kanban_home / "gc-busy-skips.json").unlink()
    gc.sweep_repo(str(repo))
    assert not (kanban_home / "gc-busy-skips.json").exists()
