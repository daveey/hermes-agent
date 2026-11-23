"""The ETA readout of ci / deploy / clock waits: waited_since, expected_s and estimate_source. [daveey]

Clock waits expect exactly wake_at minus the park time. CI and deploy waits expect the median of the last 20
finished waits of the same kind and target on the board's own event history (fewer than 3: null, "no history yet").
"""
import json
import sqlite3

import pytest

from hermes_cli import kanban_external_activity as kea
from hermes_cli import kanban_session_wait_rows as rows
from hermes_cli import kanban_wait_history as kwh
from hermes_cli import kanban_wait_info as kwi
from hermes_state_outcome import _wait_item

PARK = 1_793_100_000.0  # 2026-10-28 ~... a fixed park time
NOW = PARK + 600
REPO = "https://github.com/acme/widgets/actions/runs/{n}"


@pytest.fixture(autouse=True)
def _fresh():
    kwh.reset_for_tests()
    kea.reset_for_tests()
    yield
    kwh.reset_for_tests()
    kea.reset_for_tests()


def _wait(awaiting, *, wake_at=None, since=PARK, status="scheduled"):
    payload = {"awaiting": awaiting}
    if wake_at is not None:
        payload["wake_at"] = wake_at
    return kwi.describe(status, park=payload, park_kind="scheduled", since=since, now=NOW)


def _board():
    conn = sqlite3.connect(":memory:")
    conn.executescript("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, "
                       "created_at REAL);"
                       "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, block_kind TEXT, human_request TEXT, "
                       "awaiting TEXT, title TEXT);")
    return conn


def _finished(conn, task, kind, ref, took, *, at=PARK - 50_000, end="condition_met", start_kind="scheduled"):
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                 (task, start_kind, json.dumps({"awaiting": {"kind": kind, "ref": ref, "why": "w"}}), at))
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'external_watch', '{}', ?)",
                 (task, at + 1))  # a poll in between never ends the wait
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, '{}', ?)",
                 (task, end, at + took))


def _park(conn, task, kind, ref, *, since=PARK, wake_at=None):
    awaiting = {"kind": kind, "ref": ref, "why": "w"}
    payload = {"awaiting": awaiting, **({"wake_at": wake_at} if wake_at else {})}
    conn.execute("INSERT INTO tasks (id, status, awaiting, title) VALUES (?, 'scheduled', ?, ?)",
                 (task, json.dumps(awaiting), f"card {task}"))
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'scheduled', ?, ?)",
                 (task, json.dumps(payload), since))


def test_clock_wait_expects_exactly_wake_at_minus_the_park_time():
    wait = _wait({"kind": "clock", "why": "soak"}, wake_at=PARK + 4 * 3600)
    assert wait["wait_kind"] == "clock"
    assert wait["waited_since"] == PARK
    assert wait["expected_s"] == 4 * 3600
    assert wait["estimate_source"].startswith("wakes ") and ":" in wait["estimate_source"]


def test_clock_wake_label_names_the_weekday_when_it_is_not_the_park_day():
    same = kwi.eta_fields("clock", PARK, wake_at=PARK + 60)
    later = kwi.eta_fields("clock", PARK, wake_at=PARK + 3 * 86400)
    assert len(same["estimate_source"].split()[1]) == 5  # "wakes HH:MM"
    assert len(later["estimate_source"].split()) == 3  # "wakes Thu 09:00"


def test_clock_wait_with_a_ref_and_without_a_wake_time():
    assert _wait({"kind": "clock", "ref": "release slot"}, wake_at=PARK + 90)["expected_s"] == 90
    bare = _wait({"kind": "clock", "why": "x"})
    assert (bare["wait_kind"], bare["expected_s"], bare["estimate_source"]) == ("clock", None, "no wake time")


def test_ci_wait_is_the_median_of_the_same_repos_finished_waits():
    conn = _board()
    for n, took in enumerate((600, 1500, 900, 1200, 3000), 1):
        _finished(conn, f"t_{n}", "ci", REPO.format(n=n), took, at=PARK - 90_000 + n * 100)
    _finished(conn, "t_other", "ci", "https://github.com/acme/other/actions/runs/9", 60)  # another repo
    _finished(conn, "t_deploy", "deploy", "hub/core", 60)  # another kind
    _park(conn, "t_now", "ci", REPO.format(n=99))
    wait = rows._local_wait_info(conn, ["t_now"], now=NOW)["t_now"]
    assert wait["wait_kind"] == "ci"
    assert wait["waited_since"] == PARK
    assert wait["expected_s"] == 1200  # median of 600, 900, 1200, 1500, 3000
    assert wait["estimate_source"] == "median of last 5 CI runs (acme/widgets)"


def test_only_the_last_twenty_finished_waits_count():
    conn = _board()
    for n in range(25):  # the five oldest are 10 h waits, the newest twenty 10 min
        _finished(conn, f"t_{n}", "ci", REPO.format(n=n), 36_000 if n < 5 else 600, at=PARK - 200_000 + n * 1000)
    _park(conn, "t_now", "ci", REPO.format(n=99))
    wait = rows._local_wait_info(conn, ["t_now"], now=NOW)["t_now"]
    assert wait["expected_s"] == 600
    assert wait["estimate_source"].startswith("median of last 20 CI runs")


