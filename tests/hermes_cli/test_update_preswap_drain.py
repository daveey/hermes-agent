"""A busy pre-swap drain must not trap Desktop behind the update fence."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_cli import update_cmd_posix_pause as pause
from hermes_cli import update_pause_record as record


@pytest.mark.platforms("posix")
def test_long_drain_refuses_without_forcing_active_work(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(pause, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds)))
    monkeypatch.setattr(pause, "_drain_budget", lambda: 1885.0)
    monkeypatch.setattr(pause, "_ask_to_drain", lambda entry: True)
    monkeypatch.setattr(pause, "_alive", lambda *args: True)
    monkeypatch.setattr(record, "mark_stop_sent", lambda *args: None)
    stop = Mock()
    monkeypatch.setattr(pause, "_stop_at_once", stop)
    entry = {"pid": 4242, "kind": "systemd", "unit": "hermes-gateway-test.service"}
    with pytest.raises(pause.PauseRefused, match="drain"):
        pause._stop_gateways({}, [entry])
    assert now[0] <= 31.0
    stop.assert_not_called()


@pytest.mark.platforms("posix")
def test_resume_retains_live_drain_debt_and_starts_only_exited_units(monkeypatch):
    live = {"pid": 4242, "kind": "systemd", "scope": "user", "unit": "live.service"}
    dead = {"pid": 4243, "kind": "systemd", "scope": "user", "unit": "dead.service"}
    token = {**pause._empty_token(), "resume_needed": True, "posix_stopped": True,
             "posix_units": [live, dead], "identities": {"4242": "live", "4243": "dead"}}
    monkeypatch.setattr(record, "identity_is_live", lambda identity: identity["ct"] == "live")
    monkeypatch.setattr(pause, "_root_first", lambda entries: (entries, None))
    monkeypatch.setattr(pause, "_RESTARTED", {"units": set(), "labels": set(), "pids": set()})
    start = Mock(return_value=4244)
    monkeypatch.setattr(pause, "_start_unit", start)
    pause.resume_paused_set(token)
    start.assert_called_once_with(dead)
    assert token["posix_units"] == [live]
    assert token["resume_needed"] is True


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("failure", [pause.PauseRefused("still draining"), KeyboardInterrupt()])
def test_pause_failure_never_arms_a_tree_move_and_settles_under_fence(tmp_path, monkeypatch, failure):
    from hermes_cli import update_cmd_commit as commit, update_cmd_windows as windows
    from gateway import status

    token = pause._empty_token()
    entry = {"pid": 4242, "kind": "systemd", "unit": "test.service", "home": "/fixture"}
    monkeypatch.setattr(pause, "_RUN", token)
    monkeypatch.setattr(pause, "_discover_systemd", lambda: [entry])
    monkeypatch.setattr(pause, "_discover_launchd", list)
    monkeypatch.setattr(pause, "_left_running", lambda units, jobs: (units, jobs, []))
    monkeypatch.setattr(pause, "_discover_bare", lambda pids: ([], []))
    monkeypatch.setattr(status, "get_process_start_time", lambda pid: 1)
    monkeypatch.setattr(record, "identity", lambda pid=None: {"ct": "ct:1"})
    monkeypatch.setattr(record, "record_pause", lambda *args: None)
    monkeypatch.setattr(record, "mark_stop_requested", lambda *args, **kwargs: None)
    monkeypatch.setattr(record, "sync", lambda token: None)
    monkeypatch.setattr(pause, "_stop_gateways", Mock(side_effect=failure))
    resume = Mock()
    monkeypatch.setattr(windows, "_resume_windows_gateways_after_update", resume)
    arm = Mock()
    monkeypatch.setattr(commit, "arm_commit_obligations", arm)
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            commit.arm_commit_point([], tmp_path, "a" * 40)
    else:
        assert "drain" in (commit.arm_commit_point([], tmp_path, "a" * 40) or "")
    arm.assert_not_called()
    resume.assert_called_once_with(token)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("keep", [True, False])
def test_busy_pause_precedes_stash_or_discard_but_follows_prompt(tmp_path, monkeypatch, keep):
    import subprocess
    from hermes_cli import update_cmd_stash as stash, update_cmd_pause_gate as gate

    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True)

    git("init", "-q")
    source = tmp_path / "code.py"
    source.write_text("before = 1\n")
    git("add", "code.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "baseline")
    before = git("rev-parse", "HEAD").stdout
    source.write_text("dirty = 2\n")
    seen = []
    monkeypatch.setattr(stash, "_ask_keep_local_changes", lambda changes: seen.append("prompt") or keep)
    monkeypatch.setattr(pause, "pause_at_commit_point", lambda: seen.append("pause") or "gateway busy")
    with pytest.raises(SystemExit) as refused:
        stash._stash_local_changes_if_needed(["git"], tmp_path, ask_first=True, checkout_move=gate._moves_for(None))
    assert refused.value.code == 1
    assert seen == ["prompt", "pause"]
    assert source.read_text() == "dirty = 2\n"
    assert git("rev-parse", "HEAD").stdout == before
    assert git("stash", "list").stdout == ""


@pytest.mark.platforms("posix")
def test_unacknowledged_pause_never_forces_a_stop(monkeypatch):
    monkeypatch.setattr(record, "mark_stop_sent", lambda *args: None)
    monkeypatch.setattr(record, "mark_stop_refused", lambda *args: None)
    monkeypatch.setattr(pause, "_ask_to_drain", lambda entry: False)
    stop = Mock()
    monkeypatch.setattr(pause, "_stop_at_once", stop)
    with pytest.raises(pause.PauseRefused, match="busy"):
        pause._stop_gateways({}, [{"pid": 4242, "kind": "systemd", "unit": "busy.service"}])
    stop.assert_not_called()


def test_discovery_failure_refuses_the_tree_move(monkeypatch):
    monkeypatch.setattr(pause, "_discover_systemd", Mock(side_effect=OSError("discovery failed")))
    assert "could not list" in pause._pause(pause._empty_token())


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("mode", ["timeout", "interrupt", "busy"])
def test_command_releases_desktop_fence_after_preswap_refusal(tmp_path, monkeypatch, mode):
    import atexit
    import subprocess
    import sys

    from gateway import status
    from hermes_cli import main, update_cmd, update_cmd_windows as windows, update_lock
    from hermes_cli import update_owning_install
    from hermes_cli.gateway_connections_ssh_programs import REMOTE_MARKER_GATE_PY
    from gateway import control_socket, cron_pause

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-qm", "baseline"], check=True)
    monkeypatch.setattr(main, "PROJECT_ROOT", root)
    monkeypatch.setattr(pause, "_RUN", None)
    monkeypatch.setattr(record, "install_root", lambda: root)
    monkeypatch.setattr(update_owning_install, "retarget_to_owning_install", lambda root: None)
    monkeypatch.setattr(main, "_update_preflight_handled", lambda args: False)
    monkeypatch.setattr(main, "_install_hangup_protection", lambda **kw: None)
    monkeypatch.setattr(main, "_finalize_update_output", lambda state: None)
    monkeypatch.setattr(main, "_finalize_update_receipt", lambda *args: None)
    # A real live process identity: cleanup must not call systemctl start on it.
    import os
    pid = os.getpid()
    entry = {"pid": pid, "kind": "systemd", "scope": "user", "unit": "fixture.service", "home": str(tmp_path)}
    exited = {**entry, "pid": 99999999, "unit": "exited.service"}
    monkeypatch.setattr(pause, "_discover_systemd", lambda: [dict(entry), dict(exited)])
    monkeypatch.setattr(pause, "_discover_launchd", list)
    monkeypatch.setattr(pause, "_left_running", lambda units, jobs: (units, jobs, []))
    monkeypatch.setattr(pause, "_discover_bare", lambda pids: ([], []))
    if mode != "busy":
        monkeypatch.setattr(pause, "_ask_to_drain", lambda entry: True)
    else:
        runner = SimpleNamespace(config=object(), _active_work_count=lambda: 7, request_restart=Mock())
        monkeypatch.setattr(cron_pause, "preserve_cron_slots", Mock())
        def pause_client(home, *, idle_only):
            assert idle_only
            assert desktop_gate().startswith("LIVE:")
            return cron_pause.pause_for_update(
                runner, SimpleNamespace(call_soon_threadsafe=lambda callback: callback()), idle_only=idle_only)
        monkeypatch.setattr(control_socket, "pause_gateway_for_update", pause_client)
    monkeypatch.setattr(pause, "_drain_budget", lambda: 1885.0)
    monkeypatch.setattr(pause, "_RESTARTED", {"units": set(), "labels": set(), "pids": set()})
    def start_unit(unit):
        assert update_lock.holds_checkout_lock(root), "rollback restarted a unit outside the update fence"
        assert unit["pid"] == exited["pid"]
        return 99999998

    start = Mock(side_effect=start_unit)
    stop = Mock()
    monkeypatch.setattr(pause, "_start_unit", start)
    monkeypatch.setattr(pause, "_stop_at_once", stop)
    now = [0.0]
    monkeypatch.setattr(pause, "_disarm", lambda entry: None)
    monkeypatch.setattr(pause, "_root_first", lambda entries: (entries, None))
    marker = update_lock.update_marker_path()

    def desktop_gate():
        got = subprocess.run([sys.executable, "-c", REMOTE_MARKER_GATE_PY, str(marker), "", str(root)],
                             capture_output=True, text=True, encoding="utf-8", timeout=10, check=False)
        return got.stdout.strip().splitlines()[-1]

    def tick(seconds):
        assert desktop_gate().startswith("LIVE:")  # real subprocess sees the held command fence
        if mode == "interrupt":
            raise KeyboardInterrupt
        now[0] += 31.0

    monkeypatch.setattr(pause, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=tick))
    token = None

    def impl(args, gateway_mode=False):
        nonlocal token
        token = pause.arm_pause(no_gateway_restart=False)
        atexit.register(windows._resume_windows_gateways_after_update, token)
        # Real prepare/stash/mutation guard, including a dirty tree at an unchanged HEAD.
        monkeypatch.setattr(update_cmd, "_apply_parked_branch_guard", lambda *args, **kw: (False, True, None))
        update_cmd._prepare_checkout_for_update(
            ["git"], "main", "main", is_fork=True, assume_yes=True, gateway_mode=False,
            gw_input_fn=None, switch_branch=False, target_ref="HEAD", _windows_gateway_resume=token)

    monkeypatch.setattr(update_cmd, "_cmd_update_impl", impl)
    source = root / "source.py"
    source.write_text("before = 1\n")
    subprocess.run(["git", "-C", str(root), "add", "source.py"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "source"], check=True)
    source.write_text("dirty = 2\n")
    with pytest.raises(KeyboardInterrupt if mode == "interrupt" else SystemExit):
        main.cmd_update(SimpleNamespace(gateway=False))
    assert desktop_gate() == "CLEAR"
    assert source.read_text() == "dirty = 2\n"
    assert not (root / ".git" / "hermes-update-pull").exists()
    assert token is not None
    if mode == "busy":
        runner.request_restart.assert_not_called()
        cron_pause.preserve_cron_slots.assert_not_called()
        assert not token["posix_units"]
        assert not token["resume_needed"]
        assert not record.orphans()
        return
    assert token["resume_needed"]
    assert [e["pid"] for e in token["posix_units"]] == [pid]
    assert status.get_process_start_time(pid) is not None
    start.assert_called_once()
    stop.assert_not_called()
    # The updater has exited and the remaining incarnation subsequently exits.
    # Startup recovery must own its durable debt and hold the checkout fence.
    record.write(token, owner=record.UNOWNED)
    monkeypatch.setattr(record, "identity_is_live", lambda identity: False)

    def recover_unit(unit):
        assert update_lock.holds_checkout_lock(root)
        assert unit["pid"] == pid
        return 99999997

    recover_start = Mock(side_effect=recover_unit)
    monkeypatch.setattr(pause, "_start_unit", recover_start)
    record.recover(["doctor"])
    recover_start.assert_called_once()
    assert not record.orphans()


# --- the automated caller's opt-in: update over busy gateways, still bounded -------------------

def _fake_clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(pause, "time", SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds)))
    return now


def _busy_gateway_at_commit_point(tmp_path, monkeypatch, *, active_work=7):
    """One busy systemd gateway behind the real pause, record and control-socket handler.

    Returns ``(order, runner, repo_head, now)``: *order* lists the drain request, the supervisor
    stop and the commit arming as they happen, each stop with the checkout HEAD it saw."""
    import subprocess
    from gateway import control_socket, cron_pause, status
    from hermes_cli import update_cmd_commit as commit

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "--allow-empty", "-qm", "baseline"], check=True)

    def head():
        return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()

    order = []
    runner = SimpleNamespace(config=object(), _active_work_count=lambda: active_work,
                             request_restart=Mock(side_effect=lambda **kw: order.append("drain") or True))
    monkeypatch.setattr(cron_pause, "preserve_cron_slots", Mock())
    inline = SimpleNamespace(call_soon_threadsafe=lambda callback: callback())
    monkeypatch.setattr(control_socket, "pause_gateway_for_update",
                        lambda home, *, idle_only=False, timeout=2.0: cron_pause.pause_for_update(
                            runner, inline, idle_only=idle_only))
    entry = {"pid": 4242, "kind": "systemd", "scope": "user", "unit": "busy.service", "home": str(tmp_path)}
    monkeypatch.setattr(pause, "_RUN", pause._empty_token())
    monkeypatch.setattr(pause, "_discover_systemd", lambda: [dict(entry)])
    monkeypatch.setattr(pause, "_discover_launchd", list)
    monkeypatch.setattr(pause, "_left_running", lambda units, jobs: (units, jobs, []))
    monkeypatch.setattr(pause, "_discover_bare", lambda pids: ([], []))
    monkeypatch.setattr(status, "get_process_start_time", lambda pid: 1)
    monkeypatch.setattr(record, "identity", lambda pid=None: {"ct": "ct:1"})
    monkeypatch.setattr(record, "record_pause", lambda *args: None)
    monkeypatch.setattr(record, "mark_stop_requested", lambda *args, **kwargs: None)
    monkeypatch.setattr(record, "mark_stop_sent", lambda *args: None)
    monkeypatch.setattr(record, "mark_stop_refused", lambda *args: None)
    monkeypatch.setattr(record, "sync", lambda token: None)
    monkeypatch.setattr(pause, "_drain_budget", lambda: 1885.0)
    monkeypatch.setattr(pause, "_alive", lambda *args: True)  # its turns never finish on their own
    monkeypatch.setattr(pause, "_stop_at_once",
                        lambda e, deadline, procs, **kw: order.append(("stop", e["unit"], head())))
    monkeypatch.setattr(pause, "_stopped", lambda *args: ("stop", "busy.service", head()) in order)
    from hermes_cli import update_cmd_windows as windows
    monkeypatch.setattr(windows, "_resume_windows_gateways_after_update", lambda token: order.append("resume"))
    monkeypatch.setattr(commit, "arm_commit_obligations", lambda *args: order.append("arm"))
    monkeypatch.setattr(commit, "arm_tree_move", lambda *args, **kw: order.append("move-armed"))
    return order, runner, root, head(), _fake_clock(monkeypatch)


@pytest.mark.platforms("linux")
def test_opted_in_update_stops_a_busy_gateway_within_the_bound_before_any_tree_move(tmp_path, monkeypatch):
    from hermes_cli import update_cmd_commit as commit
    order, runner, root, start_head, now = _busy_gateway_at_commit_point(tmp_path, monkeypatch)
    monkeypatch.setenv(pause.STOP_BUSY_ENV, "1")

    assert commit.arm_commit_point(["git"], root, "a" * 40, pre=None, target="b" * 40, stash=None) is None

    # Drain first (turns keep their resume markers), one bounded wait, supervisor stop, only then the move.
    assert order == ["drain", ("stop", "busy.service", start_head), "arm", "move-armed"]
    assert 20.0 <= now[0] <= 30.0  # drained for the 30 s pre-swap cap less the stop reserve, not 1885 s
    runner.request_restart.assert_called_once_with(detached=False, via_service=True)
    assert pause.gateways_paused()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("value", [None, "", "0"])
def test_without_the_opt_in_a_busy_gateway_defers_and_nothing_is_stopped(tmp_path, monkeypatch, value):
    from hermes_cli import update_cmd_commit as commit
    order, runner, root, _head, now = _busy_gateway_at_commit_point(tmp_path, monkeypatch)
    if value is None:
        monkeypatch.delenv(pause.STOP_BUSY_ENV, raising=False)
    else:
        monkeypatch.setenv(pause.STOP_BUSY_ENV, value)

    refused = commit.arm_commit_point(["git"], root, "a" * 40, pre=None, target="b" * 40, stash=None)

    assert "busy" in refused and "the checkout was not changed" in refused
    assert order == ["resume"]  # no drain request, no stop, no arming: only the rollback settle
    runner.request_restart.assert_not_called()
    assert now[0] == 0.0


@pytest.mark.platforms("linux")
def test_opted_in_update_stops_a_gateway_that_predates_the_pause_verbs_at_once(monkeypatch):
    """hermes-hub's pinned askeve gateways: no idle verb, so the default path refuses them forever."""
    from gateway import control_socket
    now = _fake_clock(monkeypatch)
    asked = []
    monkeypatch.setattr(control_socket, "pause_gateway_for_update",
                        lambda home, **kw: asked.append(kw) or None)  # None: the verb is unknown
    monkeypatch.setattr(record, "mark_stop_sent", lambda *args: None)
    monkeypatch.setattr(pause, "_ask_to_drain", Mock(side_effect=AssertionError("idle verb on the opted-in path")))
    stop = Mock()
    monkeypatch.setattr(pause, "_stop_at_once", stop)
    monkeypatch.setattr(pause, "_stopped", lambda *args: True)
    monkeypatch.setenv(pause.STOP_BUSY_ENV, "1")
    entry = {"pid": 4242, "kind": "systemd", "unit": "pinned.service", "home": "/fixture"}

    pause._stop_gateways({}, [entry])

    assert asked and all("idle_only" not in kw for kw in asked)  # the ordinary drain verb, never idle_only
    stop.assert_called_once()
    assert stop.call_args.args[0] is entry
    assert now[0] == 0.0  # a supervised gateway that cannot drain is not waited for


