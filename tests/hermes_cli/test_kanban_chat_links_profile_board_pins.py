"""[daveey] Pins for three clauses of the profile-board ledger fallback (PR #2372) that no test covered: exact-anchor
mutants of each survived the chat-links suites (post-merge check t_01a1d5b4a18ea0f8). A replica row of the same card
dedups the ledger copy, another gateway's or profile's chats keep their profile-board cards, and a chat the link
writer never links (placeholder, machine) is not counted in ``profile_board_links``."""

from __future__ import annotations

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.test_kanban_chat_links import ME, OTHER, _ledger_row, _profile_board_row, _replica


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = kbc.connect(home / "kanban.db")
    ksc.ensure_identity(conn, "default", ME)
    ksa._reset_cache_for_tests()
    try:
        yield home
    finally:
        conn.close()
        ksa._reset_cache_for_tests()


def _rows(got):
    return sorted((c["id"], c["key"], c["source"]) for c in got)


def test_a_replica_row_of_the_card_dedups_its_profile_board_ledger_copy(kanban_home, monkeypatch, tmp_path):
    """The replica holds this chat's link to t_gone but no task row for it, so no carry probe hides the ledger's
    profile-board copy: ``_merge_profile_board`` must skip it because the replica already answered the same chat,
    owner gateway, board and id. With one owner profile the copy has the replica row's key (it would replace the
    replica's answer); with two (the replica labels it ``default``) it has other keys (the card would be listed
    once per label)."""
    rep = tmp_path / "default.db"
    _replica(rep, [], [("t_gone", ME, "s1", "shepherd", 0)])
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [_profile_board_row("t_gone", "s1")])
    got = links.replica_cards(["s1"], ME, replicas=[("default", rep)])
    assert _rows(got) == [("t_gone", f"{OTHER}/bb-tuner/default/t_gone", "replica")]
    two = [_profile_board_row("t_gone", "s1"), _profile_board_row("t_gone", "s1", profile="work")]
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: two)
    got = links.replica_cards(["s1"], ME, replicas=[("default", rep)])
    assert _rows(got) == [("t_gone", f"{OTHER}/default/default/t_gone", "replica")]


@pytest.mark.parametrize("origin", [{"origin_gateway": "mini"}, {"origin_profile": "work"}],
                         ids=["other-gateway", "other-profile"])
def test_another_origins_chat_keeps_its_profile_board_cards(kanban_home, monkeypatch, tmp_path, origin):
    """A profile-board ledger row of a chat on another gateway, or of another origin profile's chat here, is that
    origin's obligation: this reader (gateway ``ME``, profile ``default``) lists only its own, even for the same
    session id."""
    rep = tmp_path / "default.db"
    _replica(rep, [], [])
    rows = [{**_profile_board_row("t_theirs", "s1"), **origin}, _profile_board_row("t_mine", "s1")]
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: rows)
    assert [c["id"] for c in links.replica_cards(["s1"], ME, replicas=[("default", rep)])] == ["t_mine"]
    assert [c["id"] for c in links.replica_cards(None, ME, replicas=[("default", rep)])] == ["t_mine"]


def test_links_diff_does_not_count_an_unlinked_chats_profile_board_cards(kanban_home, monkeypatch, tmp_path):
    """A placeholder chat (``kanban-placement``) is never linked, so links_diff drops it from both sides and counts
    its cards in ``machine_links``; its profile-board card is one of those, not a trusted ``profile_board_links``
    row."""
    rep = tmp_path / "default.db"
    _replica(rep, [("t_open", "open", "running", OTHER)], [("t_open", ME, "s1", "shepherd", 0)])
    rows = [_ledger_row("t_open", "s1"), _profile_board_row("t_pb", "s1"),
            _profile_board_row("t_placed", "kanban-placement")]
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: rows)
    monkeypatch.setattr(ksc, "board_paths", lambda: [("default", kanban_home / "kanban.db")])
    monkeypatch.setattr(ksc, "shadow_path", lambda slug: rep)
    report = links.links_diff(ME)
    assert report["clean"], report
    assert (report["profile_board_links"], report["machine_links"]) == (1, 2), report
    assert (report["ledger_links"], report["replica_links"]) == (2, 2), report
