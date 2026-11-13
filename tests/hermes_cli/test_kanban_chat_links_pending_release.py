"""A done replica card whose chat link is not yet released reads ``done_pending_release``, never ``unknown``. [daveey]

Origin chat 20261026_235722_bef6c6 (David-MBP, ``links_reader: replica``, 2026-11-13 08:34-08:36 PST): worker cards
that had just finished (t_01a1d5ad588a78e9, t_01a1d5c0c1d54f8d, t_01a1d5c039be0093) were listed ``unknown`` by
``hermes kanban chat-tasks`` and shown to David as ``[unknown]`` by the ask-gate, for the one or two minutes between
the replica carrying the owner's ``done`` and the owner releasing the link after it verified the completion. The
card's state was known: done, release pending. These pin that label, and that it stays fail-closed: the card is still
the chat's unfinished obligation and counts until the release arrives.
"""

from __future__ import annotations

from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_kanban_chat_links_parity as parity

HUB, assert_parity, counted, replica = parity.HUB, parity.assert_parity, parity.counted, parity.replica
env = parity.env

CLOSED = ksa.LISTED_STATUSES + ("done", "archived")  # what ``hermes kanban chat-tasks`` asks for


def chat_tasks(*lineage):
    return {r["id"]: r for r in ksa.session_tasks(list(lineage), CLOSED) if r.get("remote")}


def test_done_card_reads_pending_release_then_released_one_pass_later(env):
    # Pass 1: the replica has the owner's ``done``; the owner has not released the link yet.
    replica(env, [("t_done", "done", HUB)], [("t_done", "s1", "created", 0)])
    row = chat_tasks("s1")["t_done"]
    assert (row["status"], row["reported_status"], row["unfinished"], row["released"]) == (
        "done_pending_release", "done", True, False)
    assert counted("s1")[0] == 1  # still the chat's obligation: never counted as released
    # Pass 2: the owner verified the completion and released the link.
    replica(env, [("t_done", "done", HUB)], [("t_done", "s1", "created", 1)])
    row = chat_tasks("s1")["t_done"]
    assert (row["status"], row["unfinished"], row["released"]) == ("done", False, True)
    assert counted("s1")[0] == 0


def test_done_card_that_never_verifies_is_never_released(env):
    for _ in range(3):
        replica(env, [("t_done", "done", HUB)], [("t_done", "s1", "shepherd", 0)])
        row = chat_tasks("s1")["t_done"]
        assert (row["status"], row["released"], row["unfinished"]) == ("done_pending_release", False, True)
        assert counted("s1")[0] == 1


def test_pending_release_lists_and_counts_like_the_unknown_obligation(env):
    replica(env, [("t_open", "running", HUB), ("t_done", "done", HUB)],
            [("t_open", "s1", "created", 0), ("t_done", "s1", "created", 0)])
    _, rows = assert_parity("s1", expect=2)  # default listing (open statuses) still carries it
    assert {r["id"]: r["status"] for r in rows} == {"t_open": "running", "t_done": "done_pending_release"}


def test_rows_with_no_known_done_status_stay_unknown(env):
    replica(env, [("t_none", None, HUB), ("t_arch", "archived", HUB)],
            [("t_none", "s1", "created", 0), ("t_arch", "s1", "created", 0), ("t_gone", "s1", "created", 0)])
    rows = chat_tasks("s1")
    assert {tid: rows[tid]["status"] for tid in ("t_none", "t_arch", "t_gone")} == {
        "t_none": "unknown", "t_arch": "unknown", "t_gone": "unknown"}
    assert all(rows[t]["unfinished"] and not rows[t]["released"] for t in rows)
