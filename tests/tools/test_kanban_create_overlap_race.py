"""Two concurrent creates of one failure: the insert re-checks overlap inside its write transaction. [daveey]

Incident (2026-11-03): t_01a19f0a4cc8b092 (a worker's ``kanban_create`` tool call) and t_01a19f0a4d82213d
(ci-main-watch's ``hermes kanban create`` subprocess) were inserted 186 ms apart and both own
``test_termux_assembly_permissions``. Each process ran the overlap check before the other's row existed, so both
passed it. The replay below runs the real pair (``kanban_overlap_failure_cases.json``) through the real tool and CLI
entries in two processes, holds both at a barrier after the first check, then lets one insert while the other waits
on the board's write lock. Exactly one card may land; the other must get the ``create_overlap`` refusal.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_overlap as ko
from tests.tools import test_kanban_create_overlap as helpers

home = helpers.home

REPO = Path(__file__).resolve().parents[2]
CASES = json.loads((Path(__file__).with_name("kanban_overlap_failure_cases.json")).read_text(encoding="utf-8"))
CMW_1103, WORKER_1103 = "t_01a19f0a4d82213d", "t_01a19f0a4cc8b092"

# One create process. It runs its entry path unchanged except for two hooks. ``kb.create_task`` (called after the
# entry's overlap check) waits until both processes reached it. The first process then signals from inside its write
# transaction (``initial_task_state`` runs under BEGIN IMMEDIATE, before the INSERT), and the second process only
# calls the real ``create_task`` after that signal, so it blocks on the write lock until the first commits.
RUNNER = textwrap.dedent("""
    import json, os, sys, threading
    from pathlib import Path
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_graph as graph

    sync, role, entry, case_file, cid = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
    case = json.loads(Path(case_file).read_text(encoding="utf-8"))[cid]
    tick = threading.Event()

    def wait_for(*names):
        for _ in range(3000):  # 60 s
            if all((sync / n).exists() for n in names):
                return
            tick.wait(0.02)
        raise SystemExit(f"barrier timeout waiting for {names}")

    real_create, real_state = kb.create_task, graph.initial_task_state

    def create_task(conn, **kw):
        (sync / f"{role}.checked").touch()
        wait_for("first.checked", "second.checked")  # both passed the first check, neither inserted
        if role == "second":
            wait_for("first.locked")  # the first holds the write lock; this create queues behind it
        return real_create(conn, **kw)

    def initial_task_state(conn, *a, **kw):
        if role == "first":
            (sync / "first.locked").touch()
        return real_state(conn, *a, **kw)

    kb.create_task, graph.initial_task_state = create_task, initial_task_state
    if entry == "tool":
        from tools import kanban_tools as kt
        print(kt._handle_create({"title": case["title"], "body": case["body"], "assignee": "default",
                                 "requires": [], "idempotency_key": case["key"]}))
    else:
        from hermes_cli import kanban as kc
        from hermes_cli.main import main
        kc._check_dispatcher_presence = lambda **kw: (True, "")
        sys.argv = ["hermes", "kanban", "create", case["title"], "--body", case["body"], "--assignee", "default",
                    "--idempotency-key", case["key"], "--json"]
        try:
            main()
        except SystemExit as exc:
            sys.exit(exc.code)
