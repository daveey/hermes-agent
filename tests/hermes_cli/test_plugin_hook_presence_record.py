"""A cold process answers ``has_hook`` from the recorded hook set instead of discovering every plugin.

Each step runs in a fresh interpreter (the kanban gate helper's cold path) against a real isolated
home with real user plugins: ``assign_task`` gates ``on_kanban_task_updated`` on ``has_hook``, which
used to import every plugin first (38-57 s on a loaded Mac). The record must never drop an observer:
a stale, missing or incomplete record falls back to the full discovery. [daveey]
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

PROBE = textwrap.dedent("""
    import json, os, sys
    from hermes_cli import plugins as P
    from hermes_cli.plugins_discovery import plugin_discovery_suppressed
    calls = {"discover_and_load": 0, "sweeps": 0}
    _orig_dl = P.PluginManager.discover_and_load
    def _spy_dl(self, *a, **k):
        if not plugin_discovery_suppressed():
            calls["discover_and_load"] += 1
        return _orig_dl(self, *a, **k)
    P.PluginManager.discover_and_load = _spy_dl
    _orig_inner = P.PluginManager._discover_and_load_inner
    def _spy_inner(self, *a, **k):
        calls["sweeps"] += 1
        return _orig_inner(self, *a, **k)
    P.PluginManager._discover_and_load_inner = _spy_inner
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    mode, args = sys.argv[1], json.loads(sys.argv[2])
    out = {}
    if mode == "setup":
        kb.init_db()
        conn = kbc.connect()
        out["task_id"] = kb.create_task(conn, title="t", assignee=args.get("assignee", "alice"), requires=args.get("requires"))
        conn.close()
    elif mode == "discover":   # a long-lived surface's normal startup discovery
        P.discover_plugins()
    elif mode == "assign":
        conn = kbc.connect()
        if args.get("kill_in_notify"):
            def _die(*a, **k):
                os._exit(9)    # the gate helper killed at its timeout, mid-notify
            kb.notify_task_updated = _die
        out["assigned"] = kb.assign_task(conn, args["task_id"], args["profile"])
        conn.close()
    out.update(calls)
    print("RESULT " + json.dumps(out))
""")


def _run(home: Path, mode: str, *, expect_exit: int = 0, **args) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HERMES_", "PYTEST"))}
    pythonpath = os.pathsep.join([str(REPO), *(os.path.abspath(p) for p in sys.path if p)])
    env.update(HERMES_HOME=str(home), HOME=str(home.parent), PYTHONPATH=pythonpath, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-c", PROBE, mode, json.dumps(args)], cwd=str(home.parent),
                          env=env, capture_output=True, text=True, timeout=180, check=False)
    assert proc.returncode == expect_exit, proc.stdout[-2000:] + proc.stderr[-4000:]
    if expect_exit:
        return {}
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, proc.stdout[-2000:] + proc.stderr[-4000:]
    return json.loads(line[len("RESULT "):])


def _plugin(home: Path, name: str, body: str, manifest_extra: str = "") -> Path:
    d = home / "plugins" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.yaml").write_text(f"name: {name}\nversion: 0.1.0\ndescription: test plugin\n{manifest_extra}")
    (d / "__init__.py").write_text(textwrap.dedent(body))
    return d


# A plugin that handles a hook nobody gates on kanban assign: discovery has real work to do.
BYSTANDER = """
    def register(ctx):
        ctx.register_hook("on_session_start", lambda **kw: None)
"""

# An observer that appends every on_kanban_task_updated payload it receives to a file.
OBSERVER = """
    import json, os
    def _on_update(**kw):
        path = os.path.join(os.environ["HERMES_HOME"], "observed.jsonl")
        with open(path, "a") as fh:
            fh.write(json.dumps({"task_id": kw.get("task_id"), "changed_fields": kw.get("changed_fields")}) + "\\n")
    def register(ctx):
        ctx.register_hook("on_kanban_task_updated", _on_update)
"""

SLOW = """
    import time
    def register(ctx):
        time.sleep(5)
        ctx.register_hook("on_kanban_task_updated", lambda **kw: None)
