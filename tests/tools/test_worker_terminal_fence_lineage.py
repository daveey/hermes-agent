"""[daveey] A Kanban worker's own terminal is write-fenced by design, before and after delegate_task.

A worker (t_01a1873db0b8fcd1) ran a live board-repair script from its terminal after two
synchronous delegate_task reviews, got ``PermissionError: delegate_task child contexts cannot
mutate Kanban tasks or boards``, and concluded the children's lineage had leaked into its shell.
It had not: ``scrub_kanban_env`` gives EVERY tool subprocess of a worker the
``HERMES_DELEGATED_CHILD_CONTEXT`` marker (owner = the worker's own pid) and drops
``HERMES_KANBAN_TASK``, delegation or not. The refusal text named only delegate_task, so it sent
the worker after the wrong cause. These tests pin both halves:

* the parent worker's terminal env is identical before, during-sibling and after a delegate child;
* a worker-terminal descendant's DB refusal names the worker-terminal fence and the authorized
  path, while an in-process delegate child keeps its own refusal text.
"""
import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from agent.delegation_context import (
    DELEGATED_CHILD_ENV_MARKER,
    DELEGATED_CHILD_OWNER_ENV,
    KANBAN_WORKER_TASK_ENV,
    delegated_child_context,
)
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect, write_txn
from tools.environments.local import LocalEnvironment

ROOT = Path(__file__).resolve().parents[2]
KEYS = ("HERMES_KANBAN_TASK", KANBAN_WORKER_TASK_ENV, DELEGATED_CHILD_ENV_MARKER, DELEGATED_CHILD_OWNER_ENV)
PROBE = f"import os, json; print('ENV_RESULT=' + json.dumps({{k: os.environ.get(k) for k in {KEYS!r}}}))"


@pytest.fixture
def worker(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    db = home / "kanban.db"
    conn = connect(db)
    own = kb.create_task(conn, title="own")
    kb.claim_task(conn, own)
    task = kb.get_task(conn, own)
    conn.close()
    for key, value in {
        "HERMES_KANBAN_DB": str(db), "HERMES_KANBAN_BOARD": "default",
        "HERMES_KANBAN_TASK": own, "HERMES_KANBAN_RUN_ID": str(task.current_run_id),
        "HERMES_KANBAN_CLAIM_LOCK": task.claim_lock,
    }.items():
        monkeypatch.setenv(key, value)
    for key in (DELEGATED_CHILD_ENV_MARKER, DELEGATED_CHILD_OWNER_ENV, KANBAN_WORKER_TASK_ENV):
        monkeypatch.delenv(key, raising=False)
    return own, db


def _run(tmp_path, code: str) -> str:
    script = tmp_path / f"probe_{abs(hash(code))}.py"
    script.write_text(f"import sys; sys.path.insert(0, {str(ROOT)!r})\n{code}\n")
    terminal = LocalEnvironment(cwd=str(tmp_path), timeout=60)
    try:
        return terminal.execute(f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}", timeout=60).get("output", "")
    finally:
        terminal.cleanup()


def _env(tmp_path) -> dict:
    out = _run(tmp_path, PROBE)
    return json.loads(next(line.split("ENV_RESULT=", 1)[1] for line in out.splitlines() if "ENV_RESULT=" in line))


def test_parent_worker_terminal_lineage_is_unchanged_by_delegate_child(worker, tmp_path):
    own, _db = worker
    before = _env(tmp_path)
    # The worker's own terminal: write-fenced marker owned by THIS (worker) process, no identity.
    assert before["HERMES_KANBAN_TASK"] is None
    assert before[KANBAN_WORKER_TASK_ENV] == own
    assert before[DELEGATED_CHILD_ENV_MARKER]
    assert before[DELEGATED_CHILD_OWNER_ENV].split(":")[0] == str(os.getpid())

    with delegated_child_context():
        child = _env(tmp_path)
    after = _env(tmp_path)

    assert after == before, "a finished delegate child must not change the parent's terminal env"
    assert child[DELEGATED_CHILD_OWNER_ENV] == before[DELEGATED_CHILD_OWNER_ENV]
    # The parent process itself is not fenced once the child returned: its kanban tools still write.
    conn = connect(_db)
    try:
        kb.add_comment(conn, own, "worker", "parent still owns its card")
    finally:
        conn.close()


WRITE_FROM_TERMINAL = """
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
conn = connect()
try:
    kb.create_task(conn, title="from a worker shell")
    print("REFUSAL=<none>")
except PermissionError as exc:
    print("REFUSAL=" + str(exc))
"""


def _refusal(out: str) -> str:
    return next(line.split("REFUSAL=", 1)[1] for line in out.splitlines() if line.startswith("REFUSAL="))


def test_worker_terminal_db_refusal_names_the_worker_fence(worker, tmp_path):
    own, _db = worker
    text = _refusal(_run(tmp_path, WRITE_FROM_TERMINAL))
    assert text != "<none>", "a worker's terminal must stay write-fenced"
    assert "cannot mutate Kanban" in text
    # Names the real cause, not only delegate_task, and the authorized paths.
    assert "Kanban worker" in text and own in text
    assert str(os.getpid()) in text, "the refusal must name the agent process that fenced the lineage"
    assert "by design" in text and "kanban_*" in text
    assert not text.startswith("delegate_task child contexts")


def test_worker_terminal_db_refusal_after_delegate_child_is_the_same(worker, tmp_path):
    before = _refusal(_run(tmp_path, WRITE_FROM_TERMINAL))
    with delegated_child_context():
        pass
    after = _refusal(_run(tmp_path, WRITE_FROM_TERMINAL))
    assert after == before


def test_in_process_delegate_child_keeps_its_refusal(worker):
    _own, db = worker
    conn = connect(db)
    try:
        with delegated_child_context():
            with pytest.raises(PermissionError) as exc:
                with write_txn(conn):
                    pass
    finally:
        conn.close()
    assert str(exc.value).startswith("delegate_task child contexts cannot mutate Kanban tasks or boards")
