"""Agent tab ledger: tabs agents open in the user's shared Chrome are recorded, capped and closed.

The preamble is exec'd exactly as the harness CLI execs it (one namespace holding the helpers), against a
fake browser; Hermes-side closing runs the real close snippet against the same fake through a stubbed CLI.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import time

import pytest

from tools import browser_tool_lifecycle as bt_lifecycle
from tools import browser_use_cli as bu


class FakeBrowser:
    """Chrome's target list behind the harness helpers. Helpers call the RAW cdp, as browser_harness's
    module-level helpers do, so only the exec-namespace wrappers see creates/closes."""

    def __init__(self, *user_tabs):
        self.targets = {t: "https://user.example/" + t for t in user_tabs}
        self.closed = []
        self._n = 0
        self.attached = None

    def cdp(self, method, session_id=None, **kw):
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": t, "type": "page", "url": u} for t, u in self.targets.items()]}
        if method == "Target.createTarget":
            self._n += 1
            tid = f"T{self._n}"
            self.targets[tid] = kw.get("url", "about:blank")
            return {"targetId": tid}
        if method == "Target.closeTarget":
            if kw["targetId"] not in self.targets:
                raise RuntimeError("No target with given id found")
            del self.targets[kw["targetId"]]
            self.closed.append(kw["targetId"])
            return {}
        return {}

    def current_tab(self):
        return {"targetId": self.attached, "url": self.targets.get(self.attached, "")}

    def new_tab(self, url="about:blank"):
        if url != "about:blank" and self.targets.get(self.attached) == "about:blank":
            self.targets[self.attached] = url  # browser_harness reuses a blank attached tab
            return self.attached
        tid = self.cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
        self.targets[tid] = url
        self.attached = tid
        return tid

    def close_tab(self, target=None):
        self.cdp("Target.closeTarget", targetId=target if target is not None else self.attached)

    def namespace(self):
        return {"cdp": self.cdp, "new_tab": self.new_tab, "close_tab": self.close_tab,
                "current_tab": self.current_tab}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    rt = tmp_path / "runtime"
    env = {"BH_RUNTIME_DIR": str(rt)}
    monkeypatch.setattr(bu, "_read_browser_cfg", dict)
    monkeypatch.setattr(bu, "_base_subprocess_env", lambda: dict(env))
    monkeypatch.delenv("BU_NAME", raising=False)
    return {"env": env, "dir": rt / bu._TAB_LEDGER_DIR}


def _preamble(browser, env, owner, monkeypatch, cap_owner=3, cap=8):
    """Exec the ledger preamble the way browser_exec ships it; returns the wrapped namespace."""
    monkeypatch.setenv("HERMES_AGENT_TAB_LEDGER", str(bu._tab_ledger_path(env, owner)))
    monkeypatch.setenv("HERMES_AGENT_TAB_CAP_OWNER", str(cap_owner))
    monkeypatch.setenv("HERMES_AGENT_TAB_CAP", str(cap))
    ns = browser.namespace()
    exec(bu._TAB_LEDGER_PREAMBLE, ns)
    return ns


def _tabs(env, owner):
    return bu._read_tab_ledger(bu._tab_ledger_path(env, owner))["tabs"]


def _write(env, owner, tabs, last_used):
    bu._write_tab_ledger(bu._tab_ledger_path(env, owner), {
        "owner": owner, "last_used": last_used,
        "tabs": {t: {"created": i, "bu_name": "default", "url": ""} for i, t in enumerate(tabs)}})


@pytest.fixture
def cli(monkeypatch):
    """Stub CLI that runs the shipped close snippet against ``cli.browser``; records each call's env."""
    calls = []

    class Cli:
        browser = None

    def run(cmd, code, env, timeout):
        calls.append({"code": code, "env": env})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(code, {"cdp": Cli.browser.cdp})
        return subprocess.CompletedProcess(cmd, 0, out.getvalue(), "")

    monkeypatch.setattr(bu, "_find_cli", lambda: [sys.executable, "-m", "browser_harness.run"])
    monkeypatch.setattr(bu, "_run_cli_killing_process_group", run)
    Cli.calls = calls
    return Cli


