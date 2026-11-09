"""Another gateway's card from the same keyed creator is not a duplicate. [daveey]

Per-host automation (the user-steps old-ask review: one card per host per day, keyed
``user-steps-old-asks:<date>``) runs on every gateway over that host's own store. The same-family
exemption used to apply to local rows only: replica rows carried ``key_family: None``, so the hub
and the MacBook refused each other's daily card (self-heal cron-d7cb391a4c / cron-d1d80dc84a).
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_overlap as ko
from tests.tools import test_kanban_create_overlap as helpers

home = helpers.home

MAC_TITLE = "user-steps: review 1 undecided request(s) older than 72 h"
HUB_TITLE = "user-steps (hub): review 9 undecided request(s) older than 72 h"
MIRROR_OPEN = ("Mirror core commits 1626+ into hermes-mods core/ "
               "(79929d70df and everything newer; starts 11-02 09:00 PST)")
MIRROR_NEW = ("Mirror core backlog into hermes-mods core/ (72+ unpublished fork commits, "
              "through 98274ef72a9 and anything newer)")


def remote(tmp_path, title=MAC_TITLE, key: object = "user-steps-old-asks:2026-11-08", tid="t_01a1bb3f5dafa11e"):
    task: dict[str, object] = {"id": tid, "board": "default", "title": title, "status": "ready", "gateway": "macbook"}
    if key is not None:
        task["idempotency_key"] = key
    helpers._fake_replica(tmp_path / "hub-state" / "hub.db", [("macbook", task)])
    return tid


def check(title=HUB_TITLE, key: str | None = "user-steps-old-asks:2026-11-09"):
    args = {"idempotency_key": key} if key else {}
    with kbc.connect_closing() as conn:
        refusal, _ = ko.check(conn, args, courier=False, title=title, session_id="new", self_tid=None, parents=[])
    return json.loads(refusal) if refusal else None


def test_replica_rows_carry_the_published_key_family(home, tmp_path):
    remote(tmp_path)
    (card,) = list(ko._replica_cards("hub"))
    assert card["key_family"] == "user-steps-old-asks"


def test_malformed_replica_key_is_no_family(home, tmp_path):
    remote(tmp_path, key=["user-steps-old-asks:2026-11-08"])
    (card,) = list(ko._replica_cards("hub"))
    assert card["key_family"] is None
    assert check()  # still refused


def test_other_gateways_same_family_card_is_admitted(home, tmp_path):
    remote(tmp_path)
    assert check() is None


@pytest.mark.parametrize("key", [None, "other-creator:2026-11-08"], ids=["unpublished", "other-family"])
def test_unpublished_or_other_family_remote_card_still_refused(home, tmp_path, key):
    tid = remote(tmp_path, key=key)
    result = check()
    assert result and result["code"] == "create_overlap"
    assert [m["id"] for m in result["matches"]] == [tid]


def test_unkeyed_new_card_still_refused(home, tmp_path):
    tid = remote(tmp_path)
    result = check(key=None)
    assert result and [m["id"] for m in result["matches"]] == [tid]


def test_remote_singleton_same_family_still_refused(home, tmp_path):
    tid = remote(tmp_path, title=MIRROR_OPEN, key="core-mirror:a", tid="t_remote")
    result = check(title=MIRROR_NEW, key="core-mirror:b")
    assert result and [m["id"] for m in result["matches"]] == [tid]
    assert "singleton" in result["matches"][0]["why"]