"""


def _config(home: Path, enabled: list[str], extra: str = "") -> None:
    (home / "config.yaml").write_text(f"plugins:\n  enabled: [{', '.join(enabled)}]\n{extra}")


def _hooks_record(home: Path) -> dict | None:
    path = home / "cache" / "plugin_hooks.json"
    return json.loads(path.read_text()) if path.exists() else None


def _observed(home: Path) -> list[dict]:
    path = home / "observed.jsonl"
    return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "hermes"
    h.mkdir()
    _plugin(h, "bystander", BYSTANDER)
    return h


@pytest.mark.parametrize("requires", [None, ["terminal", "file"]])
def test_cold_assign_with_a_recorded_hook_set_does_no_discovery(home, requires):
    _config(home, ["bystander"])
    tid = _run(home, "setup", requires=requires, assignee="default" if requires else "alice")["task_id"]
    _run(home, "discover")
    record = _hooks_record(home)
    assert record and "on_session_start" in record["hooks"]
    assert "on_kanban_task_updated" not in record["hooks"]
    if requires:
        _run(home, "assign", task_id=tid, profile="default")  # seeds the admission toolset record too
    out = _run(home, "assign", task_id=tid, profile="bob" if not requires else "default")
    assert out["assigned"] is True
    assert out["sweeps"] == 0 and out["discover_and_load"] == 0, out


def test_missing_record_falls_back_to_discovery_and_records_for_the_next_process(home):
    _config(home, ["bystander"])
    tid = _run(home, "setup")["task_id"]
    (home / "cache" / "plugin_hooks.json").unlink(missing_ok=True)
    first = _run(home, "assign", task_id=tid, profile="bob")
    assert first["assigned"] is True and first["sweeps"] == 1
    assert _hooks_record(home) is not None
    second = _run(home, "assign", task_id=tid, profile="carol")
    assert second["sweeps"] == 0 and second["discover_and_load"] == 0


def test_stale_record_falls_back_and_a_new_observer_still_receives_the_event(home):
    _config(home, ["bystander"])
    tid = _run(home, "setup")["task_id"]
    _run(home, "discover")
    assert "on_kanban_task_updated" not in _hooks_record(home)["hooks"]
    # Installing an observer after the record was taken changes the discovery inputs.
    _plugin(home, "observer", OBSERVER)
    _config(home, ["bystander", "observer"])
    out = _run(home, "assign", task_id=tid, profile="bob")
    assert out["assigned"] is True and out["sweeps"] == 1
    assert _observed(home) == [{"task_id": tid, "changed_fields": ["assignee"]}]


def test_recorded_observer_is_served_true_and_receives_the_event_exactly_once(home):
    _plugin(home, "observer", OBSERVER)
    _config(home, ["bystander", "observer"])
    tid = _run(home, "setup")["task_id"]
    _run(home, "discover")
    assert "on_kanban_task_updated" in _hooks_record(home)["hooks"]
    (home / "observed.jsonl").unlink(missing_ok=True)
    out = _run(home, "assign", task_id=tid, profile="bob")
    assert out["assigned"] is True
    assert out["sweeps"] == 1  # delivery needs the plugin loaded; the record only answered "yes"
    assert _observed(home) == [{"task_id": tid, "changed_fields": ["assignee"]}]


def test_manifest_declared_hook_counts_even_when_not_registered(home):
    _plugin(home, "declarer", "def register(ctx):\n    pass\n",
            manifest_extra="provides_hooks: [on_kanban_task_updated]\n")
    _config(home, ["bystander", "declarer"])
    tid = _run(home, "setup")["task_id"]
    _run(home, "discover")
    assert "on_kanban_task_updated" in _hooks_record(home)["hooks"]
    out = _run(home, "assign", task_id=tid, profile="bob")
    assert out["assigned"] is True and out["sweeps"] == 1  # served True: the invoke discovers


def test_sweep_with_a_timed_out_plugin_does_not_vouch_for_the_hook_set(home):
    _plugin(home, "slow", SLOW)
    _config(home, ["bystander", "slow"], extra="  load_timeout_seconds: 1\n")
    tid = _run(home, "setup")["task_id"]
    _run(home, "discover")
    assert _hooks_record(home) is None  # the slow plugin might register the observer when it is not slow
    out = _run(home, "assign", task_id=tid, profile="bob")
    assert out["sweeps"] == 1


def test_assignment_is_committed_before_notify_so_a_killed_helper_leaves_it_whole(home):
    import sqlite3

    _config(home, ["bystander"])
    tid = _run(home, "setup")["task_id"]
    _run(home, "assign", expect_exit=9, task_id=tid, profile="bob", kill_in_notify=True)
    db = next(home.rglob("kanban.db"))
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT assignee FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "bob"
        events = con.execute("SELECT kind, payload FROM task_events WHERE task_id = ? AND kind = 'assigned'",
                             (tid,)).fetchall()
        assert [json.loads(p)["assignee"] for _, p in events] == ["bob"]
    finally:
        con.close()
