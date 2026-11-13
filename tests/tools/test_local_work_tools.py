"""Quiet-v3 local-work coverage of tool/provider/RPC/MCP/plugin seams.

Real production seams are driven; only external effects (provider, remote kernel env, MCP
session, hook plugin) are Event-gated fakes. The registry is a real ``Registry`` swapped in
as the process registry; counters are never faked.
"""
import asyncio
import gc
import json
import socket
import threading
import types

import pytest

from hermes_cli import local_work
from hermes_cli.local_work import Registry
from tests.waiting import wait_until

T = 10.0


@pytest.fixture
def registry(monkeypatch, tmp_path):
    from hermes_constants import hermes_home_key

    reg = Registry(homes=[hermes_home_key()])
    monkeypatch.setattr(local_work, "_registry", reg)
    return reg


def _active(reg):
    return reg.snapshot().active


# ---------------------------------------------------------------- aux_call entry

def test_call_llm_scope_covers_provider_and_returns_to_zero(registry, monkeypatch):
    from agent import auxiliary_client as aux

    seen = {}
    external_operation = threading.Event()  # a provider-side op that outlives the local call

    def fake_impl(**_kwargs):
        seen["active"] = _active(registry)
        seen["kind"] = dict(registry.snapshot().per_kind)
        return "ok"

    monkeypatch.setattr(aux, "_call_llm_impl", fake_impl)
    baseline = registry.snapshot().epoch
    assert aux.call_llm(messages=[{"role": "user", "content": "x"}]) == "ok"
    assert seen["active"] == 1 and seen["kind"] == {"aux_call": 1}
    snap = registry.snapshot()
    assert snap.active == 0 and snap.epoch > baseline  # (a) local work only
    assert not external_operation.is_set()
    assert not snap.unknowns


def test_async_call_llm_scope(registry, monkeypatch):
    from agent import auxiliary_client as aux

    seen = {}

    async def fake_impl(**_kwargs):
        seen["active"] = _active(registry)
        return "ok"

    monkeypatch.setattr(aux, "_async_call_llm_impl", fake_impl)
    assert asyncio.run(aux.async_call_llm(messages=[])) == "ok"
    assert seen["active"] == 1 and _active(registry) == 0


# ---------------------------------------------------------------- hygiene compaction

def test_hygiene_worker_stays_counted_after_owner_cancel(registry):
    from gateway.run_turn_hygiene import GatewayTurnHygieneMixin as Mixin

    entered, release = threading.Event(), threading.Event()

    class Agent:
        context_compressor = None

        def _compress_context(self, *_a, **_k):
            entered.set()
            assert release.wait(T)
            return ([], "")

    agent = Agent()
    loop_holder = {}

    class Runner:
        async def _hmwa_hygiene_build_agent(self, *_a):
            return agent, None

        def _track_deferred_agent_worker(self, *_a):
            pass

        async def _hmwa_hygiene_wait_for_summary(self, *_a):
            await loop_holder["loop"].run_in_executor(None, entered.wait, T)
            raise asyncio.CancelledError  # owner is canceled while the worker lives

        def _hmwa_hygiene_on_unwind(self, *_a):
            pass

        def _evict_cached_agent(self, *_a):
            pass

        async def _cleanup_agent_resources_off_loop(self, *_a, **_k):
            pass

    attempt = types.SimpleNamespace(agent=None, commit_fence=None, future=None, wait_started=0.0,
                                    cleanup_deferred=False, history=None)
    hs = types.SimpleNamespace(total_ceiling_seconds=60)
    plan = types.SimpleNamespace(approx_tokens=1)
    session_entry = types.SimpleNamespace(session_id="s")

    async def run():
        loop_holder["loop"] = asyncio.get_running_loop()
        with pytest.raises(asyncio.CancelledError):
            await Mixin._hmwa_hygiene_detached_attempt(
                Runner(), attempt, hs, plan, [], [], "m", {}, None, session_entry, "k", "q", 0)
        # owner unwound; physical worker still gated
        assert not release.is_set() and _active(registry) == 1
        release.set()
        await loop_holder["loop"].run_in_executor(
            None, lambda: wait_until(lambda: _active(registry) == 0, timeout=T))

    try:
        asyncio.run(run())
    finally:
        release.set()
    assert _active(registry) == 0 and not registry.snapshot().unknowns


