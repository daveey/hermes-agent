"""Functional test: fork with own commits + updates.fork_merge_upstream hands a synthesized merge to the CI gate.

The merge never moves the live checkout and never reaches the fork's main untested: the real gate script
(scripts/ci/upstream_sync.py, in a subprocess) stages it on sync/upstream-<sha>, refuses it red, lands it green.
"""
import subprocess
from unittest import mock

import pytest

import hermes_cli.update_cmd_git as g
from tests.hermes_cli.upstream_gate_fake import FakeFork


def sh(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    up = tmp_path / "up"; up.mkdir(); sh("git", "init", "-q", "-b", "main", cwd=up)
    for k, v in {"user.email": "t@t", "user.name": "t"}.items(): sh("git", "config", k, v, cwd=up)
    (up / "a.txt").write_text("a\n"); (up / "b.txt").write_text("b\n")
    sh("git", "add", "-A", cwd=up); sh("git", "commit", "-qm", "base", cwd=up)
    fork = tmp_path / "fork.git"; sh("git", "clone", "-q", "--bare", str(up), str(fork))
    gate = FakeFork(tmp_path, monkeypatch, fork)
    live = tmp_path / "live"; sh("git", "clone", "-q", str(fork), str(live))
    for k, v in {"user.email": "t@t", "user.name": "t"}.items(): sh("git", "config", k, v, cwd=live)
    sh("git", "remote", "add", "upstream", str(up), cwd=live)
    (live / "a.txt").write_text("a fork\n"); sh("git", "commit", "-qam", "fork change", cwd=live); sh("git", "push", "-q", "origin", "main", cwd=live)
    gate.adopt(live, up)
    return up, fork, live, gate


def run(live, merge_flag, upstream_edit=None):
    up = live.parent / "up"
    if upstream_edit:
        (up / upstream_edit[0]).write_text(upstream_edit[1]); sh("git", "commit", "-qam", "upstream change", cwd=up)
    with mock.patch.object(g, "_fork_merges_upstream", return_value=merge_flag), \
         mock.patch("hermes_cli.update_cmd_commit.arm_commit_point", return_value=None), \
         mock.patch("hermes_cli.update_cmd_commit.target_syntax_error", return_value=None), \
         mock.patch("hermes_cli.update_cmd._should_skip_upstream_prompt", return_value=False):
        ok = g._sync_with_upstream_if_needed(["git"], live, assume_yes=True)
        if ok: g._push_synced_fork(["git"], live)
    return ok


def test_flag_off_leaves_fork_untouched(repo):
    _up, _fork, live, gate = repo; pre = sh("git", "rev-parse", "HEAD", cwd=live)
    assert run(live, False, ("b.txt", "b up\n"))
    assert sh("git", "rev-parse", "HEAD", cwd=live) == pre
    assert gate.staged() == []


@pytest.mark.parametrize("verdict", ["failure", "success"])
def test_flag_on_stages_the_merge_and_lands_only_a_green_one(repo, capsys, verdict):
    _up, _fork, live, gate = repo
    pre = sh("git", "rev-parse", "HEAD", cwd=live)
    assert run(live, True, ("b.txt", "b up\n"))
    # Staged for the fork's full suite; neither the live tree nor the fork's main moved.
    [merged] = gate.staged()
    assert sh("git", "rev-parse", "HEAD", cwd=live) == pre
    assert gate.main() == pre
    assert len(sh("git", "rev-list", "--parents", "-n1", merged, cwd=live).split()) == 3
    assert sh("git", "show", f"{merged}:b.txt", cwd=live) == "b up" and sh("git", "show", f"{merged}:a.txt", cwd=live) == "a fork"
    assert "staged" in capsys.readouterr().out

    gate.finish(merged, verdict)
    assert run(live, True)  # the next update
    out = capsys.readouterr().out
    if verdict == "failure":
        assert gate.main() == pre and "LANDED" not in out
        assert "full suite failed" in out or "failed its full suite" in out, out
    else:
        assert gate.landed(merged) and "LANDED" in out, out
        assert gate.staged() == []  # the twin merge of the next update is not staged again
    assert sh("git", "rev-parse", "HEAD", cwd=live) == pre
    assert sh("git", "status", "--porcelain", cwd=live) == ""


def test_conflict_moves_nothing(repo):
    _up, fork, live, gate = repo; pre = sh("git", "rev-parse", "HEAD", cwd=live)
    assert not run(live, True, ("a.txt", "a upstream conflict\n"))
    assert sh("git", "rev-parse", "HEAD", cwd=live) == pre
    assert sh("git", "rev-parse", "main", cwd=fork) == pre
    assert sh("git", "status", "--porcelain", cwd=live) == ""
    assert gate.staged() == []
