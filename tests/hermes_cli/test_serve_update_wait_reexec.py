"""[daveey] A serve that waited out a core update must run and report only the new commit.

David-MBP 2026-10-28: Desktop serve pid 50733 started at 07:02:38 during an update. Before its
update gate ran it had imported ``hermes_cli.main``/``config``/the provider plugins and cached
``get_version_info()`` (commit cc29d783ed: the ``source: git`` stamp still matched HEAD). The
checkout moved to 42ea812e27 at 07:02:50, the stamp followed at 07:03:36, the gate cleared at
07:03:40 and the serve started anyway, reporting ``commit: cc29d783ed`` on ``/api/health`` while
the code_skew boot fingerprint (read after the wait) said 42ea812e27.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli import serve_update_wait as suw

REPO = Path(__file__).resolve().parents[2]
OLD = "cc29d783ed4fe3aef41b5b84cc2f4e8224b084ee"
NEW = "42ea812e27cc380c1db74d39ecbf87b98eda5617"

# One serve process, run for real through cmd_dashboard: everything but the update gate, the
# re-exec, code_skew and /api/health is stubbed. The checkout HEAD, the install stamp and the
# update marker are files in ``state`` so the test can move them while the serve waits.
_CHILD = textwrap.dedent("""
    import asyncio, json, os, sys, threading, time, types
    from pathlib import Path

    repo, state = sys.argv[1], Path(sys.argv[2])
    sys.path.insert(0, repo)
    head, stamp, marker = state / "HEAD", state / "stamp", state / "marker"
    with open(state / "incarnations", "a") as f:
        f.write(f"{os.getpid()}\\n")

    def fingerprint(_root=None):
        return "git:refs/heads/main:" + head.read_text().strip()

    import hermes_cli.main as main_mod
    main_mod._read_git_revision_fingerprint = fingerprint

    import hermes_cli.version_info as vi

    def info(commit):
        return vi.VersionInfo("0.0.0", "0.0.0", 0, commit, "main", "git", False, None)

    def stamp_info():  # install-stamp.json, source: git -> trusted only while it names HEAD
        commit = stamp.read_text().strip()
        return info(commit) if commit == head.read_text().strip() else None

    vi._stamp_version_info = stamp_info
    vi._resolve_repo_dir = lambda: Path(repo)
    vi._git_version_info = lambda _d: info(head.read_text().strip())
    vi._reset_version_info_cache()  # importing main already cached the real tree's identity:
    vi.get_version_info()  # cache it from the fake HEAD, as the fireworks plugin import does

    import hermes_cli.serve_update_wait as suw
    suw.update_live = marker.exists
    suw.admission_hold_present = lambda: False
    if marker.exists():  # the update: checkout move, then the stamp, then the marker release
        def update():
            time.sleep(0.3); head.write_text(sys.argv[3])
            time.sleep(0.3); stamp.write_text(sys.argv[3])
            time.sleep(0.2); marker.unlink()
        threading.Thread(target=update, daemon=True).start()

    for name in ("_dashboard_lifecycle_flags", "_dashboard_sanitize_desktop_env", "_attach_to_host_backend",
                 "_route_named_profile_dashboard", "_maybe_setup_dashboard_auth_interactively"):
        setattr(main_mod, name, lambda *a, **k: None)
    main_mod._dashboard_validate_serve_args = lambda *a, **k: "nonce"
    main_mod._dashboard_prepare_runtime = lambda *a, **k: False
    main_mod._ssh_bootstrap_server_kwargs = lambda *a, **k: {}

    def read_token(_path):
        with open(state / "token_reads", "a") as f:
            f.write(f"{os.getpid()}\\n")
        return "tok"

    main_mod._read_ssh_session_token_file = read_token
    import hermes_cli.resource_limits as rl
    rl.apply_nofile_soft_limit = lambda: None
    import hermes_cli.observability.shared_metrics_process as smp
    smp.begin_process = lambda *a, **k: None

    def start_server(**_kw):
        from gateway import code_skew
        code_skew.record_boot_fingerprint()  # what the web_server lifespan does
        from hermes_cli.web_routers import status
        health = asyncio.run(status.get_health())
        (state / "result.json").write_text(json.dumps({
            "health_commit": health["commit"], "boot": code_skew._boot_fingerprint,
            "head": head.read_text().strip(), "pid": os.getpid()}))

    web = types.ModuleType("hermes_cli.web_server")
    web.start_server = start_server
    web.app = types.SimpleNamespace(state=types.SimpleNamespace(auth_required=False))
    sys.modules["hermes_cli.web_server"] = web

    main_mod.cmd_dashboard(types.SimpleNamespace(
        ssh_session_token_file=str(state / "token"), headless_backend=True, isolated=True,
        host="127.0.0.1", port=0, no_open=True, insecure=False, open_profile=""))