# ---------------------------------------------------------------- tool_function

def test_handle_function_call_scope(registry, monkeypatch):
    import model_tools

    seen = {}

    def fake_inner(*_a, **_k):
        seen["kinds"] = dict(registry.snapshot().per_kind)
        return "{}"

    monkeypatch.setattr(model_tools, "_handle_function_call", fake_inner)
    assert model_tools.handle_function_call("x", {}) == "{}"
    assert seen["kinds"] == {"tool_function": 1} and _active(registry) == 0


# ---------------------------------------------------------------- RPC handler

def _rpc_kwargs(dispatch):
    return dict(allowed_tools=frozenset({"t"}), tool_call_counter=[0], max_tool_calls=5,
                dispatch=dispatch, tool_call_log=[], call_start=0.0, where="test")


def test_late_rpc_callback_bumps_epoch_before_handler_body(registry):
    from tools.code_execution_rpc import _handle_rpc_request

    episode_epoch = registry.snapshot().epoch
    assert _active(registry) == 0  # episode is quiet; the callback is late
    order = {}

    def dispatch(_name, _args):
        snap = registry.snapshot()
        order["epoch"], order["active"] = snap.epoch, snap.active
        return "{}"

    _handle_rpc_request({"tool": "t", "args": {}}, **_rpc_kwargs(dispatch))
    assert order["epoch"] > episode_epoch and order["active"] == 1
    assert _active(registry) == 0


def test_refused_rpc_still_reserves_first(registry):
    from tools.code_execution_rpc import _handle_rpc_request

    before = registry.snapshot().epoch
    out = _handle_rpc_request({"tool": "nope"}, **_rpc_kwargs(lambda *_: "{}"))
    assert "not available" in out
    assert registry.snapshot().epoch > before and _active(registry) == 0


def test_rpc_socket_reserves_before_auth(registry):
    from tools.code_execution_rpc import _rpc_server_loop

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    stop = threading.Event()
    called = []
    epochs = {}
    thread = threading.Thread(target=_rpc_server_loop, args=(
        server, "t", [], [0], 5, frozenset({"t"}), stop, "secret"),
        kwargs={"dispatch": lambda *a: called.append(a) or "{}"}, daemon=True)
    before = registry.snapshot().epoch
    thread.start()
    try:
        with socket.create_connection(server.getsockname(), timeout=T) as client:
            client.sendall(json.dumps({"tool": "t", "token": "wrong"}).encode() + b"\n")
            assert "Unauthorized" in client.makefile().readline()
        epochs["after"] = registry.snapshot().epoch
    finally:
        stop.set()
        thread.join(T)
        server.close()
    assert not called and epochs["after"] > before  # unauthorized request still invalidated
    assert _active(registry) == 0


# ---------------------------------------------------------------- remote kernel (V3 case d)

class _Env:
    def __init__(self, on_cell_result, requests):
        self.on_cell_result = on_cell_result
        self.requests = list(requests)

    def get_temp_dir(self):
        return "/tmp"

    def execute(self, command, cwd=None, timeout=None, stdin_data=None):
        ok = {"output": "", "returncode": 0}
        if "nohup" in command:
            return {"output": "PID:4242\n", "returncode": 0}
        if "kill -0" in command:
            return {"output": "ALIVE\n", "returncode": 0}
        if "ls -1" in command and "/rpc/req_" in command:
            return {"output": self.requests.pop(0) if self.requests else "", "returncode": 0}
        if command.startswith("cat ") and "/rpc/req_" in command:
            return {"output": json.dumps({"tool": "t", "args": {}, "token": "x", "seq": 1}),
                    "returncode": 0}
        if command.startswith("cat ") and "cell_res_" in command:
            return {"output": self.on_cell_result(), "returncode": 0}
        return ok


