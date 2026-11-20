"""A dead kanban sync watcher opens one hub card after 5 min (not through the local outbox). [daveey]

Incident (2026-11-20): ContainmentRefused killed kanban_sync_watcher at 04:42:46 UTC; the supervisor
gave up at 04:43:21. replica-stale filed locally at 05:02 via the outbox, which could not push.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_sync_runstate as runstate
from hermes_cli import kanban_sync_watcher_alert as alert
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.test_kanban_schema_owner import FakeHubCli


@pytest.fixture
def home(tmp_path, monkeypatch):
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_SESSION_ID", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    h = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(h, True)
    yield h


CRASH = "kanban sync loop crashed: ContainmentRefused: canonical receipt identity changed"
SINCE = 1_000.0
INFO = {"installed": "/opt/hermes", "installed_sha": "0362a20c0e", "newest_sha": None, "origin_main_sha": "a99783a19e"}


def _crash(*, since=SINCE):
    runstate.path().parent.mkdir(parents=True, exist_ok=True)
    runstate.path().write_text(json.dumps({"running": False, "since": since, "reason": CRASH, "pid": 1}))


def test_crash_under_five_minutes_does_not_file(home):
    _crash()
    hub = FakeHubCli()
    assert alert.check(now=SINCE + 299, runner=hub, hub_cli="hermes-hub", host="worker-1", info=INFO) is None
    assert hub.sent("create-task") == []


def test_crash_at_five_minutes_opens_one_hub_card(home):
    _crash()
    hub = FakeHubCli()
    rid = alert.check(now=SINCE + 300, runner=hub, hub_cli="hermes-hub", host="worker-1", info=INFO)
    creates = hub.sent("create-task")
    assert rid == "kanban-sync-watcher-dead:worker-1:1000" and len(creates) == 1
    argv = creates[0]
    assert argv[2] == "hub" and "worker-1" in argv[3]
    body = argv[argv.index("--body") + 1]
    assert "ContainmentRefused" in body and "0362a20c0e" in body
    assert "hermes-hub" in argv[0] or argv[0] == "hermes-hub"
    assert 'idempotency_key="kanban-sync-watcher-dead:worker-1:1000"' in argv
    # Does not use the local default-board outbox path.
    assert "create-task" in argv[1]


def test_same_crash_incident_does_not_file_again(home):
    _crash()
    hub = FakeHubCli()
    alert.check(now=SINCE + 300, runner=hub, hub_cli="hermes-hub", host="worker-1", info=INFO)
    assert alert.check(now=SINCE + 400, runner=hub, hub_cli="hermes-hub", host="worker-1", info=INFO) is None
    assert len(hub.sent("create-task")) == 1 and not hub.sent("request")


def test_new_crash_while_open_comments_instead_of_a_second_card(home):
    _crash()
    hub = FakeHubCli()
    alert.check(now=SINCE + 300, runner=hub, hub_cli="hermes-hub", host="worker-1", info=INFO)
    hub.open = [{"id": "t_open", "status": "ready",
                 "idempotency_key": "kanban-sync-watcher-dead:worker-1:1000"}]
    runstate.path().write_text(json.dumps(
        {"running": False, "since": 2_000.0, "reason": CRASH, "pid": 2}))
    rid = alert.check(now=2_000.0 + 300, runner=hub, hub_cli="hermes-hub", host="worker-1", info=INFO)
    assert rid == "kanban-sync-watcher-dead:worker-1:2000:comment"
    assert len(hub.sent("create-task")) == 1
    (comment,) = hub.sent("request")
    assert comment[2:6] == ["hub", "tasks", "default/default/t_open", "comment"]
    assert "ContainmentRefused" in comment[comment.index("--arg") + 1]


def test_graceful_stop_and_schema_ahead_are_not_this_alarm(home):
    hub = FakeHubCli()
    runstate.path().parent.mkdir(parents=True, exist_ok=True)
    runstate.path().write_text(json.dumps(
        {"running": False, "since": SINCE, "reason": runstate.STOPPED, "pid": 1}))
    assert alert.check(now=SINCE + 900, runner=hub, hub_cli="hermes-hub", host="worker-1", info=INFO) is None
    runstate.path().write_text(json.dumps(
        {"running": False, "since": SINCE,
         "reason": "gave up the sync role: board default: SchemaAhead: x", "pid": 1}))
    assert alert.check(now=SINCE + 900, runner=hub, hub_cli="hermes-hub", host="worker-1", info=INFO) is None
    assert hub.sent("create-task") == []


def test_dispatcher_tick_runs_the_dead_watcher_check(home, monkeypatch):
    from hermes_cli import kanban_sync_outbox_age as outbox_age
    seen = []

    def fake_check(**_kwargs):
        seen.append(True)
        return "t_dead"

    monkeypatch.setattr(alert, "check", fake_check)
    outbox_age.reset()
    assert "t_dead" in outbox_age.check(mono=lambda: 1e6, boards=[], age=lambda _p: None)
    assert seen
    seen.clear()
    # The 60 s outbox throttle must not skip the dead-watcher courier (incident: 20 min local-only cards).
    ids = outbox_age.check(mono=lambda: 1e6 + 10, boards=[], age=lambda _p: None)
    assert seen and "t_dead" in ids