def test_preamble_records_created_tab_and_drops_it_on_close(ledger, monkeypatch):
    browser = FakeBrowser("user1")
    ns = _preamble(browser, ledger["env"], "task-a", monkeypatch)

    tid = ns["new_tab"]("https://example.com")
    own = ns["cdp"]("Target.createTarget", url="about:blank", background=True)["targetId"]
    tabs = _tabs(ledger["env"], "task-a")
    assert set(tabs) == {tid, own}
    assert tabs[tid]["url"] == "https://example.com" and tabs[tid]["bu_name"] == "default"

    ns["cdp"]("Target.closeTarget", targetId=own)
    ns["close_tab"](tid)
    assert _tabs(ledger["env"], "task-a") == {}
    assert set(browser.targets) == {"user1"}


def test_reused_blank_tab_is_not_recorded(ledger, monkeypatch):
    browser = FakeBrowser("blank")
    browser.targets["blank"] = "about:blank"
    browser.attached = "blank"
    ns = _preamble(browser, ledger["env"], "task-a", monkeypatch)
    assert ns["new_tab"]("https://example.com") == "blank"
    assert _tabs(ledger["env"], "task-a") == {}


def test_per_owner_cap_closes_owners_oldest_tab_first(ledger, monkeypatch):
    browser = FakeBrowser("user1")
    ns = _preamble(browser, ledger["env"], "task-a", monkeypatch, cap_owner=2)
    first, second = ns["new_tab"]("https://a.example"), ns["new_tab"]("https://b.example")
    third = ns["new_tab"]("https://c.example")

    assert browser.closed == [first]
    assert set(_tabs(ledger["env"], "task-a")) == {second, third}
    assert "user1" in browser.targets


def test_global_cap_closes_idle_owners_tabs_first(ledger, monkeypatch):
    browser = FakeBrowser("user1", "idle1", "idle2", "busy1")
    _write(ledger["env"], "idle-task", ["idle1", "idle2"], last_used=time.time() - 1000)
    _write(ledger["env"], "busy-task", ["busy1"], last_used=time.time())
    ns = _preamble(browser, ledger["env"], "task-a", monkeypatch, cap=3)

    tid = ns["new_tab"]("https://example.com")
    assert browser.closed == ["idle1"]
    assert set(_tabs(ledger["env"], "idle-task")) == {"idle2"}
    assert set(_tabs(ledger["env"], "busy-task")) == {"busy1"}
    assert tid in _tabs(ledger["env"], "task-a")


def test_global_cap_raises_when_only_active_owners_hold_tabs(ledger, monkeypatch):
    browser = FakeBrowser("user1", "b1", "b2", "b3")
    _write(ledger["env"], "busy-task", ["b1", "b2", "b3"], last_used=time.time())
    ns = _preamble(browser, ledger["env"], "task-a", monkeypatch, cap=3)

    with pytest.raises(RuntimeError, match=r"limit 3\).*close_tab"):
        ns["new_tab"]("https://example.com")
    assert browser.closed == []
    assert set(browser.targets) == {"user1", "b1", "b2", "b3"}


def test_cleanup_browser_closes_exactly_that_owners_tabs(ledger, cli):
    cli.browser = FakeBrowser("user1", "a1", "a2", "b1")
    _write(ledger["env"], "task-a", ["a1", "a2"], last_used=time.time())
    _write(ledger["env"], "task-b", ["b1"], last_used=time.time())

    bt_lifecycle.cleanup_browser("task-a")

    assert sorted(cli.browser.closed) == ["a1", "a2"]
    assert set(cli.browser.targets) == {"user1", "b1"}
    assert [c["env"]["BU_NAME"] for c in cli.calls] == ["default"]
    assert cli.calls[0]["env"]["BH_REQUIRE_EXISTING_DAEMON"] == "1"  # never spawns a daemon
    assert not bu._tab_ledger_path(ledger["env"], "task-a").exists()
    assert set(_tabs(ledger["env"], "task-b")) == {"b1"}