""")


def _spawn(tmp_path, sync, role, entry, cid):
    # Each contender runs as its own durably registered human chat.
    helpers.register_chat(Path(os.environ["HERMES_HOME"]), f"chat-{entry}-{cid}")
    env = {**os.environ, "HERMES_HOME": os.environ["HERMES_HOME"], "HERMES_HUB_STATE": str(tmp_path / "hub-state"),
           "PYTHONPATH": os.pathsep.join([str(REPO), os.environ.get("PYTHONPATH", "")]),
           "HERMES_SESSION_ID": f"chat-{entry}-{cid}"}
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        env.pop(var, None)
    # Keep source in each child's argv: rewriting one shared script can truncate it
    # before a previously spawned interpreter reads it (clean exit, no barrier).
    cases = Path(__file__).with_name("kanban_overlap_failure_cases.json")
    return subprocess.Popen([sys.executable, "-c", RUNNER, str(sync), role, entry, str(cases), cid], cwd=str(REPO),
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_spawn_keeps_pending_child_source_intact(home, tmp_path, monkeypatch):
    """Force the first interpreter to read while the second spawn publishes source."""
    real_popen, real_write = subprocess.Popen, Path.write_text
    procs, observed = [], []
    monkeypatch.setattr(sys.modules[__name__], "RUNNER", "import json, sys; print(json.dumps(sys.argv[1:]))")

    def gated_popen(argv, **kwargs):
        # An owned child waits before exec, not after opening/compiling the runner.
        gate = "import os, sys; sys.stdin.buffer.read(1); os.execv(sys.executable, [sys.executable, *sys.argv[1:]])"
        proc = real_popen([sys.executable, "-c", gate, *argv[1:]], stdin=subprocess.PIPE, **kwargs)
        procs.append(proc)
        return proc

    def publish(path, data, *args, **kwargs):
        if path.name == "race_runner.py" and procs:
            # Path.write_text opens with 'w': a pending interpreter may see this empty inode.
            with path.open("w", encoding="utf-8"):
                observed.append(procs[0].communicate(input="x", timeout=10))
        return real_write(path, data, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", gated_popen)
    monkeypatch.setattr(Path, "write_text", publish)
    try:
        first = _spawn(tmp_path, tmp_path, "first", "tool", WORKER_1103)
        second = _spawn(tmp_path, tmp_path, "second", "cli", CMW_1103)
        if not observed:
            observed.append(first.communicate(input="x", timeout=10))
        second_out, second_err = second.communicate(input="x", timeout=10)
        first_out, first_err = observed[0]
        assert first.returncode == second.returncode == 0, (first_err, second_err)
        assert first_out, (first.returncode, first_out, first_err)
        assert json.loads(first_out)[1:] == ["first", "tool", str(Path(__file__).with_name(
            "kanban_overlap_failure_cases.json")), WORKER_1103]
        assert json.loads(second_out)[1] == "second"
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=10)


def _open_ids() -> list[str]:
    with kbc.connect_closing() as conn:
        return [r["id"] for r in conn.execute(
            f"SELECT id FROM tasks WHERE status IN ({','.join('?' * len(ko.OPEN))})", ko.OPEN)]


def _refusal(out: str) -> dict | None:
    for line in [*out.splitlines()[::-1], out]:
        try:
            got = json.loads(line)
        except ValueError:
            continue
        if isinstance(got, dict) and got.get("code") == ko.EVENT:
            return got
    return None


# The real incident: the worker's card through the tool, ci-main-watch's through the CLI. Either may win the lock.
@pytest.mark.parametrize("first,second", [(("tool", WORKER_1103), ("cli", CMW_1103)),
                                          (("cli", CMW_1103), ("tool", WORKER_1103))],
                         ids=["tool-inserts-cli-refused", "cli-inserts-tool-refused"])
def test_concurrent_pair_lands_one_card_and_one_refusal(home, tmp_path, first, second):
    sync = tmp_path / "sync"
    sync.mkdir()
    procs, outs = {}, {}
    try:
        for role, (entry, cid) in (("first", first), ("second", second)):
            procs[role] = _spawn(tmp_path, sync, role, entry, cid)
        for role, proc in procs.items():
            out, err = proc.communicate(timeout=180)
            outs[role] = (proc.returncode, out, err)
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=10)
    assert all((sync / f"{r}.checked").exists() for r in procs), outs  # both passed the first check
    landed = _open_ids()
    assert len(landed) == 1, (landed, outs)
    rc, out, err = outs["second"]
    refusal = _refusal(out)
    assert refusal is not None, outs["second"]
    assert [m["id"] for m in refusal["matches"]] == landed, refusal
    assert refusal["matches"][0]["why"].startswith("same failing test "), refusal
    if second[0] == "cli":
        assert rc == 2, outs["second"]
    assert outs["first"][0] == 0 and landed[0] in outs["first"][1], outs["first"]


# REST shares the guard: a competing create that commits between the REST check and its insert is refused. A
# competing row stamped ``created_at`` before the check began (its create waited on the write lock for seconds) is
# still re-read: the guard keys on the rowid, not only the time.
@pytest.mark.parametrize("stamped_early_s", [0, 30])
def test_rest_insert_rechecks_inside_its_transaction(home, monkeypatch, stamped_early_s):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from plugins.kanban.dashboard.plugin_api import router
    first, second = CASES[WORKER_1103], CASES[CMW_1103]
    real, raced = kb.create_task, []

    def create_task(conn, **kw):
        with kbc.connect_closing() as other:  # another process's create lands after this request's first check
            raced.append(real(other, title=first["title"], body=first["body"], assignee="default",
                              session_id="other-chat", idempotency_key=first["key"]))
            other.execute("UPDATE tasks SET created_at = created_at - ? WHERE id = ?", (stamped_early_s, raced[-1]))
            other.commit()
        return real(conn, **kw)

    monkeypatch.setattr(kb, "create_task", create_task)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        r = client.post("/tasks", json={"title": second["title"], "body": second["body"], "assignee": "default",
                                        "idempotency_key": second["key"]})
    assert r.status_code == 409, r.text
    detail = r.json().get("detail")
    assert detail["code"] == ko.EVENT and [m["id"] for m in detail["matches"]] == raced
    assert _open_ids() == raced


def test_recheck_reads_only_cards_created_since_the_first_check(home, monkeypatch):
    """An old open look-alike that the first check admitted (distinct_from) is not re-read under the lock."""
    old = CASES[WORKER_1103]
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title=old["title"], body=old["body"], assignee="default", session_id="old-chat")
        conn.execute("UPDATE tasks SET created_at = created_at - 3600 WHERE id = ?", (tid,))
        conn.commit()
    new = CASES[CMW_1103]
    with kbc.connect_closing() as conn:
        refused, after = ko.check(conn, {"body": new["body"], "distinct_from": [tid], "distinct_reason": "separate"},
                                  courier=False, title=new["title"], session_id="new-chat", self_tid=None, parents=[])
        assert refused is None
        seen, real = [], ko._board_cards

        def spy(c, slug, self_gw, *, since=None):
            cards = real(c, slug, self_gw, since=since)
            seen.append((since, [x["id"] for x in cards]))
            return cards

        monkeypatch.setattr(ko, "_board_cards", spy)
        new_tid = kb.create_task(conn, title=new["title"], body=new["body"], assignee="default",
                                 session_id="new-chat", insert_guard=after.guard)
    assert new_tid and len(seen) == 1 and seen[0][0] is not None and seen[0][1] == [], seen
