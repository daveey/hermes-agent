"""Released chat links listed back for readers of closed cards only (core #2312). [daveey]

``kanban_chat_links_released`` lists a chat's RELEASED links to closed cards as explicit ``done``/``archived`` rows
for ``hermes kanban chat-tasks``. #2312 claimed three limits no test pinned (post-merge check t_01a1d59497a6a6d7
found the mutants that drop them surviving): the open-status readers (sidebar counts, handover, links-diff) never
consult it; it reads only released links; and only this origin profile's links count, like ``replica_cards``.
"""
from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_chat_links_released as released
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support

ME, OTHER = "macbook", "hub"
CLOSED = ksa.LISTED_STATUSES + ("done", "archived")


@pytest.fixture
def home(tmp_path, monkeypatch):
    from hermes_cli import profiles
    home = support.kanban_home(tmp_path, monkeypatch)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [])
    links._reset_for_tests()
    yield home
    links._reset_for_tests()


def _replica(path, tasks, chat_links):
    """``tasks``: (id, status); ``chat_links``: (task, chat profile, session, kind, released), all this gateway's."""
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, assignee TEXT, created_at INTEGER, "
        "started_at INTEGER, priority INTEGER, block_kind TEXT, human_request TEXT, created_on TEXT);"
        "CREATE TABLE task_chat_links (uid TEXT, task_id TEXT, chat_gateway TEXT, chat_profile TEXT, chat_session TEXT, "
        "kind TEXT, released INTEGER, created_at INTEGER);")
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, 'worker', 1, NULL, 0, NULL, NULL, ?)",
                     [(t, t, s, OTHER) for t, s in tasks])
    conn.executemany("INSERT INTO task_chat_links VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                     [(links.link_uid(t, ME, p, s, k), t, ME, p, s, k, r) for t, p, s, k, r in chat_links])
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def rep(home, tmp_path):
    return _replica(tmp_path / "default.db",
                    [("t_released", "done"), ("t_unreleased", "done"), ("t_open", "running"),
                     ("t_theirs", "done"), ("t_arch", "archived")],
                    [("t_released", "default", "s1", "created", 1), ("t_unreleased", "default", "s1", "created", 0),
                     ("t_open", "default", "s1", "shepherd", 0), ("t_arch", "default", "s1", "shepherd", 1),
                     # Another origin profile's chat on this gateway, same session id: never this profile's card.
                     ("t_theirs", "bb-tuner", "s1", "created", 1)])


def test_the_released_reader_returns_only_released_links_of_this_profile(rep):
    got = sorted((r["id"], r["chat_profile"]) for r in released.read(rep, ME, "default", ["s1"], CLOSED))
    assert got == [("t_arch", "default"), ("t_released", "default")]
    assert [r["id"] for r in released.read(rep, ME, "bb-tuner", ["s1"], CLOSED)] == ["t_theirs"]


def test_a_closed_card_reader_lists_no_other_profiles_released_link(rep):
    got = {c["id"]: c for c in links.replica_cards(["s1"], ME, CLOSED, replicas=[("default", rep)])}
    assert sorted((t, c["status"], c["released"]) for t, c in got.items()) == [
        ("t_arch", "archived", True), ("t_open", "running", False), ("t_released", "done", True),
        ("t_unreleased", "unknown", False)]
    assert {c["origin_profile"] for c in got.values()} == {"default"}


def test_open_status_readers_never_consult_the_released_reader(rep, home, monkeypatch):
    """The sidebar counts, handover and links-diff read open statuses only: they neither open the replica a second
    time for released links nor grow by them."""
    calls = []
    real = released.read

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(released, "read", spy)
    for statuses in (links.OPEN_STATUSES, ksa.LISTED_STATUSES):
        got = sorted(c["id"] for c in links.replica_cards(["s1"], ME, statuses, replicas=[("default", rep)]))
        assert got == ["t_open", "t_unreleased"]
    monkeypatch.setattr(ksc, "board_paths", lambda: [("default", home / "kanban.db")])
    monkeypatch.setattr(ksc, "shadow_path", lambda slug: rep)
    for statuses in (links.OPEN_STATUSES, ksa.LISTED_STATUSES):
        report = links.links_diff(ME, statuses)
        assert report["replica_links"] == 2 and report["only_in_replica"] == {
            "s1": [(OTHER, "default", "t_open"), (OTHER, "default", "t_unreleased")]}
    assert calls == []
    # The spy sees the closed-card reader, so the empty list above is not a dead spy.
    links.replica_cards(["s1"], ME, CLOSED, replicas=[("default", rep)])
    assert len(calls) == 1