""")


def _run_serve(tmp_path, *, update_running: bool, new: str = NEW):
    state = tmp_path / "state"
    state.mkdir()
    (state / "HEAD").write_text(OLD)
    (state / "stamp").write_text(OLD)
    if update_running:
        (state / "marker").write_text("update")
    script = tmp_path / "serve_child.py"
    script.write_text(_CHILD)
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "HERMES_SERVE_REEXEC_COUNT"}
    env.update(HERMES_HOME=str(home), HERMES_SERVE_UPDATE_POLL_S="0.05")
    proc = subprocess.run([sys.executable, str(script), str(REPO), str(state), new],
                          env=env, capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr[-4000:]
    result = json.loads((state / "result.json").read_text())
    incarnations = (state / "incarnations").read_text().split()
    token_reads = (state / "token_reads").read_text().split()
    return result, incarnations, token_reads, proc.stderr


@pytest.mark.skipif(os.name == "nt", reason="re-exec in place is POSIX only")
def test_serve_that_waited_out_an_update_reports_the_commit_it_runs(tmp_path):
    result, incarnations, token_reads, stderr = _run_serve(tmp_path, update_running=True)

    assert result["head"] == NEW
    assert result["boot"].rsplit(":", 1)[-1] == NEW, "code_skew boot fingerprint is not HEAD"
    assert result["health_commit"] == NEW, (
        f"/api/health reports {result['health_commit'][:10]} after the checkout moved to {NEW[:10]}")
    # In place: same pid (Desktop's READY pipe and process handle stay valid), one re-exec,
    # and the one-shot SSH token was read once, by the new image only.
    assert len(incarnations) == 2 and len(set(incarnations)) == 1 == len(set(token_reads))
    assert token_reads == incarnations[:1]
    assert "re-executing" in stderr


@pytest.mark.skipif(os.name == "nt", reason="re-exec in place is POSIX only")
def test_serve_with_no_update_does_not_reexec(tmp_path):
    result, incarnations, token_reads, stderr = _run_serve(tmp_path, update_running=False)
    assert result["health_commit"] == result["head"] == OLD
    assert len(incarnations) == 1 and len(token_reads) == 1
    assert "re-executing" not in stderr


# ── the decision itself ───────────────────────────────────────────────────────────────────────

FP_OLD, FP_NEW = f"git:refs/heads/main:{OLD}", f"git:refs/heads/main:{NEW}"


def _decide(result, launch, current, cached, monkeypatch, count=None):
    if count is None:
        monkeypatch.delenv(suw.REEXEC_ENV, raising=False)
    else:
        monkeypatch.setenv(suw.REEXEC_ENV, str(count))
    calls, lines = [], []
    out = suw.reexec_if_checkout_moved(result, launch, current_fingerprint=lambda: current,
                                       cached_commit=lambda: cached,
                                       execv=lambda exe, argv: calls.append((exe, argv)),
                                       log=lines.append)
    return out, calls, lines


@pytest.mark.parametrize("launch,cached", [(FP_OLD, OLD), (FP_OLD, None), (FP_NEW, OLD)])
def test_checkout_moved_or_identity_stale_reexecs(monkeypatch, launch, cached):
    monkeypatch.setattr(os, "name", "posix")
    out, calls, lines = _decide("cleared", launch, FP_NEW, cached, monkeypatch)
    assert out is True and len(calls) == 1
    assert calls[0][0] == sys.executable
    assert os.environ[suw.REEXEC_ENV] == "1"
    assert any("re-executing" in line for line in lines)


@pytest.mark.parametrize("result", ["clear", "update-timeout", "hold-timeout"])
def test_no_reexec_unless_the_wait_saw_the_update_finish(monkeypatch, result):
    out, calls, _ = _decide(result, FP_OLD, FP_NEW, OLD, monkeypatch)
    assert out is False and calls == []


def test_no_reexec_when_nothing_moved_or_no_checkout(monkeypatch):
    assert _decide("cleared", FP_NEW, FP_NEW, NEW, monkeypatch)[:2] == (False, [])
    assert _decide("cleared", FP_NEW, FP_NEW, None, monkeypatch)[:2] == (False, [])
    assert _decide("cleared", None, None, OLD, monkeypatch)[:2] == (False, [])


def test_reexec_is_capped_and_the_counter_never_leaks(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    out, calls, lines = _decide("cleared", FP_OLD, FP_NEW, OLD, monkeypatch, count=suw.MAX_REEXECS)
    assert out is False and calls == []
    assert any("starting anyway" in line for line in lines)
    assert suw.REEXEC_ENV not in os.environ
    _decide("clear", FP_NEW, FP_NEW, NEW, monkeypatch, count=1)
    assert suw.REEXEC_ENV not in os.environ
