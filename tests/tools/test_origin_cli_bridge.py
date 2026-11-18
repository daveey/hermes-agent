"""Private CLI socket transport, using real native creation custody."""
import json
import os
import socket
from array import array
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest


def packet(launch, data, fds=()):
    launch._child.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array("i", fds))] if fds else [])


@pytest.fixture
def echo_launch(monkeypatch):
    from tools import origin_cli_bridge as bridge
    monkeypatch.setattr("tools.thread_context._callback_api", lambda: ())
    def invoke(self, args, gate):
        gate.admit(args["title"])
        return {"ok": True, "task_id": args["title"]}
    monkeypatch.setattr(bridge.Launch, "_invoke", invoke)
    launch = bridge.Launch("echo")
    try:
        yield launch
    finally:
        launch.close()
        launch._thread.join(2)
        assert not launch._thread.is_alive()


def test_empty_packet_does_not_poison_launch(echo_launch):
    from tools import origin_cli_bridge as bridge
    packet(echo_launch, b"")
    assert bridge.request(echo_launch.locator, {"title": "after-empty"}, timeout=1)["ok"]


def test_non_socket_reply_fd_is_closed(echo_launch):
    from tools import origin_cli_bridge as bridge
    with open(os.devnull, "rb") as file:
        before = set(os.listdir("/proc/self/fd"))
        for _ in range(5):
            packet(echo_launch, b"{}", [file.fileno()])
        assert bridge.request(echo_launch.locator, {"title": "barrier"}, timeout=1)["ok"]
        assert set(os.listdir("/proc/self/fd")) == before


def test_bad_nonce_closes_reply_without_timeout(echo_launch):
    from tools import origin_cli_bridge as bridge
    locator = json.loads(echo_launch.locator)
    locator["nonce"] = "wrong"
    start = time.monotonic()
    result = bridge.request(json.dumps(locator), {"title": "bad"}, timeout=2)
    assert not result["ok"]
    assert time.monotonic() - start < 1
    assert echo_launch.retire() == []


def test_dispatch_exception_does_not_poison_launch(echo_launch, monkeypatch):
    from tools import origin_cli_bridge as bridge
    invoke = bridge.Launch._invoke
    def failing(self, args, gate):
        if args["title"] == "bad":
            raise KeyError("private detail")
        return invoke(self, args, gate)
    monkeypatch.setattr(bridge.Launch, "_invoke", failing)
    result = bridge.request(echo_launch.locator, {"title": "bad"}, timeout=1)
    assert not result["ok"]
    assert "private detail" not in str(result)
    assert bridge.request(echo_launch.locator, {"title": "good"}, timeout=1)["ok"]


def test_abandoned_queued_request_is_not_dispatched(echo_launch, monkeypatch):
    from tools import origin_cli_bridge as bridge
    entered, release = threading.Event(), threading.Event()
    invoke = bridge.Launch._invoke
    calls = []
    def slow(self, args, gate):
        calls.append(args["title"])
        if args["title"] == "slow":
            entered.set()
            assert release.wait(3)
        return invoke(self, args, gate)
    monkeypatch.setattr(bridge.Launch, "_invoke", slow)
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(bridge.request, echo_launch.locator, {"title": "slow"})
        try:
            assert entered.wait(2)
            result = bridge.request(echo_launch.locator, {"title": "abandoned"}, timeout=0.05)
            assert not result["ok"]
        finally:
            release.set()
        assert first.result()["ok"]
    assert bridge.request(echo_launch.locator, {"title": "barrier"})["ok"]
    assert calls == ["slow", "barrier"]


def test_parallel_clients_receive_their_own_replies(echo_launch):
    from tools import origin_cli_bridge as bridge
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda i: bridge.request(echo_launch.locator, {"title": str(i)}), range(24)))
    assert [r["task_id"] for r in results] == list(map(str, range(24)))

def test_bad_client_fd_does_not_leak(echo_launch):
    from tools import origin_cli_bridge as bridge
    with open(os.devnull, "rb") as file:
        before = set(os.listdir("/proc/self/fd"))
        result = bridge.request(json.dumps({"fd": file.fileno(), "nonce": "x"}), {})
        assert not result["ok"]
        assert set(os.listdir("/proc/self/fd")) == before


def test_received_reply_fd_is_not_inheritable(echo_launch, monkeypatch):
    from tools import origin_cli_bridge as bridge
    handle = bridge.Launch._handle
    observed = []
    def inspect(self, data, flags, fds):
        observed.extend(os.get_inheritable(fd) for fd in fds)
        return handle(self, data, flags, fds)
    monkeypatch.setattr(bridge.Launch, "_handle", inspect)
    assert bridge.request(echo_launch.locator, {"title": "cloexec"})["ok"]
    assert observed == [False]


from hermes_cli import kanban_db_connect as kbc, kanban_origin_native as native
from tests.hermes_cli import test_origin_native_admission as fixtures
from tools import kanban_tools

turn = fixtures.turn


