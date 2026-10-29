"""``diff`` and foreign cards' chat links: a picked-up copy of another gateway's card never carries that gateway's
chat links (hub t_01a1895d106aeb02, 2026-10-29: child_mismatch task_chat_links with pending=0)."""

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli.test_kanban_cross_gateway_parents import (
    BOARD, ME, OTHER, _other, _round, kanban_home, live, svc,
)

__all__ = ["kanban_home", "live", "svc"]


def _foreign_copy(live, svc, kanban_home):
    _other(svc, "card", "tasks", "t_foreign", {"title": "theirs", "status": "running", "created_at": 1,
                                                 "created_on": OTHER})
    _other(svc, "chat", "task_chat_links", f"t_foreign:{OTHER}:-:s1:created",
           {"task_id": "t_foreign", "chat_gateway": OTHER, "chat_session": "s1", "kind": "created", "released": 0,
            "created_at": 1})
    _round(kanban_home, svc)
    replica = ksc.open_shadow(BOARD, ME)
    try:
        replica.row_factory = ksc.sqlite3.Row
        assert wn.import_cards(live, replica, ["t_foreign"]) == ["t_foreign"]
    finally:
        replica.close()
    assert not live.execute("SELECT 1 FROM task_chat_links WHERE task_id = 't_foreign'").fetchone()


def _diff(live):
    shadow = ksc.open_shadow(BOARD, ME)
    try:
        return ksc.diff(live, shadow, ME)
    finally:
        shadow.close()


def test_a_picked_up_foreign_card_without_its_creators_chat_links_is_not_a_mismatch(live, svc, kanban_home):
    _foreign_copy(live, svc, kanban_home)
    report = _diff(live)
    assert report["checked"] >= 1
    assert report["child_mismatch"] == {}


def test_a_missing_chat_link_of_this_gateway_on_a_foreign_card_is_still_a_mismatch(live, svc, kanban_home):
    _foreign_copy(live, svc, kanban_home)
    shadow = ksc.open_shadow(BOARD, ME)
    try:
        shadow.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_session, kind, released, "
                       "created_at) VALUES ('t_foreign:mine', 't_foreign', ?, 's2', 'shepherd', 0, 2)", (ME,))
        shadow.commit()
        assert ksc.diff(live, shadow, ME)["child_mismatch"] == {"task_chat_links": 1}
    finally:
        shadow.close()


def test_a_missing_chat_link_on_an_own_card_is_still_a_mismatch(live, svc, kanban_home):
    tid = kb.create_task(live, title="own", assignee="worker")
    _round(kanban_home, svc)
    _other(svc, "x", "task_chat_links", f"{tid}:{OTHER}:-:s3:shepherd",
           {"task_id": tid, "chat_gateway": OTHER, "chat_session": "s3", "kind": "shepherd", "released": 0,
            "created_at": 2})
    ksc._pull_replica(BOARD, svc.hub(ME), ME)
    assert _diff(live)["child_mismatch"] == {"task_chat_links": 1}
