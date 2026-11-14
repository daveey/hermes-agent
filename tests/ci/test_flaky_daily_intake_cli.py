"""Daily intake admission exercised through the real script with isolated external effects."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tools.environments.local import build_subprocess_env

ROOT = Path(__file__).resolve().parents[2]
FAKE_BOARD = r'''
import json, os, sys
from pathlib import Path
path = Path(os.environ["FAKE_BOARD"])
state = json.loads(path.read_text())
a = sys.argv[2:]
verb = a[0]
if verb == "list":
    result = list(state["cards"].values())
elif verb == "create":
    key = a[a.index("--idempotency-key") + 1]
    body = sys.stdin.read()
    existing = [c for c in state["cards"].values() if c["idempotency_key"] == key]
    if existing:
        result = existing[0]
    else:
        cid = "t_fixture_%d" % len(state["cards"])
        result = {"id": cid, "title": a[1], "body": body, "status": "ready",
                  "idempotency_key": key, "comments": []}
        state["cards"][cid] = result
        state["creates"] += 1
elif verb == "show":
    c = state["cards"][a[1]]
    result = {"task": c, "comments": [{"body": b} for b in c["comments"]]}
elif verb == "comment":
    state["cards"][a[1]]["comments"].append(a[2])
    result = {}
elif verb == "reopen":
    state["cards"][a[1]]["status"] = "ready"
    result = {}
else:
    raise SystemExit("unexpected external command: " + repr(a))
path.write_text(json.dumps(state))
print(json.dumps(result))
'''


def initialize_cli(state, env):
    command = [sys.executable, str(ROOT / "scripts/ci/flaky_kanban.py"),
               "--state", str(state), "--initialize-intake"]
    result = subprocess.run(command, env=env, capture_output=True, text=True,
                            encoding="utf-8", timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    again = subprocess.run(command, env=env, capture_output=True, text=True,
                           encoding="utf-8", timeout=30, check=False)
    assert again.returncode == 1


@pytest.mark.parametrize("payload", [None, [], {"version": 1, "flaky": ["secret-output"]},
                                    {"version": 1, "flaky": {"secret-output": True}}])
def test_malformed_cli_report_fails_without_traceback_or_payload(tmp_path, payload):
    home = tmp_path / "home"
    home.mkdir()
    report = tmp_path / "flaky.json"
    report.write_text(json.dumps(payload), encoding="utf-8")
    env = build_subprocess_env(extra={"HOME": str(home), "HERMES_HOME": str(home / ".hermes")})
    initialize_cli(home / "cursor.json", env)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/ci/flaky_kanban.py"), "--report", str(report),
         "--repo", "example/project", "--state", str(home / "cursor.json"),
         "--run-url", "https://github.com/example/project/actions/runs/5/job/9", "--branch", "a" * 40],
        env=env, capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
    assert result.returncode == 1
    assert result.stderr == "FAILED intake unavailable; durable retry pending\n"
    assert "secret-output" not in result.stdout
    with sqlite3.connect(home / "cursor.intake.sqlite3") as conn:
        assert conn.execute("SELECT count(*) FROM occurrences").fetchone()[0] == 0


@pytest.mark.parametrize("damage", ["none", "all_queue_files", "all_state", "missing_anchor",
                                    "replacement", "restore"])
def test_thirty_new_tests_create_one_daily_intake_and_thirty_durable_rows(tmp_path, damage):
    home = tmp_path / "home"
    home.mkdir()
    (home / "hub").mkdir()
    with sqlite3.connect(home / "hub/hub.db") as replica:
        replica.execute("CREATE TABLE replica (gw TEXT, key TEXT, coll TEXT, data TEXT)")
        replica.execute("CREATE TABLE meta (k TEXT, v TEXT)")
        replica.execute("INSERT INTO meta VALUES ('gateway', 'fixture')")
    board_path = tmp_path / "board.json"
    board_path.write_text(json.dumps({"cards": {}, "creates": 0}), encoding="utf-8")
    fake = tmp_path / "fake-hermes"
    fake.write_text(f"#!{sys.executable}\n{FAKE_BOARD}", encoding="utf-8")
    fake.chmod(0o700)
    report = tmp_path / "flaky.json"
    report.write_text(json.dumps({"version": 1, "flaky": [
        {"test_id": f"tests/example.py::test_{i}", "retried": "tests"} for i in range(30)
    ]}), encoding="utf-8")
    state = home / "cursor.json"
    env = build_subprocess_env(extra={
        "HOME": str(home), "HERMES_HOME": str(home / ".hermes"),
        "HERMES_HUB_STATE": str(home / "hub"), "FAKE_BOARD": str(board_path),
    })
    command = [sys.executable, str(ROOT / "scripts/ci/flaky_kanban.py"),
               "--report", str(report), "--repo", "example/project", "--state", str(state),
               "--run-url", "https://github.com/example/project/actions/runs/5/job/9",
               "--branch", "a" * 40, "--hermes", str(fake)]
    initialize_cli(state, env)
    first = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", timeout=90, check=False)
    assert first.returncode == 0, first.stdout + first.stderr
    board = json.loads(board_path.read_text(encoding="utf-8"))
    assert board["creates"] == 1, f"expected one intake, created {board['creates']} cards"
    intake = next(iter(board["cards"].values()))
    assert intake["idempotency_key"].startswith("ci-flaky-intake:example/project:")
    queue = state.with_suffix(".intake.sqlite3")
    with sqlite3.connect(queue) as conn:
        assert conn.execute("SELECT count(*) FROM occurrences").fetchone()[0] == 30
        assert conn.execute("SELECT count(*) FROM occurrences WHERE delivered = 1").fetchone()[0] == 30
    second = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", timeout=90, check=False)
    assert second.returncode == 0, second.stdout + second.stderr
    assert json.loads(board_path.read_text(encoding="utf-8")) == board
    # Exercise actual consistent backup, not a hand-written replacement schema.
    backup = tmp_path / "backup.sqlite3"
    with sqlite3.connect(queue) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    if damage != "none":
        if damage != "missing_anchor":
            for path in home.glob("cursor.intake.*"):
                path.unlink()
        if damage in {"all_state", "missing_anchor"}:
            for path in (home / ".hermes/intake-identities").glob("*.uuid"):
                path.unlink()
        if damage == "all_state":
            state.unlink()
        if damage == "replacement":
            from scripts.ci.flaky_intake import Journal
            replacement = Journal(queue, identity_path=tmp_path / "other.uuid", initialize=True)
            replacement.close()
        elif damage == "restore":
            with sqlite3.connect(backup) as source, sqlite3.connect(queue) as destination:
                source.backup(destination)
        damaged = subprocess.run(command, env=env, capture_output=True, text=True,
                                 encoding="utf-8", timeout=90, check=False)
        assert damaged.returncode == (0 if damage == "restore" else 1), damaged.stdout + damaged.stderr
        assert json.loads(board_path.read_text(encoding="utf-8")) == board
        if damage != "restore":
            return
        with sqlite3.connect(queue) as conn:
            assert conn.execute("SELECT count(*) FROM occurrences WHERE delivered=1").fetchone()[0] == 30
    # Losing only the polling cache must not lose or duplicate durable admission.
    state.unlink()
    recovered = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", timeout=90, check=False)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert json.loads(board_path.read_text(encoding="utf-8")) == board