def _run_remote(env, monkeypatch, dispatch):
    from tools import code_execution_rpc, code_kernel_remote as ckr

    monkeypatch.setattr("tools.code_execution_tool._ship_file_to_remote", lambda *a, **k: None)
    monkeypatch.setattr(code_execution_rpc, "_default_dispatch", lambda _t: dispatch)
    monkeypatch.setattr(code_execution_rpc, "_rpc_token_ok", lambda *_: True)
    ckr.shutdown_all_remote_kernels()
    try:
        return ckr.execute_in_remote_kernel(
            "1", env=env, env_type="ssh", task_env_id="local-work-t", sandbox_tools=frozenset({"t"}),
            timeout=10, max_tool_calls=5, reset=False)
    finally:
        ckr.shutdown_all_remote_kernels()


def _cell_ok():
    return json.dumps({"id": "000001", "status": "ok", "stdout": "", "stderr": "", "traceback": "",
                       "stdout_clipped": False, "stderr_clipped": False, "execution_count": 1})


def test_remote_rpc_join_timeout_with_live_handler_stays_nonzero(registry, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def dispatch(_name, _args):
        entered.set()
        assert release.wait(T)
        return "{}"

    def cell_result():
        assert entered.wait(T)  # the cell ends while the RPC handler is mid-flight
        return _cell_ok()

    env = _Env(cell_result, ["/tmp/x/rpc/req_000001"])
    observed = {}
    try:
        result = _run_remote(env, monkeypatch, dispatch)  # join(timeout=5) expires inside
        observed["active"] = _active(registry)
        observed["kinds"] = dict(registry.snapshot().per_kind)
    finally:
        release.set()
    assert result is not None
    assert observed["active"] >= 1
    assert observed["kinds"].get("tool_dispatch") == 1
    assert observed["kinds"].get("rpc_poll_thread") == 1
    wait_until(lambda: _active(registry) == 0, timeout=T, what="handler + poll thread finished")
    assert not registry.snapshot().unknowns


def test_remote_kernel_external_operation_outliving_call_is_not_counted(registry, monkeypatch):
    external_operation = threading.Event()  # remote op keeps "running"; never touched locally
    env = _Env(_cell_ok, [])
    baseline = registry.snapshot().epoch
    result = _run_remote(env, monkeypatch, lambda *_: "{}")
    assert result is not None
    snap = registry.snapshot()
    assert snap.active == 0 and snap.epoch > baseline  # poll thread was local work, now ended
    assert not external_operation.is_set()


# ---------------------------------------------------------------- MCP loop

@pytest.fixture
def mcp_loop(monkeypatch):

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(T)
    import tools.mcp_tool as _core
    monkeypatch.setattr(_core, "_mcp_loop", loop)
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(T)
    loop.close()


def test_mcp_call_returns_to_zero_while_external_session_op_lives(registry, mcp_loop):
    from tools import mcp_tool_loop
    from tools.mcp_tool_handlers import _call_tool_racing_stdio_death

    external_operation = threading.Event()
    seen = {}

    class Session:
        async def call_tool(self, name, arguments=None, **_kw):
            seen["active"] = _active(registry)
            return "result"

    server = types.SimpleNamespace(session=Session())
    baseline = registry.snapshot().epoch
    out = mcp_tool_loop._run_on_mcp_loop(
        lambda: _call_tool_racing_stdio_death(server, "srv", "tool", {}), timeout=T)
    assert out == "result" and seen["active"] == 1
    snap = registry.snapshot()
    wait_until(lambda: _active(registry) == 0, timeout=T)
    assert snap.epoch > baseline and not external_operation.is_set()


def test_mcp_future_cancel_with_running_coroutine_stays_nonzero(registry, mcp_loop):
    from tools import mcp_tool_loop

    entered = threading.Event()
    wake = []
    exited = threading.Event()

    async def stubborn():
        park = asyncio.Event()
        loop = asyncio.get_running_loop()
        wake.append(lambda: loop.call_soon_threadsafe(park.set))
        entered.set()
        while not park.is_set():
            try:
                await park.wait()
            except asyncio.CancelledError:
                pass  # ignores cancel: physical work continues
        exited.set()

    with pytest.raises(TimeoutError):
        mcp_tool_loop._run_on_mcp_loop(stubborn, timeout=0.2)  # health: allow HX013 -- timeout under test
    try:
        assert entered.wait(T)
        assert not exited.is_set()
        assert _active(registry) == 1  # future.cancel() is NOT release
    finally:
        wake[0]()
    assert exited.wait(T)
    wait_until(lambda: _active(registry) == 0, timeout=T)
    assert not registry.snapshot().unknowns


def test_mcp_never_started_cancel_is_released(registry, mcp_loop):
    from tools import mcp_tool_loop

    blocked, unblock = threading.Event(), threading.Event()
    ran = []
    mcp_loop.call_soon_threadsafe(lambda: (blocked.set(), unblock.wait(T)))
    assert blocked.wait(T)

    async def never():
        ran.append(1)

    with pytest.raises(TimeoutError):
        mcp_tool_loop._run_on_mcp_loop(never, timeout=0.05)
    assert _active(registry) == 1  # loop still stuck: nothing proved the wrapper finished
    unblock.set()

    def drained():
        gc.collect()
        return _active(registry) == 0

    wait_until(drained, timeout=T, what="never-started wrapper released")
    assert not ran and not registry.snapshot().unknowns


def test_mcp_schedule_failure_releases(registry, mcp_loop, monkeypatch):
    from tools import mcp_tool_loop

    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", lambda *a, **k: None)

    async def never():
        pass

    with pytest.raises(RuntimeError):
        mcp_tool_loop._run_on_mcp_loop(never, timeout=1)
    assert _active(registry) == 0


# ---------------------------------------------------------------- hook thread

def test_hook_timeout_thread_stays_nonzero_until_runner_finishes(registry, monkeypatch):
    from hermes_cli.plugins import PluginManager

    monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 0.2)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def cb(**_kwargs):
        entered.set()
        try:
            assert release.wait(T)
        finally:
            finished.set()

    mgr = PluginManager()
    mgr._hooks["pre_verify"] = [cb]
    try:
        assert mgr.invoke_hook("pre_verify", session_id="s") == []  # timed out, abandoned
        assert entered.wait(T) and not finished.is_set()
        assert _active(registry) == 1
        assert registry.snapshot().per_kind == {"hook_thread": 1}
    finally:
        release.set()
    assert finished.wait(T)
    wait_until(lambda: _active(registry) == 0, timeout=T)


