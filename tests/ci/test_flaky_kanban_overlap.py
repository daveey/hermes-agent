"""scripts/ci/flaky_kanban.py when the core overlap guard refuses the create (t_01a189b2c78e9005).

Regression: the guard named three unrelated cards on gateway macbook (one of them done) as the
owners of three flaky tests. ci-flaky-cards commented them through the hub's own `hermes kanban`
(which cannot see another gateway's card), swallowed the error, logged "appended", mapped each
test to the wrong card and exited 0. These tests drive the real CLI path (CliBoard through a
fake `hermes` executable, HubRelay through a fake `hermes-hub` on PATH and a real sqlite replica).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms("posix")

_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("flaky_kanban_overlap", _ROOT / "scripts" / "ci" / "flaky_kanban.py")
fk = importlib.util.module_from_spec(_spec)
sys.modules["flaky_kanban_overlap"] = fk
_spec.loader.exec_module(fk)

TEST = "tests/x.py::test_a"
RUN = "https://github.com/o/r/actions/runs/5/job/9"

FAKE_HERMES = r'''
import json, os, sys
p = os.environ["FAKE_KANBAN"]
s = json.load(open(p))
a = sys.argv[2:]                       # after "kanban"
s["argv"].append(list(a))
board = "default"
if a[0] == "--board":
    board, a = a[1], a[2:]
def done(out="", rc=0, err=""):
    json.dump(s, open(p, "w"))
    if out: print(out)
    if err: print(err, file=sys.stderr)
    sys.exit(rc)
verb = a[0]
if verb == "create":
    dist = [a[i + 1] for i, x in enumerate(a) if x == "--distinct-from"]
    reason = a[a.index("--distinct-reason") + 1] if "--distinct-reason" in a else ""
    if s["refuse"] and not (reason and all(m["id"] in dist for m in s["refuse"])):
        done(json.dumps({"ok": False, "created": False, "code": "create_overlap",
                         "note": "No card was created", "matches": s["refuse"]}), 2)
    tid = "t_new%d" % len(s["cards"])
    s["cards"][tid] = {"id": tid, "title": a[1], "body": sys.stdin.read(), "status": "ready", "comments": [],
                       "distinct_from": dist, "distinct_reason": reason}
    done(json.dumps({"id": tid}))
card = s["cards"].get(a[1]) if len(a) > 1 else None
if card is not None and card.get("board", "default") != board:
    card = None
if verb == "list":
    done(json.dumps([{k: v for k, v in c.items() if k != "comments"} for c in s["cards"].values()
                     if c.get("board", "default") == board]))
if card is None:
    done(rc=1, err="no such task: %s" % (a[1:2],))
if verb == "show":
    done(json.dumps({"task": {k: v for k, v in card.items() if k != "comments"},
                     "comments": [{"body": b} for b in card["comments"]]}))
if verb == "comment":
    card["comments"].append(a[2])
    done()
done(rc=1, err="unsupported")
'''

FAKE_HUB = r'''
import json, os, sqlite3, sys
a = sys.argv[1:]
open(os.environ["FAKE_HUB_LOG"], "a").write(json.dumps(a) + "\n")
gw, coll, key, action = a[1:5]
text = next(x[5:] for i, x in enumerate(a) if i and a[i - 1] == "--arg" and x.startswith("text="))
rid = a[a.index("--id") + 1]
db = sqlite3.connect(os.path.join(os.environ["HERMES_HUB_STATE"], "hub.db"))
if os.environ.get("FAKE_HUB_MODE") == "reject":
    db.execute("INSERT OR IGNORE INTO sent VALUES (?, '{}', 'rejected', ?, 0, 0, 1)",
               (rid, json.dumps({"error": "kanban: no such task"})))
else:
    db.execute("INSERT OR IGNORE INTO sent VALUES (?, '{}', 'done', ?, 0, 0, 1)",
               (rid, json.dumps({"output": "Comment added to " + key.split("/")[-1]})))
    data = json.loads(db.execute("SELECT data FROM replica WHERE gw = ? AND key = ?", (gw, key)).fetchone()[0])
    data["last_comments"] = (data.get("last_comments") or [])[-2:] + [{"author": "hub:hub", "body": text[:500]}]
    db.execute("UPDATE replica SET data = ? WHERE gw = ? AND key = ?", (json.dumps(data), gw, key))
db.commit()
print(rid)
'''


def _exe(path: Path, source: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fake local board (`hermes`), a hermes-hub replica of gateway macbook, and a fake `hermes-hub`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    hermes = _exe(bin_dir / "hermes", FAKE_HERMES)
    _exe(bin_dir / "hermes-hub", FAKE_HUB)
    hub = tmp_path / "hub"
    hub.mkdir()
    db = sqlite3.connect(hub / "hub.db")
    db.executescript(
        "CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);"
        "CREATE TABLE replica (gw TEXT, coll TEXT, key TEXT, version INTEGER, seq INTEGER, data TEXT,"
        " updated_at INTEGER, PRIMARY KEY (gw, coll, key));"
        "CREATE TABLE sent (id TEXT PRIMARY KEY, body TEXT, state TEXT, result TEXT, created_at REAL,"
        " done_at REAL, posted INTEGER DEFAULT 0);"
        "INSERT INTO meta VALUES ('gateway', 'hub');")
    db.commit()
    db.close()
    store = tmp_path / "kanban.json"
    store.write_text(json.dumps({"cards": {}, "refuse": [], "argv": []}))
    monkeypatch.setenv("FAKE_KANBAN", str(store))
    monkeypatch.setenv("FAKE_HUB_LOG", str(tmp_path / "hub.log"))
    monkeypatch.setenv("HERMES_HUB_STATE", str(hub))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    class Env:
        def card(self, cid, status, title, body="", board="default"):
            s = json.loads(store.read_text())
            s["cards"][cid] = {"id": cid, "status": status, "title": title, "body": body, "comments": [],
                               "board": board}
            store.write_text(json.dumps(s))

        def refuse(self, *matches):
            s = json.loads(store.read_text())
            s["refuse"] = list(matches)
            store.write_text(json.dumps(s))

        def remote(self, gw, key, status, title, body=""):
            c = sqlite3.connect(hub / "hub.db")
            data = {"id": key.rsplit("/", 1)[-1], "board": key.split("/")[-2], "status": status, "title": title,
                    "body": body, "gateway": gw, "last_comments": []}
            c.execute("INSERT INTO replica VALUES (?, 'tasks', ?, 1, 1, ?, 0)", (gw, key, json.dumps(data)))
            c.commit()
            c.close()

        def kanban(self):
            return json.loads(store.read_text())

        def hub_calls(self):
            p = tmp_path / "hub.log"
            return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

        def remote_comments(self, gw, key):
            c = sqlite3.connect(hub / "hub.db")
            data = json.loads(c.execute("SELECT data FROM replica WHERE gw = ? AND key = ?", (gw, key)).fetchone()[0])
            c.close()
            return [x["body"] for x in data["last_comments"]]

    e = Env()
    e.hermes, e.tmp = str(hermes), tmp_path
    return e


def _entry(test_id=TEST, excerpt="AssertionError: boom"):
    return {"test_id": test_id, "file": test_id.split("::")[0], "retried": "tests", "first_attempt_excerpt": excerpt}


def _file(env, state):
    return fk.file_cards([_entry()], run_url=RUN, branch="main", board=fk.CliBoard(env.hermes), state=state,
                         relay=fk.HubRelay())


def _report(env) -> Path:
    report = env.tmp / "flaky.json"
    report.write_text(json.dumps({"version": 1, "flaky": [_entry()]}))
    return report


def test_unrelated_overlap_match_is_not_the_owner_the_card_is_filed_distinct_from_it(env):
    env.card("t_other", "ready", "Fix recurring local session GET stalls after 24c3eda", "latency, not tests")
    env.refuse({"id": "t_other", "gateway": "hub", "board": "default", "status": "ready",
                "title": "Fix recurring local session GET stalls after 24c3eda"})
    state = fk.load_state(env.tmp / "absent.json")

    log = _file(env, state)

    board = env.kanban()
    new = [c for c in board["cards"].values() if c["id"] != "t_other"]
    assert len(new) == 1 and new[0]["title"] == f"Deflake {TEST}"
    assert new[0]["distinct_from"] == ["t_other"]
    assert TEST in new[0]["distinct_reason"] and "session GET stalls" in new[0]["distinct_reason"]
    assert board["cards"]["t_other"]["comments"] == []
    assert log == [f"opened {new[0]['id']} {TEST} (distinct from t_other)"]
    assert state["cards"][TEST] == {"card": new[0]["id"], "generation": 0}


def test_closed_overlap_matches_are_ignored_even_when_the_guard_or_a_stale_legacy_row_says_open(env):
    env.card("t_local_done", "done", f"Deflake {TEST}", f"`{TEST}` fixed")
    # Gateway macbook: the canonical row is done, a stale legacy row still says running (real t_d6973c6f).
    env.remote("macbook", "default/hermes/t_remote_done", "done", f"Deflake {TEST}", f"`{TEST}`")
    env.remote("macbook", "hermes/t_remote_done", "running", f"Deflake {TEST}", f"`{TEST}`")
    env.refuse({"id": "t_local_done", "gateway": "hub", "board": "default", "status": "running", "title": "x"},
               {"id": "t_remote_done", "gateway": "macbook", "board": "hermes", "status": "running", "title": "y"})
    state = fk.load_state(env.tmp / "absent.json")

    log = _file(env, state)

    board = env.kanban()
    assert board["cards"]["t_local_done"]["comments"] == []
    assert env.hub_calls() == []                                   # nothing sent to the closed remote card
    new = [c for c in board["cards"].values() if c["id"].startswith("t_new")]
    assert len(new) == 1 and sorted(new[0]["distinct_from"]) == ["t_local_done", "t_remote_done"]
    assert log[0].startswith(f"opened {new[0]['id']} {TEST}")
    assert state["cards"][TEST]["card"] == new[0]["id"]


def test_cross_gateway_owner_with_failed_delivery_is_not_appended_not_recorded_and_exits_non_ok(env, monkeypatch, capsys):
    env.remote("macbook", "default/hermes/t_remote", "ready", f"Deflake {TEST}", f"Flaky test: `{TEST}`")
    env.refuse({"id": "t_remote", "gateway": "macbook", "board": "hermes", "status": "ready", "title": "z"})
    monkeypatch.setenv("FAKE_HUB_MODE", "reject")
    state_path = env.tmp / "state.json"

    rc = fk.main(["--report", str(_report(env)), "--run-url", RUN, "--hermes", env.hermes, "--state", str(state_path)])

    out = capsys.readouterr().out
    assert rc != 0
    assert "appended" not in out and "FAILED" in out and "t_remote" in out
    assert TEST not in fk.load_state(state_path)["cards"]
    assert [c[4] for c in env.hub_calls()] == ["comment"]           # it did try the cross-gateway path
    assert not [c for c in env.kanban()["cards"]]                   # and did not paper over it with a duplicate


def test_failed_delivery_leaves_the_github_run_unprocessed_for_the_next_poll(env, monkeypatch):
    env.remote("macbook", "default/hermes/t_remote", "ready", f"Deflake {TEST}", f"`{TEST}`")
    env.refuse({"id": "t_remote", "gateway": "macbook", "board": "hermes", "status": "ready", "title": "z"})
    monkeypatch.setenv("FAKE_HUB_MODE", "reject")
    from scripts.ci import flaky_artifact as fa
    from tests.ci.flaky_artifact_fakes import FakeGitHub

    gh = FakeGitHub()
    gh.simple(tests=(TEST,))
    monkeypatch.setattr(fk, "_gh", gh.gh)
    monkeypatch.setattr(fa, "download_zip", gh.download)
    state = fk.load_state(env.tmp / "absent.json")
    log = fk.poll_github("o/r", "tok", fk.CliBoard(env.hermes), state, relay=fk.HubRelay())
    assert log[0].startswith("FAILED") and state["runs"] == []


def test_cross_gateway_owner_gets_the_comment_and_it_is_read_back(env, capsys):
    key = "default/hermes/t_remote"
    env.remote("macbook", key, "ready", f"Deflake {TEST}", f"Flaky test: `{TEST}`")
    env.refuse({"id": "t_remote", "gateway": "macbook", "board": "hermes", "status": "ready", "title": "z"})
    state_path = env.tmp / "state.json"

    rc = fk.main(["--report", str(_report(env)), "--run-url", RUN, "--hermes", env.hermes, "--state", str(state_path)])

    out = capsys.readouterr().out
    assert rc == 0, out
    assert f"appended @task:hermes@macbook/t_remote {TEST}" in out and "comment read back" in out
    assert any(TEST in c and RUN in c for c in env.remote_comments("macbook", key))
    assert [c[:5] for c in env.hub_calls()] == [["request", "macbook", "tasks", key, "comment"]]
    assert fk.load_state(state_path)["cards"][TEST] == {"card": "t_remote", "generation": 0,
                                                       "gateway": "macbook", "board": "hermes"}
    assert not env.kanban()["cards"]                                 # no local duplicate

    # The next flake of the same test goes straight to the recorded remote owner, also read back.
    rc = fk.main(["--report", str(_report(env)), "--run-url", RUN + "0", "--hermes", env.hermes,
                  "--state", str(state_path)])
    assert rc == 0 and f"appended @task:hermes@macbook/t_remote {TEST}" in capsys.readouterr().out
    assert len(env.hub_calls()) == 2


def test_local_owner_about_the_test_gets_the_comment_read_back(env):
    """The guard may name a real owner on this gateway (another board): comment it and read it back."""
    env.card("t_owner", "running", "triage", f"`{TEST}` flakes under load", board="ops")
    env.refuse({"id": "t_owner", "gateway": "hub", "board": "ops", "status": "running", "title": "triage"})
    state = fk.load_state(env.tmp / "absent.json")

    log = _file(env, state)

    assert log[0].startswith(f"appended @task:ops/t_owner {TEST}") and "comment read back" in log[0]
    assert any(RUN in c and TEST in c for c in env.kanban()["cards"]["t_owner"]["comments"])
    assert any(argv[:3] == ["--board", "ops", "comment"] for argv in env.kanban()["argv"])
    assert state["cards"][TEST] == {"card": "t_owner", "generation": 0, "board": "ops"}


def test_a_card_that_only_quotes_the_test_id_among_others_is_not_its_owner(env):
    """Live: the guard named t_01a189b2c78e9005 (a card ABOUT this cron) for all three tests its body
    lists in a table. A lead paragraph naming several test ids is not the card that fixes this one."""
    other = "tests/y.py::test_b"
    # On another board here, so the default-board scan does not reach them and the refusal path decides.
    env.card("t_report", "running", "ci-flaky-cards drops flaky tests when the overlap guard names an unrelated card",
             f"Found while verifying the fix.\n\n| test | card |\n| {TEST} | t_1 |\n| {other} | t_2 |", board="ops")
    env.card("t_pair", "ready", "triage", f"`{TEST}` and `{other}` both flake under load", board="ops")
    env.refuse({"id": "t_report", "gateway": "hub", "board": "ops", "status": "running", "title": "r"},
               {"id": "t_pair", "gateway": "hub", "board": "ops", "status": "ready", "title": "p"})
    state = fk.load_state(env.tmp / "absent.json")

    log = _file(env, state)

    cards = env.kanban()["cards"]
    assert cards["t_report"]["comments"] == [] and cards["t_pair"]["comments"] == []
    new = [c for c in cards.values() if c["id"].startswith("t_new")]
    assert len(new) == 1 and sorted(new[0]["distinct_from"]) == ["t_pair", "t_report"]
    assert log == [f"opened {new[0]['id']} {TEST} (distinct from t_report, t_pair)"]


def test_board_scan_skips_a_report_card_that_cites_the_job_and_lists_several_tests(env):
    """Live (repair run): the default-board scan took t_01a189b2c78e9005, whose body lists the three dropped
    tests with their job URLs, as the card 'citing this job' and commented it instead of filing a card."""
    other = "tests/y.py::test_b"
    env.card("t_report", "running", "ci-flaky-cards drops flaky tests when the overlap guard names an unrelated card",
             f"Found while verifying the fix.\n\n| test | job |\n| {TEST} | {RUN} |\n| {other} | {RUN} |")
    state = fk.load_state(env.tmp / "absent.json")

    log = _file(env, state)

    cards = env.kanban()["cards"]
    assert cards["t_report"]["comments"] == []
    new = [c for c in cards.values() if c["id"].startswith("t_new")]
    assert len(new) == 1 and new[0]["title"] == f"Deflake {TEST}"
    assert log == [f"opened {new[0]['id']} {TEST}"]