def policy_launch(monkeypatch, task_id):
    import model_tools
    from tools.origin_cli_bridge import Launch
    with monkeypatch.context() as scoped:
        scoped.setattr(model_tools, "_handle_function_call", lambda *a: Launch(task_id))
        return model_tools.handle_function_call("terminal", {}, task_id=task_id,
                                               enabled_tools=["kanban_create", "terminal"])


@pytest.mark.parametrize("entrypoint", ["client", "cli"])
@pytest.mark.parametrize("revoke", [False, True])
def test_real_subprocess_uses_only_parent_custody(turn, monkeypatch, tmp_path, revoke, entrypoint):
    import subprocess
    import sys
    from tools import origin_cli_bridge as bridge
    from tools.environments.local import build_subprocess_env
    launch = policy_launch(monkeypatch, "terminal-test")
    monkeypatch.setattr("model_tools._select_tool_names", lambda *a, **k: {"kanban_create"})
    monkeypatch.setattr("model_tools.handle_function_call",
                        lambda name, args, **kw: kanban_tools._handle_create(args))
    script = tmp_path / "cli_client.py"
    script.write_text("import json,sys\nfrom tools.origin_cli_bridge import request\n"
                      "print(json.dumps(request(sys.argv[1], {'title':'subprocess child',"
                      "'assignee':'default','placement':'local','requires':[]})))\n")
    try:
        if revoke:
            turn.release()
        env = build_subprocess_env(scrub_secrets=True)
        env["PYTHONPATH"] = os.getcwd()
        env[bridge.ENV_KEY] = launch.locator
        argv = ([sys.executable, "-m", "hermes_cli.main", "kanban", "create", "subprocess child",
                 "--assignee", "default", "--no-requires", "--json"] if entrypoint == "cli"
                else [sys.executable, str(script), launch.locator])
        result = subprocess.run(argv,
                                env=env, pass_fds=(launch.child_fd,), capture_output=True,
                                text=True, encoding="utf-8", timeout=20, check=False)
        assert result.returncode == (2 if revoke and entrypoint == "cli" else 0), result.stderr
        assert result.stdout.strip(), (result.returncode, result.stderr)
        value = json.loads(result.stdout)
        assert bool(value.get("ok")) is (not revoke), value
        with kbc.connect_closing() as conn:
            if revoke:
                assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
            else:
                assert native._receipt(conn, value["task_id"])["origin"]["session"] == fixtures.fixtures.CHAT
    finally:
        launch.close()
        launch._thread.join(2)


def test_retirement_is_bounded_while_dispatch_is_blocked(echo_launch, monkeypatch):
    from tools import origin_cli_bridge as bridge
    entered, release = threading.Event(), threading.Event()
    def blocked(self, args, gate):
        entered.set()
        assert release.wait(3)
        gate.admit("late")
        return {"ok": True, "task_id": "late"}
    monkeypatch.setattr(bridge.Launch, "_invoke", blocked)
    with ThreadPoolExecutor(1) as pool:
        call = pool.submit(bridge.request, echo_launch.locator, {"title": "late"})
        try:
            assert entered.wait(2)
            start = time.monotonic()
            assert echo_launch.retire() == [{"outcome": "no_board_commit", "task_id": None, "finished": False}]
            assert time.monotonic() - start < 1
        finally:
            release.set()
        assert not call.result()["ok"]


def test_post_admission_exception_reports_unknown(echo_launch, monkeypatch):
    from tools import origin_cli_bridge as bridge
    def failed(self, args, gate):
        gate.admit("committed-maybe")
        raise KeyError("post commit")
    monkeypatch.setattr(bridge.Launch, "_invoke", failed)
    result = bridge.request(echo_launch.locator, {"title": "x"})
    assert result["outcome"] == "outcome_unknown"
    assert result["task_id"] == "committed-maybe"


def test_transport_without_dispatch_policy_refuses(turn, monkeypatch):
    from tools import origin_cli_bridge as bridge
    called = []
    monkeypatch.setattr("model_tools.handle_function_call", lambda *a, **k: called.append(a))
    launch = bridge.Launch("not-authority")
    try:
        result = bridge.request(launch.locator, {"title": "denied"})
        assert not result["ok"]
        assert result["error"] == "CLI launch has no parent dispatch policy"
        assert called == []
        with kbc.connect_closing() as conn:
            assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    finally:
        launch.close()
        launch._thread.join(2)


def test_transport_creates_with_parent_custody(turn, monkeypatch):
    from tools import origin_cli_bridge as bridge
    launch = policy_launch(monkeypatch, "terminal-test")
    monkeypatch.setattr("model_tools._select_tool_names", lambda *a, **k: {"kanban_create"})
    monkeypatch.setattr("model_tools.handle_function_call",
                        lambda name, args, **kw: kanban_tools._handle_create(args))
    try:
        result = bridge.request(launch.locator, {"title": "socket child", "assignee": "default",
                                                 "placement": "local", "requires": []})
        assert result["ok"], result
        with kbc.connect_closing() as conn:
            receipt = native._receipt(conn, result["task_id"])
            assert receipt["origin"]["session"] == fixtures.fixtures.CHAT
        assert launch.retire() == [{"outcome": "created", "task_id": result["task_id"], "finished": True}]
    finally:
        launch.close()