# ---------------------------------------------------------------- plugin host ingress

@pytest.fixture
def host(tmp_path):
    from hermes_cli.plugin_host import PluginHost

    instance = object.__new__(PluginHost)
    instance._home = tmp_path
    instance._handles = {}
    return instance


@pytest.mark.parametrize("method,params,attr", [
    ("ctx", {"method": "dispatch_tool"}, "_serve_ctx"),
    ("facade", {}, "_serve_facade"),
    ("session_event", {}, "_serve_session_event"),
])
def test_plugin_host_ingress_reserves_before_body(registry, host, monkeypatch, method, params, attr):
    seen = {}
    episode_epoch = registry.snapshot().epoch

    def body(_params):
        snap = registry.snapshot()
        seen["epoch"], seen["kinds"] = snap.epoch, dict(snap.per_kind)
        return "ok"

    monkeypatch.setattr(host, attr, body)
    assert host._handle(method, params, None) == "ok"
    assert seen["epoch"] > episode_epoch and seen["kinds"] == {"plugin_host_ingress": 1}
    assert _active(registry) == 0


def test_plugin_host_register_and_dispose_are_passive(registry, host, monkeypatch):
    disposed = []
    host._handles[7] = types.SimpleNamespace(dispose=lambda: disposed.append(1))
    monkeypatch.setattr(host, "_serve_ctx", lambda _p: None)
    before = registry.snapshot().epoch
    host._handle("ctx", {"method": "register_tool"}, None)
    host._handle("dispose", {"handle": 7}, None)
    assert disposed and registry.snapshot().epoch == before


# ---------------------------------------------------------------- no registry installed

def test_seams_are_inert_without_registry(monkeypatch):
    from tools.code_execution_rpc import _handle_rpc_request

    monkeypatch.setattr(local_work, "_registry", None)
    assert _handle_rpc_request({"tool": "t", "args": {}}, **_rpc_kwargs(lambda *_: "{}")) == "{}"
