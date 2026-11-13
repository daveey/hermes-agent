"""Chat-wait notices name the pool workers that could run a home-waiting card. [daveey]

Defect kanban-e7a33105a4 (hermes-hub, 2026-11-10 23:53Z): a chat told David its build cards "start as hub worker
slots free up" while worker-1 could run them. The only queue facts it saw were the chat-wait notice ("16 card(s)
ahead of it, 7/7 worker slots busy"): ``kanban_queue_position.position`` had computed ``pool_could_run`` and
``local_words`` (kanban_show) printed the re-place command, but ``kanban_parent_unclaimed._queue_words``, which
both chat notices and ``ready_hint`` use, dropped it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_chat_wait as cw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_notify as kn
from hermes_cli import kanban_parent_unclaimed as pu
from hermes_cli import kanban_queue_position as kqp

_ENV = ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_HOME", "HERMES_DELEGATED_CHILD_CONTEXT",
        "HERMES_KANBAN_WORKER_TASK")
_PLACE = {"lane": "local", "ahead": 16, "running": 7, "limit": 7, "approximate": True, "board": "default"}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(kbd, "configured_max_in_progress", lambda: 7)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return h


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch):
    """worker-2 carries the card's labels and has a free slot."""
    monkeypatch.setattr(kqp, "_pool_could_run", lambda conn, task_id: ["worker-2"])


def _waiting_card(conn, *, age_s: int = 40 * 60) -> str:
    now = int(time.time())
    tid = kb.create_task(conn, title="waiting", assignee="default")
    kn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="origin-chat", delivery_metadata={"reason": "create"})
    conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (now - age_s, tid))
    conn.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (now - age_s, tid))
    conn.commit()
    return tid


def _notice(conn, tid: str, kind: str) -> dict:
    (payload,) = [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, kind))]
    return payload


def _assert_pool_hint(text: str, tid: str) -> None:
    assert "worker-2" in text
    assert f"hermes kanban move {tid} --runs-on machine:worker-2" in text


def test_queue_words_carry_the_pool_hint_from_the_shared_helper():
    place = {**_PLACE, "pool_could_run": ["worker-2", "worker-1"], "task_id": "t_x"}
    words = pu._queue_words(place)
    assert words.startswith("16 card(s) ahead of it, 7/7 worker slots busy. ")
    assert words.endswith(kqp.pool_words(place))
    assert kqp.pool_words(place) in kqp.local_words(place)  # one wording for kanban_show and the chat notices
    _assert_pool_hint(words, "t_x")
    assert "worker-2, worker-1" in words


def test_queue_words_without_pool_are_unchanged():
    assert pu._queue_words(dict(_PLACE)) == "16 card(s) ahead of it, 7/7 worker slots busy"
    assert pu._queue_words({**_PLACE, "pool_could_run": []}) == "16 card(s) ahead of it, 7/7 worker slots busy"


def test_direct_chat_wait_notice_names_the_pool_worker(home, pool):
    with kbc.connect_closing() as conn:
        tid = _waiting_card(conn)
        assert cw.sweep(conn) == 1
        payload = _notice(conn, tid, cw.NOTICE_KIND)
    _assert_pool_hint(cw.notice_text(payload), tid)


def test_parent_unclaimed_notice_names_the_pool_worker(home, pool):
    with kbc.connect_closing() as conn:
        parent = _waiting_card(conn)
        child = kb.create_task(conn, title="child", assignee="default", parents=[parent])
        kn.add_notify_sub(conn, task_id=child, platform="tui", chat_id="origin-chat",
                          delivery_metadata={"reason": "create"})
        conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'dependency_wait', "
                     "'{}', ?)", (child, int(time.time()) - 40 * 60))
        conn.commit()
        assert pu.sweep(conn) == 1
        payload = _notice(conn, child, pu.NOTICE_KIND)
    _assert_pool_hint(pu.notice_text(payload), parent)


def test_ready_hint_names_the_pool_worker(home, pool):
    with kbc.connect_closing() as conn:
        tid = _waiting_card(conn)
        hint = pu.ready_hint(conn, kb.get_task(conn, tid))
    assert hint is not None
    _assert_pool_hint(hint, tid)


def test_notices_without_pool_name_no_worker(home):
    with kbc.connect_closing() as conn:
        tid = _waiting_card(conn)
        assert cw.sweep(conn) == 1
        text = cw.notice_text(_notice(conn, tid, cw.NOTICE_KIND))
    assert "--runs-on machine:" not in text and "pool worker(s)" not in text
