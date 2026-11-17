"""Default ``kanban.llm_pacing`` (``pace_starts: false``): a paced pool holds no start, so the read-only views that
describe pacing holds must not describe one. ``paced_pools`` feeds the priority-raise guard
(``kanban_db_edit.paced_raise_refusal``) and the chat-wait / parent-unclaimed notices
(``kanban_parent_unclaimed._pacing_admission``); each is exercised here on the default config. The opt-in mode is
covered by ``test_kanban_notice_pacing_admission.py`` and ``test_kanban_edit_priority_paced.py``. [daveey]"""
from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_chat_wait as cw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_llm_pacing as lp
from hermes_cli import kanban_llm_pacing_dispatch as pd
from tests.hermes_cli import test_kanban_edit_priority_paced as edit_paced
from tests.hermes_cli import test_kanban_notice_pacing_admission as notices

hub = notices.hub
board = notices.board


@pytest.fixture(autouse=True)
def _default_config(monkeypatch):
    monkeypatch.setattr(pd, "_CACHE", {})
    assert pd.load_config().pace_starts is False  # this file runs on the shipped default


def _anthropic_card(conn, priority=900):
    return kb.create_task(conn, title="paced pool card", assignee="default", priority=priority,
                          model_override=notices.OPUS, provider_override="anthropic")


def test_a_paced_pool_is_no_hold_to_describe_and_a_held_one_still_is(board):
    with kbc.connect_closing() as conn:
        tid = _anthropic_card(conn)
        notices._store(("anthropic", lp.PACED))
        assert pd.paced_pools(conn, tid) is None
        until = time.time() + 3600
        notices._store(("anthropic", lp.HELD, until))
        assert [(p.provider, p.state) for p in pd.paced_pools(conn, tid)] == [("anthropic", lp.HELD)]


def test_raising_priority_on_a_paced_pool_card_is_not_refused(board, capsys):
    with kbc.connect_closing() as conn:
        tid = _anthropic_card(conn)
    notices._store(("anthropic", lp.PACED))
    assert edit_paced._main(["edit", tid, "--priority", "1000"]) == 0, capsys.readouterr().err
    assert edit_paced._state(tid)[0] == 1000


def test_raising_priority_on_a_held_pool_card_is_still_refused_without_a_reason(board, capsys):
    with kbc.connect_closing() as conn:
        tid = _anthropic_card(conn)
    notices._store(("anthropic", lp.HELD, time.time() + 3600))
    assert edit_paced._main(["edit", tid, "--priority", "1000"]) == 1
    assert "llm_pacing" in capsys.readouterr().err and edit_paced._state(tid)[0] == 900


def test_chat_wait_notice_for_a_paced_pool_card_keeps_its_ordinary_words(hub):
    notices._store(("anthropic", lp.PACED))
    tid = notices._hub_card(hub.conn)
    payload, text = notices._chat_wait(hub.conn, tid)
    assert (payload.get("admission") or {}).get("reason") != "llm_pacing", payload
    assert "llm_pacing" not in text and "back on pace" not in text, text


def test_parent_unclaimed_notice_for_a_paced_pool_card_keeps_its_ordinary_words(hub):
    notices._store(("anthropic", lp.PACED))
    tid = notices._hub_card(hub.conn)
    payload, text = notices._parent_unclaimed(hub.conn, tid)
    assert (payload.get("admission") or {}).get("reason") != "llm_pacing", payload
    assert "llm_pacing" not in text and "back on pace" not in text, text


def test_held_pool_notice_is_unchanged_by_default(hub):
    notices._store(("anthropic", lp.HELD, time.time() + 3600))
    tid = notices._hub_card(hub.conn)
    payload, text = notices._chat_wait(hub.conn, tid)
    assert payload["admission"]["reason"] == "llm_pacing", payload
    assert cw.NOTICE_KIND and "llm_pacing" in text, text