@pytest.mark.platforms("linux")
def test_opted_in_stop_that_fails_still_refuses_the_move(tmp_path, monkeypatch):
    from hermes_cli import update_cmd_commit as commit
    order, _runner, root, _head, _now = _busy_gateway_at_commit_point(tmp_path, monkeypatch)
    monkeypatch.setenv(pause.STOP_BUSY_ENV, "1")
    monkeypatch.setattr(pause, "_stop_at_once", Mock(side_effect=pause.PauseRefused("systemctl stop failed")))

    refused = commit.arm_commit_point(["git"], root, "a" * 40, pre=None, target="b" * 40, stash=None)

    assert "systemctl stop failed" in refused and "the checkout was not changed" in refused
    assert order == ["drain", "resume"]  # rolled back under the fence, never armed


@pytest.mark.parametrize("native", [
    pytest.param(False, marks=pytest.mark.platforms("posix")),
    pytest.param(True, marks=pytest.mark.platforms("macos")),
])
def test_macos_ignores_the_opt_in_and_stays_idle_only(monkeypatch, native):
    # The native case exercises the real platform predicate on macOS CI; the
    # simulated case keeps the refusal contract exercised on Linux as well.
    if not native:
        monkeypatch.setattr(pause, "_is_linux", lambda: False)
    monkeypatch.setenv(pause.STOP_BUSY_ENV, "1")
    assert not pause._stop_busy_opted_in()
    monkeypatch.setattr(pause, "_stop_busy_gateways", lambda *a: pytest.fail("busy stop on macOS"))
    asked = []
    monkeypatch.setattr(pause, "_ask_to_drain", lambda e: asked.append(e) or False)
    monkeypatch.setattr(record, "mark_stop_sent", lambda *a: None)
    monkeypatch.setattr(record, "mark_stop_refused", lambda *a: None)
    entry = {"pid": 4242, "kind": "launchd", "label": "ai.hermes.gateway", "home": "/h"}
    with pytest.raises(pause.PauseRefused):
        pause._stop_gateways({}, [entry])
    assert asked == [entry]


def test_the_opt_in_is_not_inherited_by_a_replayed_gateway():
    import inspect
    assert "STOP_BUSY_ENV" in inspect.getsource(pause._relaunch_bare).split("for leaked in", 1)[1].split("\n", 1)[0]
