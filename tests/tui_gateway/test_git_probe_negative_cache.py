"""A non-repo cwd is not re-probed every 30 s; ``git init`` there is still seen at once."""
import os

from tui_gateway import git_probe


def _counting(monkeypatch, answers):
    calls = []

    def fake(cwd, *args):
        calls.append((cwd, args))
        return answers.get(cwd, "")

    monkeypatch.setattr(git_probe, "run_git", fake)
    return calls


def test_unchanged_non_repo_dir_is_not_reprobed_after_old_30s_ttl(tmp_path, monkeypatch):
    git_probe.invalidate()
    calls = _counting(monkeypatch, {})
    clock = [1000.0]
    monkeypatch.setattr(git_probe.time, "monotonic", lambda: clock[0])
    cwd = str(tmp_path)
    assert git_probe.repo_root(cwd) == ""
    clock[0] += 31  # past the old 30 s negative TTL: idle Desktop polls re-spawned git here
    assert git_probe.repo_root(cwd) == ""
    assert len(calls) == 1


def test_git_init_in_cwd_is_seen_without_waiting_for_ttl(tmp_path, monkeypatch):
    git_probe.invalidate()
    answers = {}
    calls = _counting(monkeypatch, answers)
    cwd = str(tmp_path)
    assert git_probe.repo_root(cwd) == ""
    (tmp_path / ".git").mkdir()
    st = os.stat(cwd)
    os.utime(cwd, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    answers[cwd] = cwd
    assert git_probe.repo_root(cwd) == cwd
    assert len(calls) == 2


def test_negative_still_expires_after_ttl(tmp_path, monkeypatch):
    git_probe.invalidate()
    calls = _counting(monkeypatch, {})
    clock = [1000.0]
    monkeypatch.setattr(git_probe.time, "monotonic", lambda: clock[0])
    cwd = str(tmp_path)
    git_probe.repo_root(cwd)
    clock[0] += git_probe._NEG_TTL + 1
    git_probe.repo_root(cwd)
    assert len(calls) == 2
