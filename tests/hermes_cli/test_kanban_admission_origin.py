"""Original-chat attribution survives the real foreign-card pickup boundary."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worker_node as wn
from hermes_cli.kanban_admission_origin import origin_key
from tests.hermes_cli import test_kanban_cross_gateway_parents as helpers
from tests.hermes_cli.test_kanban_sync_copy_links import _import

kanban_home = helpers.kanban_home
live = helpers.live
svc = helpers.svc


@pytest.mark.parametrize("profile", [None, "", "default", "named"])
def test_pickup_carries_original_chat_without_outbox(live, svc, kanban_home, profile):
    helpers._other(svc, "task", "tasks", "t_copy", {
        "title": "capacity", "status": "ready", "created_at": 1,
        "created_on": helpers.OTHER, "created_by": "creator", "assignee": "worker",
        "admission_class": "capacity", "admission_frees": "frees: worker slots",
    })
    for uid, kind, session, at, released in [
        ("old", "created", "original", 1, 1), ("new", "created", "newer", 2, 0),
        ("shepherd", "shepherd", "operator", 0, 0),
    ]:
        helpers._other(svc, uid, "task_chat_links", uid, {
            "uid": uid, "task_id": "t_copy", "chat_gateway": "source", "chat_profile": profile,
            "chat_session": session, "kind": kind, "released": released, "created_at": at,
        })
    helpers._round(kanban_home, svc)
    assert kb.get_task(live, "t_copy") is None
    for _ in range(2):
        _import(live, "t_copy")
        assert json.loads(origin_key(live, "t_copy")) == ["chat", "source", profile or "default", "original"]
        assert live.execute("SELECT count(*) FROM task_chat_links WHERE task_id='t_copy'").fetchone()[0] == 3
        assert not live.execute("SELECT 1 FROM kanban_sync_outbox").fetchone()
        assert wn.evict(live) == ["t_copy"]
        assert not live.execute("SELECT 1 FROM task_chat_links WHERE task_id='t_copy'").fetchone()
        assert not live.execute("SELECT 1 FROM kanban_sync_outbox").fetchone()
