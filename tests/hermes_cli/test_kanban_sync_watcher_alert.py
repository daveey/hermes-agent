"""A dead kanban sync watcher opens one hub card after 5 min (not through the local outbox). [daveey]

Incident (2026-11-20): ContainmentRefused killed kanban_sync_watcher at 04:42:46 UTC; the supervisor
gave up at 04:43:21. replica-stale filed locally at 05:02 via the outbox, which could not push.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_sync_claims as claims
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
    claims.reset()
    yield h
    claims.reset()


CRASH = "kanban sync loop crashed: ContainmentRefused: canonical receipt identity changed"
CRASH_2 = "kanban sync loop crashed: RuntimeError: feed socket closed"
SINCE = 1_000.0
KEY = "kanban-sync-watcher-dead:worker-1:1000"
INFO = {"installed": "/opt/hermes", "installed_sha": "0362a20c0e", "newest_sha": None, "origin_main_sha": "a99783a19e"}


def _crash(*, since=SINCE, why=CRASH):
    runstate.path().parent.mkdir(parents=True, exist_ok=True)
    runstate.path().write_text(json.dumps({"running": False, "since": since, "reason": why, "pid": 1}))


def _round_completed():
    runstate.clear()


def _check(hub, now, host="worker-1"):
    return alert.check(now=now, runner=hub, hub_cli="hermes-hub", host=host, info=INFO)


class FailingHubCli(FakeHubCli):
    def __init__(self, verb: str) -> None:
        super().__init__()
        self.verb = verb

    def __call__(self, argv):
        if argv[1] == self.verb:
            self.calls.append(argv)
            return 1, "", "hub unreachable"
        return super().__call__(argv)


def test_crash_under_five_minutes_does_not_file(home):
    _crash()
    hub = FakeHubCli()
    assert _check(hub, SINCE + 299) is None
    assert hub.calls == []


def test_crash_at_five_minutes_opens_one_hub_card_through_the_courier_only(home, monkeypatch):
    from hermes_cli import kanban_db as kb
    local = []
    monkeypatch.setattr(kb, "create_task", lambda *a, **k: local.append((a, k)) or "t_local")
    _crash()
    hub = FakeHubCli()
    rid = _check(hub, SINCE + 300)
    assert rid == KEY
    tasks, create = hub.calls
    assert tasks == ["hermes-hub", "tasks", "--gw", "hub", "--json", "--all"]
    body = create[5]
    assert create == ["hermes-hub", "create-task", "hub", "Kanban sync watcher dead on worker-1 for 5+ min",
                      "--body", body, "--assignee", "default", "--origin-session", "", "--id", KEY,
                      "--option", f'idempotency_key="{KEY}"', "--option", "priority=900"]
    assert "ContainmentRefused" in body and "0362a20c0e" in body and "a99783a19e" in body
    assert "has not completed a round for 5 min" in body and "stopped restarting" not in body
    # The local default-board outbox path (kanban_db.create_task) is not used.
    assert local == []


def test_same_crash_incident_does_not_file_again(home):
    _crash()
    hub = FakeHubCli()
    _check(hub, SINCE + 300)
    assert _check(hub, SINCE + 400) is None
    assert len(hub.sent("create-task")) == 1 and not hub.sent("request")


def test_no_card_while_a_sync_loop_is_running(home):
    _crash()
    hub = FakeHubCli()
    claims.loop_started()  # a respawned loop whose first round has not cleared the old crash record
    assert _check(hub, SINCE + 900) is None
    assert hub.calls == [] and "first_seen_dead" not in alert._read_state()
    claims.loop_stopped()
    assert _check(hub, SINCE + 900) == KEY


def test_loop_running_here_follows_the_loop_lifecycle(home):
    assert [claims.loop_running_here(), claims.loop_started(), claims.loop_running_here(), claims.loop_stopped(),
            claims.loop_running_here()] == [False, None, True, None, False]


def test_no_card_while_another_process_holds_the_sync_lock(home, monkeypatch):
    from gateway import kanban_singleton
    _crash()
    hub = FakeHubCli()
    monkeypatch.setattr(kanban_singleton, "holder_info", lambda _path: {"pid": 4242})
    assert _check(hub, SINCE + 900) is None
    assert hub.calls == []


def test_crash_respawn_crash_with_new_text_is_one_incident(home):
    hub = FakeHubCli()
    _crash()
    assert _check(hub, SINCE + 10) is None  # first seen dead: the crash record's since
    claims.loop_started()  # supervisor respawn
    assert _check(hub, SINCE + 100) is None
    claims.loop_stopped()
    _crash(since=SINCE + 150, why=CRASH_2)  # a different crash text resets runstate's since
    assert _check(hub, SINCE + 300) == KEY  # aged from the first crash, not from SINCE + 150
    claims.loop_started()
    claims.loop_stopped()
    _crash(since=SINCE + 400, why=CRASH)
    assert _check(hub, SINCE + 1_000) is None
    assert len(hub.sent("create-task")) == 1 and not hub.sent("request")


def test_a_cleared_record_ends_the_incident_and_a_new_crash_is_a_new_one(home):
    hub = FakeHubCli()
    _crash()
    assert _check(hub, SINCE + 300) == KEY
    _round_completed()
    assert _check(hub, SINCE + 400) is None
    assert "first_seen_dead" not in alert._read_state()
    hub.open = [{"id": "t_first", "status": "done", "idempotency_key": KEY}]  # the first card was resolved and closed
    _crash(since=5_000.0, why=CRASH_2)
    assert _check(hub, 5_000.0 + 299) is None
    assert _check(hub, 5_000.0 + 300) == "kanban-sync-watcher-dead:worker-1:5000"
    assert len(hub.sent("create-task")) == 2


def test_new_incident_while_the_card_is_open_comments_instead_of_a_second_card(home):
    _crash()
    hub = FakeHubCli()
    _check(hub, SINCE + 300)
    hub.open = [{"id": "t_open", "status": "ready", "idempotency_key": KEY}]
    _round_completed()
    _check(hub, 1_900.0)
    _crash(since=2_000.0)
    rid = _check(hub, 2_000.0 + 300)
    assert rid == "kanban-sync-watcher-dead:worker-1:2000:comment"
    assert len(hub.sent("create-task")) == 1
    (comment,) = hub.sent("request")
    assert comment[2:6] == ["hub", "tasks", "default/default/t_open", "comment"]
    assert "ContainmentRefused" in comment[comment.index("--arg") + 1]


@pytest.mark.parametrize("verb", ["tasks", "create-task"])
def test_a_failing_courier_is_retried_once_per_ten_minutes_not_every_tick(home, verb):
    _crash()
    hub = FailingHubCli(verb)
    assert _check(hub, SINCE + 300) is None
    assert len(hub.sent(verb)) == 1
    state = alert._read_state()
    assert state["last_attempt"] == SINCE + 300 and "exit 1" in state["last_error"]
    for tick in range(301, 900, 7):  # dispatcher ticks inside the backoff
        assert _check(hub, SINCE + tick) is None
    assert len(hub.sent(verb)) == 1
    assert _check(hub, SINCE + 900) is None  # backoff over: one more try
    assert len(hub.sent(verb)) == 2
    hub.verb = "none"  # the hub is back
    assert _check(hub, SINCE + 1_499) is None
    assert _check(hub, SINCE + 1_500) == KEY
    assert "last_error" not in alert._read_state()


def test_update_check_runs_only_after_a_crash_record_is_due(home, monkeypatch):
    from hermes_cli import kanban_sync_schema_alert as schema_alert
    calls = []
    monkeypatch.setattr(schema_alert, "_updating", lambda: calls.append(True) or True)
    hub = FakeHubCli()
    assert _check(hub, SINCE + 900) is None  # no record: no update probe
    _crash()
    assert _check(hub, SINCE + 100) is None  # not yet due
    assert calls == []
    assert _check(hub, SINCE + 900) is None  # due, but an update is in flight
    assert calls == [True] and hub.calls == []


def test_the_hub_gateway_never_files_through_the_courier(home, caplog):
    _crash()
    hub = FakeHubCli()
    with caplog.at_level(logging.ERROR, logger=alert.log.name):
        assert _check(hub, SINCE + 300, host="hub") is None
        assert _check(hub, SINCE + 900, host="hub") is None
        assert alert.report("hub", CRASH, SINCE, runner=hub, hub_cli="hermes-hub", info=INFO, now=SINCE + 950) is None
    assert hub.calls == []
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "ContainmentRefused" in errors[0].getMessage()
    state = alert._read_state()
    assert state["key"] == "kanban-sync-watcher-dead:hub:1000"
    assert state["hub_incident"]["why"] == CRASH


def test_the_host_label_is_the_gateway_identity(home, monkeypatch):
    from hermes_cli import kanban_sync_client as ksc
    monkeypatch.setattr(ksc, "load_identity", lambda *a, **k: SimpleNamespace(gateway="worker-7"))
    _crash()
    hub = FakeHubCli()
    assert alert.check(now=SINCE + 300, runner=hub, hub_cli="hermes-hub", info=INFO) == \
        "kanban-sync-watcher-dead:worker-7:1000"
    (create,) = hub.sent("create-task")
    assert create[3] == "Kanban sync watcher dead on worker-7 for 5+ min"


def test_graceful_stop_and_schema_ahead_are_not_this_alarm(home):
    hub = FakeHubCli()
    runstate.path().parent.mkdir(parents=True, exist_ok=True)
    runstate.path().write_text(json.dumps(
        {"running": False, "since": SINCE, "reason": runstate.STOPPED, "pid": 1}))
    assert _check(hub, SINCE + 900) is None
    runstate.path().write_text(json.dumps(
        {"running": False, "since": SINCE,
         "reason": "gave up the sync role: board default: SchemaAhead: x", "pid": 1}))
    assert _check(hub, SINCE + 900) is None
    assert hub.calls == []


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


def test_new_incident_while_the_first_create_is_unresolved_waits_instead_of_a_second_create(home):
    _crash()
    hub = FakeHubCli()  # create-task exits 0 ("queued"): no task id is known and the replica shows no row
    _check(hub, SINCE + 300)
    assert len(hub.sent("create-task")) == 1
    _round_completed()
    _check(hub, 1_900.0)
    _crash(since=2_000.0)
    assert _check(hub, 2_300.0) is None
    assert len(hub.sent("create-task")) == 1
    state = alert._read_state()
    assert state["request_id"] == KEY and state["key"] == KEY  # the first request identity is retained
    # Delivery catches up: the replica shows the first card, so the later incident comments on it.
    hub.open = [{"id": "t_first", "status": "ready", "idempotency_key": KEY}]
    rid = _check(hub, 2_300.0 + alert.RETRY_S)
    assert rid == "kanban-sync-watcher-dead:worker-1:2000:comment"
    assert len(hub.sent("create-task")) == 1
    (comment,) = hub.sent("request")
    assert "default/default/t_first" in comment


def test_an_unresolved_create_older_than_six_hours_is_not_waited_on_forever(home):
    _crash()
    hub = FakeHubCli()
    _check(hub, SINCE + 300)
    _round_completed()
    _check(hub, 1_900.0)
    later = 2_000.0 + 7 * 3600
    _crash(since=later)
    assert _check(hub, later + 300) == f"kanban-sync-watcher-dead:worker-1:{int(later)}"
    assert len(hub.sent("create-task")) == 2


def test_a_create_the_courier_rejected_is_not_waited_on(home, monkeypatch):
    from hermes_cli import kanban_placement as kp
    _crash()
    hub = FakeHubCli()
    _check(hub, SINCE + 300)
    _round_completed()
    _check(hub, 1_900.0)
    monkeypatch.setattr(kp, "remote_state", lambda rid: "rejected")
    _crash(since=2_000.0)
    assert _check(hub, 2_300.0) == "kanban-sync-watcher-dead:worker-1:2000"
    assert len(hub.sent("create-task")) == 2