def test_deploy_wait_matches_on_host_and_service():
    conn = _board()
    for n, took in enumerate((300, 360, 420), 1):
        _finished(conn, f"d_{n}", "deploy", f"hub/core-update#{n}", took, at=PARK - 9000 + n * 100,
                  end="continuation_woken", start_kind="continued")
    _finished(conn, "d_other", "deploy", "mini/core-update#1", 5000)
    _park(conn, "d_now", "deploy", "hub/core-update#77")
    wait = rows._local_wait_info(conn, ["d_now"], now=NOW)["d_now"]
    assert wait["wait_kind"] == "deploy"
    assert wait["expected_s"] == 360
    assert wait["estimate_source"] == "median of last 3 deploys (hub/core-update)"


def test_fewer_than_three_samples_is_no_estimate():
    conn = _board()
    for n, took in enumerate((600, 900), 1):
        _finished(conn, f"t_{n}", "ci", REPO.format(n=n), took)
    _park(conn, "t_now", "ci", REPO.format(n=99))
    wait = rows._local_wait_info(conn, ["t_now"], now=NOW)["t_now"]
    assert wait["expected_s"] is None
    assert wait["estimate_source"] == "no history yet"
    assert wait["waited_since"] == PARK


def test_an_unfinished_wait_is_no_sample():
    conn = _board()
    for n, took in enumerate((600, 900, 1200), 1):
        _finished(conn, f"t_{n}", "ci", REPO.format(n=n), took)
    _park(conn, "t_open", "ci", REPO.format(n=50), since=PARK - 100)  # parked, never woken
    _park(conn, "t_now", "ci", REPO.format(n=99))
    wait = rows._local_wait_info(conn, ["t_now"], now=NOW)["t_now"]
    assert wait["expected_s"] == 900  # three finished waits; the open one adds nothing


def test_a_replicated_card_carries_the_fields_from_its_published_park():
    card = {"status": "scheduled", "awaiting": {"kind": "clock", "why": "soak"}, "parked_at": PARK,
            "park": {"wake_at": PARK + 7200, "reason": ""}, "wake_check": None}
    wait = kwi.describe_replica(card, now=NOW)
    assert (wait["wait_kind"], wait["waited_since"], wait["expected_s"]) == ("clock", PARK, 7200)
    ci = kwi.describe_replica({**card, "awaiting": {"kind": "ci", "ref": REPO.format(n=1), "why": "w"}}, now=NOW)
    # a replica has no event history here: an honest "no estimate", never an invented one
    assert (ci["wait_kind"], ci["expected_s"], ci["estimate_source"]) == ("ci", None, "no history yet")


def test_other_parks_carry_no_eta_fields():
    for awaiting in ({"kind": "card", "ref": "t_abc123"}, {"kind": "external", "ref": "vendor"}):
        wait = _wait(awaiting)
        assert "wait_kind" not in wait and "expected_s" not in wait


def test_session_external_adds_clock_cards_and_eta_to_every_card():
    conn = _board()
    for n, took in enumerate((600, 900, 1200), 1):
        _finished(conn, f"t_{n}", "ci", REPO.format(n=n), took)
    _park(conn, "t_clock", "clock", "", wake_at=PARK + 4 * 3600)
    _park(conn, "t_ci", "ci", REPO.format(n=99))
    out = kea.session_external(conn, [("s1", "t_clock"), ("s1", "t_ci")], now=NOW)
    cards = {c["task_id"]: c for c in out["s1"]["cards"]}
    clock = cards["t_clock"]
    assert clock["external"]["adapter"] == "clock" and clock["external"]["job"] == "running"
    assert clock["external"]["next_due_at"] is None  # nothing polls a timer: never unknown or down
    assert clock["eta"]["expected_s"] == 4 * 3600 and clock["eta"]["waited_since"] == PARK
    assert out["s1"]["counts"][0] >= 1 and out["s1"]["counts"][2] == 0


def test_outcome_wait_items_keep_the_eta_readout():
    item = _wait_item({"kind": "time", "id": "t_x", "waited_since": PARK, "expected_s": 90.0,
                       "estimate_source": "wakes 14:30"})
    assert (item["waited_since"], item["expected_s"], item["estimate_source"]) == (PARK, 90.0, "wakes 14:30")
    bare = _wait_item({"kind": "time", "id": "t_x", "expected_s": "soon"})
    assert "expected_s" not in bare


def test_clock_wait_paints_the_canonical_light_blue_stripe(monkeypatch):
    import hermes_state_outcome as so
    from hermes_cli import kanban_session_activity as ksa
    from hermes_state_session_status import _verdict

    wait = _wait({"kind": "clock", "why": "soak"}, wake_at=PARK + 4 * 3600)
    card = {"id": "t_clock", "title": "soak", "status": "scheduled", "wait": wait}
    monkeypatch.setattr(ksa, "session_outcome_tasks", lambda ids, statuses, stale_ok=False: [card])
    decisions, waits = so._read_card_items(["s1"])
    assert decisions == []
    (item,) = waits
    assert item["state"] == "external_running"
    assert (item["wait_kind"], item["expected_s"], item["waited_since"]) == ("clock", 4 * 3600, PARK)
    verdict = _verdict({"work": waits, "turn": {"generation": 1, "state": "completed"}}, {"state": "complete"}, 1, [])
    assert verdict["presentation"]["stripe"] == "external"
    plain = _verdict({"work": [{"id": "t", "kind": "card", "state": "scheduled"}],
                      "turn": {"generation": 1, "state": "completed"}}, {"state": "complete"}, 1, [])
    assert plain["presentation"]["stripe"] == "waiting"
