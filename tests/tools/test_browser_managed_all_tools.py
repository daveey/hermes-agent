"""Every built-in browser tool that attaches to a managed ``browser.cdp_url`` holds an owner lease.

Each entry path runs twice: under ``browser.managed_agent`` (lease acquired before any CDP traffic,
only the lease-scoped endpoint used, released by task cleanup) and with a plain ``cdp_url``
(no lifecycle traffic, the raw override used exactly as before).
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _Lifecycle:
    """Fake managed supervisor: /lifecycle/* control, /lease/<cap>/json/version discovery, and a
    raw root that answers like plain Chrome when unmanaged and 403s when managed (as the real one does)."""

    def __init__(self, managed: bool):
        self.managed = managed
        self.calls = []        # (path, payload) lifecycle POSTs
        self.gets = []         # every GET path
        self.rotate = False    # next acquire grants a fresh capability
        self.caps = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.calls.append((self.path, payload))
                if self.path == "/lifecycle/acquire":
                    owner = tuple(payload["owner"])
                    if outer.rotate or owner not in outer.caps:
                        outer.rotate = False
                        outer.caps[owner] = "cap" + str(len(outer.calls))
                    cap = outer.caps[owner]
                    return self._send(200, dict(capability=cap, request_id="req" + str(len(outer.calls)),
                                                daemon_name="lease-" + cap,
                                                endpoint=outer.endpoint + "/lease/" + cap))
                return self._send(200, {"ok": True})

            def do_GET(self):
                outer.gets.append(self.path)
                port = outer.server.server_port
                if self.path.startswith("/lease/") and self.path.endswith("/json/version"):
                    cap = self.path.split("/")[2]
                    return self._send(200, {"webSocketDebuggerUrl":
                                            f"ws://127.0.0.1:{port}/lease/{cap}/devtools/browser/scoped"})
                if self.path == "/json/version" and not outer.managed:
                    return self._send(200, {"webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/raw"})
                return self._send(403, {"error": "managed Chrome requires an owner lease"})

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def paths(self):
        return [path for path, _ in self.calls]

    def raw_ws(self):
        return f"ws://127.0.0.1:{self.server.server_port}/devtools/browser/raw"

    def scoped_ws(self, cap):
        return f"ws://127.0.0.1:{self.server.server_port}/lease/{cap}/devtools/browser/scoped"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _write_config(home, browser):
    (home / "config.yaml").write_text(json.dumps({"browser": browser}))


@pytest.fixture(params=["managed", "unmanaged"])
def env(request, tmp_path, monkeypatch):
    """Fake lifecycle + agent-browser + supervisor; ``env.managed`` selects the config."""
    from tools import browser_agent_lifecycle as lifecycle
    from tools import browser_tool as bt
    from tools import browser_tool_session as bt_session
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    managed = request.param == "managed"
    fake = _Lifecycle(managed)
    browser = {"cdp_url": fake.endpoint}
    if managed:
        browser["managed_agent"] = {"endpoint": fake.endpoint, "installation_id": "test-installation"}
    _write_config(tmp_path, browser)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.delenv("CAMOFOX_URL", raising=False)

    for name in ("_active_sessions", "_session_last_activity", "_last_active_session_key"):
        monkeypatch.setattr(bt, name, {})
    monkeypatch.setattr("tools.browser_tool_lifecycle._start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(bt, "_url_policy_error", lambda *a, **k: None)
    monkeypatch.setattr(bt, "_post_redirect_block", lambda *a, **k: None)
    monkeypatch.setattr(bt, "_attach_auto_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(bt, "_maybe_start_recording", lambda *a, **k: None)
    monkeypatch.setattr(bt, "_maybe_stop_recording", lambda *a, **k: None)
    monkeypatch.setattr(bt, "_blocked_private_page_content", lambda *a, **k: None)
    monkeypatch.setattr(bt_session, "_browser_command_preflight", lambda: {"browser_cmd": "/usr/bin/agent-browser"})

    spawned = []
    responses = {
        "open": {"success": True, "data": {"title": "Example", "url": "https://example.com/"}},
        "snapshot": {"success": True, "data": {"snapshot": '- button "Go" [ref=e1] and more text', "refs": {}}},
        "click": {"success": True, "data": {}},
        "close": {"success": True, "data": {}},
    }

    def fake_spawn(task_id, session_info, cmd_parts, command, *rest):
        spawned.append((command, list(cmd_parts)))
        return json.loads(json.dumps(responses.get(command, {"success": True, "data": {}})))

    monkeypatch.setattr(bt_session, "_spawn_and_collect", fake_spawn)

    attached, stopped = [], []
    monkeypatch.setattr(SUPERVISOR_REGISTRY, "get_or_start",
                        lambda task_id, cdp_url, **kw: attached.append((task_id, cdp_url)))
    monkeypatch.setattr(SUPERVISOR_REGISTRY, "get", lambda task_id: None)
    monkeypatch.setattr(SUPERVISOR_REGISTRY, "stop", lambda task_id: stopped.append(task_id))

    class Env:
        pass

    e = Env()
    e.managed, e.fake, e.spawned, e.responses = managed, fake, spawned, responses
    e.attached, e.stopped, e.home = attached, stopped, tmp_path
    yield e
    lifecycle.release_all()
    fake.close()


def _dispatch(name, args, task_id):
    import tools.browser_cdp_tool  # noqa: F401 — registers browser_cdp
    import tools.browser_dialog_tool  # noqa: F401
    import tools.browser_tool  # noqa: F401
    import tools.browser_vault_tool  # noqa: F401
    from tools.registry import registry
    out = registry.dispatch(name, args, task_id=task_id)
    return json.loads(out) if isinstance(out, str) else out


def _cdp_arg(cmd_parts):
    return cmd_parts[cmd_parts.index("--cdp") + 1] if "--cdp" in cmd_parts else None


def _acquires(fake):
    return [payload for path, payload in fake.calls if path == "/lifecycle/acquire"]


def test_navigate_snapshot_click_flow(env):
    from tools.browser_tool_lifecycle import cleanup_browser
    fake = env.fake
    assert _dispatch("browser_navigate", {"url": "https://example.com/"}, "task-a")["success"]
    assert _dispatch("browser_snapshot", {}, "task-a")["success"]
    assert _dispatch("browser_click", {"ref": "@e1"}, "task-a")["success"]
    commands = [command for command, _ in env.spawned if command != "eval"]  # click pre-checks via eval
    assert commands == ["open", "snapshot", "click"]

    if env.managed:
        acquires = _acquires(fake)
        assert len(acquires) == 3  # one owner request per tool call (nested calls reuse it)
        assert {tuple(a["owner"][1:]) for a in acquires} == {("task-a", "tools")}
        assert all("daemon_dir" not in a for a in acquires)  # no harness daemon for agent-browser
        cap = fake.caps[tuple(acquires[0]["owner"])]
        assert fake.paths().count("/lifecycle/complete") == 3
        assert {_cdp_arg(parts) for _, parts in env.spawned} == {fake.scoped_ws(cap)}
        assert "/json/version" not in fake.gets  # never the unscoped root, even to discover
        assert env.attached and {url for _, url in env.attached} == {fake.scoped_ws(cap)}
        cleanup_browser("task-a")
        released = [p["capability"] for path, p in fake.calls if path == "/lifecycle/release"]
        assert released == [cap]
        assert [c for c, _ in env.spawned].count("close") == 0  # cleanup never wakes Chrome
        assert len(_acquires(fake)) == 3
        assert "task-a" in env.stopped
    else:
        assert fake.calls == []
        assert {_cdp_arg(parts) for _, parts in env.spawned} == {fake.raw_ws()}
        assert {url for _, url in env.attached} == {fake.raw_ws()}
        cleanup_browser("task-a")
        assert [c for c, _ in env.spawned][-1] == "close"
        assert fake.calls == []


def test_rotated_capability_reattaches_session(env):
    if not env.managed:
        pytest.skip("rotation is a managed-lease concept")
    fake = env.fake
    assert _dispatch("browser_navigate", {"url": "https://example.com/"}, "task-a")["success"]
    first = _cdp_arg(env.spawned[-1][1])
    fake.rotate = True
    assert _dispatch("browser_snapshot", {}, "task-a")["success"]
    second = _cdp_arg(env.spawned[-1][1])
    assert first != second and "/lease/" in second
    released = [p["capability"] for path, p in fake.calls if path == "/lifecycle/release"]
    assert len(released) == 1 and released[0] in first  # the old capability is retired
    assert env.attached[-1][1] == second
    # Retiring the old capability never stops the live task supervisor: the only stop is the
    # rotation teardown that runs before the re-attach.
    assert env.stopped == ["task-a"]


def test_outside_a_lease_fails_clearly_never_raw(env):
    from tools import browser_tool_cdp
    from tools.browser_agent_lifecycle import ManagedBrowserError
    if env.managed:
        with pytest.raises(ManagedBrowserError, match="owner lease"):
            browser_tool_cdp._get_cdp_override()
        assert env.fake.gets == []
    else:
        assert browser_tool_cdp._get_cdp_override() == env.fake.raw_ws()


def test_lease_refusal_is_a_tool_error_naming_the_tool(env, monkeypatch):
    from tools import browser_agent_lifecycle as lifecycle
    if not env.managed:
        pytest.skip("unmanaged never contacts the lifecycle")

    def refused(*args, **kwargs):
        raise lifecycle.ManagedBrowserError("Managed browser deliberately stopped/disabled")

    monkeypatch.setattr(lifecycle, "_post", refused)
    result = _dispatch("browser_navigate", {"url": "https://example.com/"}, "task-a")
    assert not result["success"] and result["error"].startswith("browser_navigate:")
    result = _dispatch("browser_cdp", {"method": "Target.getTargets"}, "task-a")
    assert "browser_cdp:" in result["error"]
    assert env.spawned == [] and env.fake.gets == []


def test_browser_cdp(env, monkeypatch):
    from tools import browser_cdp_tool
    from tools.browser_tool_lifecycle import cleanup_browser
    seen = []

    async def fake_call(endpoint, method, params, target_id, timeout):
        seen.append(endpoint)
        return {"targetInfos": []}

    monkeypatch.setattr(browser_cdp_tool, "_cdp_call", fake_call)
    assert browser_cdp_tool._browser_cdp_check()
    result = _dispatch("browser_cdp", {"method": "Target.getTargets"}, "task-c")
    assert result.get("success") is not False, result
    fake = env.fake
    if env.managed:
        (acquire,) = _acquires(fake)
        assert acquire["owner"][1:] == ["task-c", "tools"]
        cap = fake.caps[tuple(acquire["owner"])]
        assert seen == [fake.scoped_ws(cap)]
        assert fake.paths() == ["/lifecycle/acquire", "/lifecycle/complete"]
        assert "/json/version" not in fake.gets
        cleanup_browser("task-c")
        assert [p["capability"] for path, p in fake.calls if path == "/lifecycle/release"] == [cap]
    else:
        assert seen == [fake.raw_ws()]
        assert fake.calls == []


def test_vault_fill_holds_the_selected_scope(env, monkeypatch):
    from tools import browser_agent_lifecycle as lifecycle
    from tools import browser_vault_tool as vault
    from tools.browser_tool_lifecycle import cleanup_browser
    held = []

    def fake_fill(handle, task_id=None):
        held.append((lifecycle.current_tool_lease(), list(env.fake.paths())))
        return json.dumps({"success": True, "filled": 1})

    monkeypatch.setattr(vault, "browser_vault_fill", fake_fill)
    assert _dispatch("browser_navigate", {"url": "https://example.com/"}, "task-v")["success"]
    before = len(env.fake.calls)
    assert _dispatch("browser_vault", {"action": "fill", "handle": "h"}, "task-v")["success"]
    fake = env.fake
    if env.managed:
        cap = next(iter(fake.caps.values()))
        lease, paths = held[0]
        assert lease == fake.endpoint + "/lease/" + cap  # same scope as the page it fills
        assert paths[before:] == ["/lifecycle/acquire"]  # leased while the fill runs
        assert fake.paths()[-1] == "/lifecycle/complete"
        assert lifecycle.supervisor_task("task-v") == "task-v"
        # A rotated capability means the page is gone: refuse instead of filling elsewhere.
        fake.rotate = True
        result = _dispatch("browser_vault", {"action": "fill", "handle": "h"}, "task-v")
        assert not result["success"] and result["error"].startswith("browser_vault:")
        assert len(held) == 1
        cleanup_browser("task-v")
        assert len([p for path, p in fake.calls if path == "/lifecycle/release"]) == 2
    else:
        assert held == [("", [])]
        assert fake.calls == []


def test_vault_fill_renews_browser_exec_scope(env, monkeypatch):
    if not env.managed:
        pytest.skip("covered unmanaged by test_vault_fill_holds_the_selected_scope")
    import subprocess
    from tools import browser_agent_lifecycle as lifecycle
    from tools import browser_use_cli as cli
    from tools import browser_vault_tool as vault
    monkeypatch.setattr(cli, "_base_subprocess_env", dict)
    monkeypatch.setattr(cli, "_find_cli", lambda: ["unused-test-cli"])
    monkeypatch.setattr(cli, "_attach_vault_supervisor", lambda *args: None)
    monkeypatch.setattr(cli, "_workspace_dir", lambda task: None)
    monkeypatch.setattr(cli, "_find_screenshot", lambda *args: None)
    monkeypatch.setattr(cli, "_code_url_verdict", lambda code: cli._CodeUrlVerdict("", False))
    monkeypatch.setattr(cli, "_run_cli_killing_process_group",
                        lambda cmd, code, env, timeout: subprocess.CompletedProcess(cmd, 0, "ok", ""))
    assert json.loads(cli.browser_exec("print(1)", task_id="task-x"))["success"]
    before = len(_acquires(env.fake))
    owners = []
    monkeypatch.setattr(vault, "browser_vault_fill",
                        lambda handle, task_id=None: owners.append(
                            (len(_acquires(env.fake)), _acquires(env.fake)[-1]["owner"])) or
                        json.dumps({"success": True}))
    assert _dispatch("browser_vault", {"action": "fill", "handle": "h"}, "task-x")["success"]
    assert owners[0][0] == before + 1  # leased while the fill runs
    assert owners[0][1][1:] == ["task-x", "default"]  # the browser_exec scope, not a new tools scope
    assert env.fake.paths()[-1] == "/lifecycle/complete"
    assert lifecycle.supervisor_task("task-x").startswith("managed-")


def test_requirements_and_doctor_treat_idle_endpoint_as_available(env, monkeypatch):
    """Capability checks are config reads: no HTTP probe, idle or not."""
    import urllib.request
    import requests
    from tools import browser_cdp_tool, browser_tool as bt
    from tools import browser_tool_cloud, browser_tool_install, browser_tool_lightpanda_fallback as lp
    from hermes_cli import doctor_tools

    env.fake.close()  # idle: nothing listens on the managed endpoint at all
    monkeypatch.setattr(requests, "get", lambda *a, **k: pytest.fail("network probe in a capability check"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("network probe"))
    monkeypatch.setattr(bt, "_is_browser_use_cli_mode", lambda: False)
    assert browser_tool_install.check_browser_requirements()
    assert browser_cdp_tool._browser_cdp_check()
    assert not browser_tool_cloud._is_local_mode()
    assert browser_tool_cloud.browser_backend_name() == "cdp"
    monkeypatch.setattr(doctor_tools, "check_bool", lambda *a, **k: pytest.fail("chromium warned"))
    doctor_tools._check_chromium()
    monkeypatch.setattr(browser_tool_cloud, "_get_browser_engine", lambda: "lightpanda")
    assert lp.lightpanda_engine_status() == (False, "a CDP override is active (/browser connect or browser.cdp_url)")
    env.fake.calls.clear()


def test_lightpanda_fallback_gating(env, monkeypatch):
    from tools import browser_tool_cloud, browser_tool_lightpanda_fallback as lp
    fallbacks = []
    monkeypatch.setattr(browser_tool_cloud, "_get_browser_engine", lambda: "lightpanda")
    monkeypatch.setattr(lp, "_run_chrome_fallback_command",
                        lambda *a, **k: fallbacks.append(a) or {"success": True, "data": {"snapshot": "x" * 40}})
    env.responses["snapshot"] = {"success": True, "data": {"snapshot": "", "refs": {}}}
    assert _dispatch("browser_navigate", {"url": "https://example.com/"}, "task-l")["success"]
    result = _dispatch("browser_snapshot", {}, "task-l")
    if env.managed:
        assert fallbacks == []  # never a temporary local Chrome outside the lease
        assert "fallback_warning" not in result
    else:
        assert len(fallbacks) == 1  # unchanged: the existing Lightpanda→Chrome retry
