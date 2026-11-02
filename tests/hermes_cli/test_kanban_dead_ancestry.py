"""Real-git regression: a rebased PR head must not leave a park asleep."""
import shlex
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as kwc


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def history(tmp_path):
    repo = tmp_path / "repo with spaces"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "base").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "feature")
    (repo / "feature").write_text("patch\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "feature")
    old = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "main")
    (repo / "independent").write_text("main advanced\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "advance")
    git(repo, "cherry-pick", old)
    landed = git(repo, "rev-parse", "HEAD")
    assert old != landed
    git(repo, "update-ref", "refs/remotes/origin/main", landed)
    git(repo, "checkout", "-b", "unmerged", base)
    (repo / "other").write_text("unmerged\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "other")
    unmerged = git(repo, "rev-parse", "HEAD")
    return repo, old, landed, unmerged


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn
    kwc._shutdown_for_tests()


def park(conn, cmd):
    tid = kb.create_task(conn, title="ancestry fixture", assignee="worker")
    assert kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z",
                            wake_check=cmd, check_every=120)
    return tid


@pytest.mark.parametrize("target", ["old", "landed", "unmerged", "absent", "non-ancestry"])
def test_scheduler_rebased_head_and_controls(board, history, target):
    repo, old, landed, unmerged = history
    sha = {"old": old, "landed": landed, "unmerged": unmerged,
           "absent": "a" * 40, "non-ancestry": old}[target]
    # Keep HEAD behind both feature identities, including the unmerged control.
    git(repo, "checkout", "--detach", "HEAD~1")
    cmd = f"git -C {shlex.quote(str(repo))} merge-base --is-ancestor {sha} HEAD"
    if target == "absent":
        cmd += " 2>/dev/null || exit 1"
    elif target == "non-ancestry":
        cmd = "test -f /no-such-ancestry-fixture"
    tid = park(board, cmd)
    now = int(time.time())
    kwc.run_due_wake_checks(board, now=now, wait=True)
    if target == "old":
        assert kb.get_task(board, tid).status == "ready"
        comments = kb.list_comments(board, tid)
        assert any(old in c.body and landed in c.body for c in comments)
    else:
        assert kb.get_task(board, tid).status == "scheduled"
        assert kwc.get_state(board, tid)["fails"] == 0
    before = kb.list_comments(board, tid)
    kwc.run_due_wake_checks(board, now=now + 121, wait=True)
    assert kb.list_comments(board, tid) == before
    if target == "old":
        assert kb.schedule_task(board, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z",
                                wake_check=cmd, check_every=120)
        kwc.run_due_wake_checks(board, now=now + 242, wait=True)
        assert kb.get_task(board, tid).status == "scheduled"
        assert kb.list_comments(board, tid) == before


def test_detector_default_branch_and_unknowns(history, tmp_path, monkeypatch):
    from hermes_cli import kanban_dead_ancestry as dead

    repo, old, landed, _ = history
    cmd = f"git merge-base --is-ancestor {old[:12]} HEAD"
    assert dead.detect(cmd, str(repo))[0]["landed"] == landed
    git(repo, "update-ref", "refs/remotes/origin/trunk", landed)
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    assert dead.detect(cmd, str(repo))[0]["upstream"] == "refs/remotes/origin/trunk"
    monkeypatch.setattr(dead, "BUDGET_SECONDS", 0)
    assert dead.detect(cmd, str(repo)) == []
    monkeypatch.setattr(dead, "BUDGET_SECONDS", 10)
    git(repo, "update-ref", "-d", "refs/remotes/origin/trunk")
    assert dead.detect(cmd, str(repo)) == []
    assert dead.detect(cmd, str(tmp_path)) == []


@pytest.mark.parametrize("template", [
    "echo 'git merge-base --is-ancestor {sha} HEAD'",
    "cd elsewhere && git merge-base --is-ancestor {sha} HEAD",
    "git -C $REPO merge-base --is-ancestor {sha} HEAD",
    "bash -c 'git merge-base --is-ancestor {sha} HEAD'",
    "git -C 'unterminated merge-base --is-ancestor {sha} HEAD",
])
def test_parser_does_not_guess_shell_state(history, template):
    from hermes_cli.kanban_dead_ancestry import detect

    repo, old, _, _ = history
    assert detect(template.format(sha=old), str(repo)) == []


def test_parser_multiple_and_relative_paths(history):
    from hermes_cli.kanban_dead_ancestry import detect

    repo, old, landed, _ = history
    cmd = (f"git -C {shlex.quote(repo.name)} merge-base --is-ancestor {old} HEAD; "
           f"git -C {shlex.quote(str(repo))} merge-base --is-ancestor {old[:12]} HEAD")
    hits = detect(cmd, str(repo.parent))
    assert len(hits) == 1
    assert hits[0]["landed"] == landed


def test_stale_generation_does_not_wake(board, history):
    from hermes_cli.kanban_dead_ancestry import detect

    repo, old, _, _ = history
    cmd = f"git -C {shlex.quote(str(repo))} merge-base --is-ancestor {old} HEAD"
    tid = park(board, cmd)
    hits = detect(cmd, str(repo))
    assert hits
    assert kwc.apply_result(board, tid, cmd, 1, "", generation="stale", dead_ancestry=hits) is None
    assert kb.get_task(board, tid).status == "scheduled"
    assert kb.list_comments(board, tid) == []