def test_reaper_closes_tabs_of_idle_owners_only(ledger, cli, monkeypatch):
    monkeypatch.setattr(bu, "_read_browser_cfg", lambda: {"agent_tab_idle_s": 1800})
    cli.browser = FakeBrowser("user1", "old1", "new1")
    now = time.time()
    _write(ledger["env"], "old-task", ["old1"], last_used=now - 3600)
    _write(ledger["env"], "new-task", ["new1"], last_used=now - 60)

    bu.reap_orphaned_harness_daemons(ledger["env"], now=now)

    assert cli.browser.closed == ["old1"]
    assert not bu._tab_ledger_path(ledger["env"], "old-task").exists()
    assert set(_tabs(ledger["env"], "new-task")) == {"new1"}


def test_ids_not_in_ledger_are_never_closed(ledger, cli):
    cli.browser = FakeBrowser("user1", "user2")
    _write(ledger["env"], "task-a", ["gone-already"], last_used=time.time())

    assert bu.close_agent_tabs("task-a") == []
    assert cli.browser.closed == []
    assert set(cli.browser.targets) == {"user1", "user2"}


def test_corrupt_ledger_file_is_treated_as_empty(ledger, cli, monkeypatch):
    path = bu._tab_ledger_path(ledger["env"], "task-a")
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    cli.browser = FakeBrowser("user1")

    assert bu.close_agent_tabs("task-a") == []
    assert cli.calls == []  # nothing to close: no CLI round trip

    path.write_text("[1, 2]", encoding="utf-8")
    ns = _preamble(cli.browser, ledger["env"], "task-a", monkeypatch)
    tid = ns["new_tab"]("https://example.com")
    assert set(_tabs(ledger["env"], "task-a")) == {tid}


@pytest.mark.platforms("posix")  # the fake CLI is a /bin/sh script
def test_browser_exec_ships_ledger_preamble_on_shared_browser(ledger, tmp_path, monkeypatch):
    """Unnamed sessions on a shared browser get the ledger (before the own-tab pin for named ones), with
    the owner's ledger path and caps exported, and the call refreshes the owner's last_used."""
    from tools import browser_tool_cloud as bt_cloud

    monkeypatch.setattr("tools.browser_tool_cdp._get_cdp_override", lambda: "http://127.0.0.1:9222")
    monkeypatch.setattr(bt_cloud, "_get_cloud_provider", lambda: None)
    monkeypatch.setattr(bu, "_attach_vault_supervisor", lambda env, task_id: None)
    monkeypatch.setattr(bu, "_read_browser_cfg", lambda: {"agent_tab_cap_per_owner": 2})
    monkeypatch.setattr(bu, "_base_subprocess_env", lambda: {"PATH": os.environ["PATH"], **ledger["env"]})
    script = tmp_path / "browser-use"
    script.write_text('#!/bin/sh\ncat\necho "ledger=$HERMES_AGENT_TAB_LEDGER cap=$HERMES_AGENT_TAB_CAP_OWNER"\n', encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr(bu, "_find_cli", lambda: [str(script)])

    out = json.loads(bu.browser_exec("print('payload')", task_id="task/a"))["output"]
    path = bu._tab_ledger_path(ledger["env"], "task/a")
    assert path.name == "task_a.json"
    assert f"ledger={path} cap=2" in out
    assert "_hermes_tab_ledger" in out and "_hermes_ensure_own_tab" not in out
    assert bu._read_tab_ledger(path)["last_used"] > time.time() - 60

    named = json.loads(bu.browser_exec("print('payload')", session="r7k2", task_id="task/a"))["output"]
    assert named.index("_hermes_tab_ledger") < named.index("_hermes_ensure_own_tab") < named.index("payload")
