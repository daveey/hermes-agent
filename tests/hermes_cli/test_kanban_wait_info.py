"""What a parked card waits on, who produces it, and when an agent-owned wait counts as stalled. [daveey]

Replays the 10-26/27 orphan waits (evidence: kanban/evidence/orphan-waits-20261027/5whys.md): a card parked on an
opaque IAM probe, a timed park whose reason needs a supervisor, a hidden human ask, a continuation_exhausted
owner and a gave_up->triage card all read as agent-owned and turn amber after 2 h; real producers (a CI run, a card,
the clock) stay grey.
"""
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_wait_check as kwc
from hermes_cli import kanban_wait_info as kwi

NOW = 1_793_140_000.0
H = 3600


@pytest.fixture(autouse=True)
def _not_a_probe_run(monkeypatch):
    # wait-check reads the waiter from HERMES_KANBAN_TASK (set for dispatcher probes and kanban workers).
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WAKE_PROBE", raising=False)


def d(status, **kw) -> dict:
    info = kwi.describe(status, now=NOW, **kw)
    assert info is not None
    return info


# ---- classification -------------------------------------------------------------------------------------------
def test_not_parked_statuses_have_no_wait():
    for status in ("running", "ready", "todo", "review", "done", "archived", ""):
        assert kwi.describe(status, now=NOW) is None


def test_iam_probe_park_is_agent_owned_and_stalls_after_two_hours():
    park = {"reason": "waiting for EC2 describe on the hub role", "wake_check": "python3 ~/.hermes/scripts/aws_hub_ec2_probe.py"}
    fresh = d("scheduled", park=park, park_kind="scheduled", since=NOW - 600)
    assert fresh["agent_owned"] is True and fresh["wait_state"] == "waiting"
    assert fresh["waiting_on"] == "probe aws_hub_ec2_probe.py"
    old = d("scheduled", park=park, park_kind="scheduled", since=NOW - 2 * H - 1)
    assert old["wait_state"] == "stalled" and old["waiting_since"] == NOW - 2 * H - 1


def test_awaiting_with_live_producer_is_never_stalled():
    for kind, ref in (("ci", "run 40577358595"), ("card", "t_abc on hub"), ("deploy", "release 93ed6b10"),
                      ("external", "AWS support case 123"), ("clock", "24h soak")):
        info = d("scheduled", awaiting={"kind": kind, "ref": ref, "why": "x"}, since=NOW - 30 * H)
        assert info["agent_owned"] is False and info["wait_state"] == "waiting", kind
        assert info["waiting_on"] == f"{kind} {ref}"


def test_awaiting_read_from_park_payload_json():
    payload = json.dumps({"reason": "r", "awaiting": {"kind": "ci", "ref": "run 1", "why": "w"}})
    assert d("scheduled", park=payload, since=NOW - 9 * H)["waiting_on"] == "ci run 1"


def test_hidden_human_ask_and_producerless_awaiting_are_agent_owned():
    hidden = d("scheduled", awaiting={"kind": "human", "ref": "", "why": "David's decision on attachment180"},
               since=NOW - 3 * H)
    assert hidden["agent_owned"] is True and hidden["wait_state"] == "stalled"
    assert "hidden ask" in hidden["waiting_on"]
    nobody = d("scheduled", awaiting={"kind": "ci", "ref": "", "why": "grant"}, since=NOW - 3 * H)
    assert nobody["agent_owned"] is True and nobody["wait_state"] == "stalled"


def test_legacy_wait_check_on_card_or_ci_names_a_live_producer():
    card = d("scheduled", park={"wake_check": "hermes kanban wait-check task t_b11f0591 --gateway hub"},
             since=NOW - 9 * H)
    assert card["waiting_on"] == "card t_b11f0591 on hub" and card["agent_owned"] is False
    ci = d("scheduled", park={"wake_check": "gh run view 123 --json status | jq -e '.status==\"completed\"'"},
           since=NOW - 9 * H)
    assert ci["waiting_on"] == "ci CI run" and ci["wait_state"] == "waiting"


def test_clock_only_park_is_a_clock_wait():
    info = d("scheduled", park={"timed_park": True, "wake_at": NOW + 3600}, since=NOW - 9 * H)
    assert info["agent_owned"] is False and info["waiting_on"].startswith("the clock until ")


def test_transient_and_capability_blocks_are_agent_owned():
    for kind in ("transient", "capability", "assignment"):
        info = d("blocked", block_kind=kind, park={"reason": "hub role denied ec2:Describe*"}, since=NOW - 3 * H)
        assert info["agent_owned"] is True and info["wait_state"] == "stalled", kind
        assert info["premise"].startswith(f"blocked:{kind}:")


def test_human_block_is_the_users_move_not_stalled():
    info = d("blocked", block_kind="needs_input", human=True, since=NOW - 30 * H)
    assert info["wait_state"] == "human" and info["agent_owned"] is False and info["premise"] is None


def test_triage_gave_up_and_continuation_exhausted():
    gave_up = d("triage", block_kind="transient", park={"reason_code": "worker_failures_exhausted"},
                park_kind="gave_up", since=NOW - 3 * H)
    assert gave_up["agent_owned"] and gave_up["wait_state"] == "stalled" and "gave up" in gave_up["waiting_on"]
    exhausted = d("blocked", block_kind="transient",
                  park={"reason": "continuation_exhausted: split the work or reset the continuation budget"},
                  park_kind="blocked", since=NOW - 60)
    assert exhausted["premise"].startswith("continuation_exhausted:") and exhausted["wait_state"] == "waiting"


def test_dependency_block_waits_on_a_parent():
    info = d("blocked", block_kind="dependency", since=NOW - 30 * H)
    assert info["agent_owned"] is False and info["waiting_on"] == "a parent card"


def test_premise_is_stable_across_reparks_and_changes_with_the_reason():
    a = d("blocked", block_kind="transient", park={"reason": "denied at 2026-10-27T03:35Z, attempt 3"}, since=NOW - 9)
    b = d("blocked", block_kind="transient", park={"reason": "denied at 2026-10-27T05:12Z, attempt 4"}, since=NOW - 1)
    c = d("blocked", block_kind="transient", park={"reason": "ssh to the mini refused"}, since=NOW - 1)
    assert a["premise"] == b["premise"] != c["premise"]


def test_millisecond_stamps_are_scaled():
    info = d("blocked", block_kind="transient", since=(NOW - 3 * H) * 1000)
    assert info["waiting_since"] == pytest.approx(NOW - 3 * H) and info["wait_state"] == "stalled"


def test_replica_card_uses_run_outcome_and_human_request():
    # A remote schedule whose owner published no producer is unknown, never stalled: from here a clock park and an
    # orphan wait look the same. SCHEDULED_UNTIL in the run summary reads as the clock.
    card = {"status": "scheduled", "block_kind": None, "human_request": None,
            "run": {"outcome": "scheduled", "ended_at": NOW - 5 * H,
                    "summary": "waiting for a scope only an AWS SSO owner session can create"}}
    info = kwi.describe_replica(card, now=NOW) or {}
    assert info["agent_owned"] is False and info["wait_state"] == "waiting"
    timed = {**card, "run": {**card["run"], "summary": "SCHEDULED_UNTIL=2026-10-30T00:00:00Z - waiting"}}
    assert (kwi.describe_replica(timed, now=NOW) or {})["waiting_on"] == "the clock until 2026-10-30 00:00 UTC"
    published = {**card, "awaiting": {"kind": "human", "ref": "", "why": "decision"}}
    assert (kwi.describe_replica(published, now=NOW) or {})["agent_owned"] is True
    blocked = {"status": "blocked", "block_kind": "transient", "run": {"outcome": "blocked", "ended_at": NOW - 3 * H,
                                                                       "summary": "ssh to the mini refused"}}
    assert (kwi.describe_replica(blocked, now=NOW) or {})["wait_state"] == "stalled"
    reclaimed = {"status": "triage", "block_kind": "transient", "run": {"outcome": "reclaimed", "ended_at": NOW - 60}}
    assert (kwi.describe_replica(reclaimed, now=NOW) or {})["agent_owned"] is True
    loop = {"status": "triage", "block_kind": "capability", "run": {"outcome": "blocked", "ended_at": NOW - 9 * H}}
    assert (kwi.describe_replica(loop, now=NOW) or {})["agent_owned"] is False
    human = {"status": "blocked", "block_kind": "needs_input", "human_request": json.dumps({
        "action": "Pick a window.", "why_human": "Owner decision.", "interaction": "reply", "scope": "deploy",
        "evidence": "No window chosen."}), "run": {"outcome": "blocked", "ended_at": NOW - 9 * H}}
    assert (kwi.describe_replica(human, now=NOW) or {})["wait_state"] == "human"


# ---- session activity: sidebar count + strip rows --------------------------------------------------------------
def _board(path: Path, cards: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT NOT NULL, session_id TEXT, "
              "block_kind TEXT, human_request TEXT)")
    c.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, "
              "payload TEXT, created_at INTEGER)")
    for card in cards:
        c.execute("INSERT INTO tasks (id, title, status, session_id, block_kind) VALUES (?,?,?,?,?)",
                  (card["id"], card["id"], card["status"], card["session"], card.get("kind")))
        for kind, payload, at in card.get("events", []):
            c.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                      (card["id"], kind, json.dumps(payload), at))
    c.commit()
    c.close()


@pytest.fixture
def kanban_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    ksa._reset_cache_for_tests()
    yield tmp_path
    ksa._reset_cache_for_tests()


def test_sidebar_stamps_stalled_only_for_old_agent_owned_waits(kanban_root, monkeypatch):
    import time as _time
    now = _time.time()
    _board(kanban_root / "kanban.db", [
        {"id": "t_old_probe", "status": "scheduled", "session": "s_stalled",
         "events": [("scheduled", {"reason": "grant", "wake_check": "python3 probe.py"}, int(now - 3 * H))]},
        {"id": "t_ci", "status": "scheduled", "session": "s_ci",
         "events": [("scheduled", {"reason": "ci", "wake_check": "gh run watch 1"}, int(now - 30 * H))]},
        {"id": "t_new_block", "status": "blocked", "kind": "transient", "session": "s_fresh",
         "events": [("blocked", {"kind": "transient", "reason": "flaky"}, int(now - 60))]},
        {"id": "t_running", "status": "running", "session": "s_stalled"},
    ])
    rows = [{"id": "s_stalled"}, {"id": "s_ci"}, {"id": "s_fresh"}]
    ksa.annotate_kanban_activity(rows)
    got = {r["id"]: r for r in rows}
    assert got["s_stalled"]["kanban_stalled"] == 1
    assert "kanban_stalled" not in got["s_ci"] and "kanban_stalled" not in got["s_fresh"]
    assert ksa.stalled_task_counts() == {"s_stalled": 1}


def test_session_tasks_rows_carry_what_each_card_waits_on(kanban_root):
    import time as _time
    now = _time.time()
    _board(kanban_root / "kanban.db", [
        {"id": "t_wait", "status": "scheduled", "session": "s1",
         "events": [("blocked", {"kind": "transient"}, int(now - 9 * H)),
                    ("scheduled", {"reason": "waiting on hub card",
                                   "wake_check": "hermes kanban wait-check task t_x --gateway hub"}, int(now - H))]},
        {"id": "t_run", "status": "running", "session": "s1"},
    ])
    rows = {t["id"]: t for t in ksa.session_tasks(["s1"])}
    assert rows["t_wait"]["wait"]["waiting_on"] == "card t_x on hub"
    assert rows["t_wait"]["wait"]["waiting_since"] == pytest.approx(int(now - H))
    assert rows["t_wait"]["wait"]["wait_state"] == "waiting"
    assert "wait" not in rows["t_run"]


# ---- tracker wake default --------------------------------------------------------------------------------------
@pytest.mark.parametrize("status,met", [("scheduled", 1), ("triage", 0), ("review", 0), ("blocked", 0),
                                        ("done", 0), ("running", 1), ("ready", 1)])
def test_default_tracker_wake_includes_owner_parking_blocking_and_triage(tmp_path, monkeypatch, status, met):
    db = tmp_path / "kanban.db"
    _board(db, [{"id": "t_owner", "status": status, "session": "s"}])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    rc, _ = kwc.check_local("task", "t_owner")
    assert rc == met


def test_default_tracker_wake_on_scheduled_only_when_the_park_is_agent_owned(tmp_path, monkeypatch):
    import time as _time
    now = _time.time()
    db = tmp_path / "kanban.db"
    _board(db, [
        {"id": "t_checkpoint", "status": "scheduled", "session": "s",  # a continuation checkpoint between runs
         "events": [("blocked", {"kind": "transient"}, int(now - 9 * H)),
                    ("continued", {"status": "scheduled", "wake_at": int(now + 600)}, int(now - 60))]},
        {"id": "t_orphan", "status": "scheduled", "session": "s",
         "events": [("scheduled", {"reason": "grant", "wake_check": "python3 aws_hub_ec2_probe.py"}, int(now - 3 * H))]},
        {"id": "t_ci", "status": "scheduled", "session": "s",
         "events": [("scheduled", {"wake_check": "gh run watch 9"}, int(now - 9 * H))]},
    ])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    assert kwc.check_local("task", "t_checkpoint")[0] == 1
    assert kwc.check_local("task", "t_ci")[0] == 1
    rc, msg = kwc.check_local("task", "t_orphan")
    assert rc == 0 and "agent-owned" in msg
    assert kwc.check_local("task", "t_orphan", since=("scheduled",))[0] == 1


def test_remote_from_is_applied_locally(monkeypatch):
    import subprocess as sp
    seen = {}

    def fake_run(argv, **kw):
        seen["remote"] = argv[-1]
        return sp.CompletedProcess(argv, 0, stdout="t_x scheduled (agent-owned park)\n", stderr="")
    monkeypatch.setattr(kwc.subprocess, "run", fake_run)
    assert kwc.check_remote("hub", "task", "t_x", since=("scheduled",))[0] == 1
    assert "--from" not in seen["remote"] and "parked-agent" in seen["remote"]
    assert kwc.check_remote("hub", "task", "t_x")[0] == 0


def test_from_baseline_waits_for_the_next_change(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    _board(db, [{"id": "t_owner", "status": "scheduled", "session": "s"}])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    assert kwc.check_local("task", "t_owner", until=("scheduled", "triage"), since=("scheduled",))[0] == 1
    assert kwc.run(SimpleNamespace(check="task", target="t_owner", board=None, until="scheduled,triage",
                                   since="scheduled", gateway=None)) == 1
    with sqlite3.connect(db) as c:
        c.execute("UPDATE tasks SET status='triage'")
    assert kwc.check_local("task", "t_owner", since=("scheduled",))[0] == 0


def test_unknown_card_is_cannot_tell(tmp_path, monkeypatch):
    db = tmp_path / "kanban.db"
    _board(db, [])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    ns = SimpleNamespace(check="task", target="t_missing", board=None, until=None, since=None, gateway=None)
    assert kwc.run(ns) == 2  # unknown card is "cannot tell", never "not yet"


def test_parked_waits_lists_each_owning_session_with_wait_info(kanban_root):
    import time as _time
    now = _time.time()
    _board(kanban_root / "kanban.db", [
        {"id": "t_cap", "status": "blocked", "kind": "capability", "session": "s_owner",
         "events": [("blocked", {"kind": "capability", "reason": "hub role denied ec2:Describe*"}, int(now - 60))]},
        {"id": "t_run", "status": "running", "session": "s_owner"},
        {"id": "t_ci", "status": "scheduled", "session": "s_other",
         "events": [("scheduled", {"wake_check": "gh run watch 7"}, int(now - 60))]},
    ])
    rows = {r["id"]: r for r in ksa.parked_waits()}
    assert set(rows) == {"t_cap", "t_ci"}
    assert rows["t_cap"]["session"] == "s_owner" and rows["t_cap"]["key"] == "default/t_cap"
    assert rows["t_cap"]["wait"]["agent_owned"] is True and rows["t_cap"]["title"] == "t_cap"
    assert rows["t_ci"]["wait"]["agent_owned"] is False


# ---- review fixes: false positives that must not wake anyone ------------------------------------------------------
def test_needs_input_without_a_validated_ask_is_agent_owned():
    # Matches kanban_block_policy.AGENT_OWNED_KINDS: an ask-less needs_input is invisible to David, so an agent owns it.
    from hermes_cli.kanban_block_policy import AGENT_OWNED_KINDS
    assert "needs_input" in AGENT_OWNED_KINDS
    fresh = d("blocked", block_kind="needs_input", human=False, park={"reason": "Two decisions for David"},
              since=NOW - 60)
    assert fresh["agent_owned"] is True and fresh["wait_state"] == "waiting"
    stale = d("blocked", block_kind="needs_input", human=False, park={"reason": "x"}, since=NOW - 30 * H)
    assert stale["agent_owned"] is True and stale["wait_state"] == "stalled"


def test_untyped_legacy_block_counts_only_once_stalled():
    fresh = d("blocked", since=NOW - 60)
    assert fresh["agent_owned"] is True and fresh["agent_owned_certain"] is False and fresh["wait_state"] == "waiting"
    assert d("blocked", since=NOW - 3 * H)["wait_state"] == "stalled"


def test_continuation_checkpoint_is_a_clock_park():
    info = d("scheduled", park={"status": "scheduled", "wake_at": NOW + 600, "continuation": 2},
             park_kind="continued", since=NOW - 9 * H)
    assert info["agent_owned"] is False and info["waiting_on"].startswith("the clock until ")


def test_triage_only_agent_owned_after_a_failed_run():
    assert d("triage", park_kind="gave_up", since=NOW - 60)["agent_owned"] is True
    assert d("triage", park_kind="supervisor_escalated", since=NOW - 60)["agent_owned"] is True
    assert d("triage", park_kind=None, since=NOW - 30 * H)["agent_owned"] is False  # intake
    loop = d("triage", block_kind="capability", park_kind="block_loop_detected", since=NOW - 30 * H)
    assert loop["agent_owned"] is False and "block loop" in loop["waiting_on"]


def test_stale_exhausted_event_does_not_outlive_a_later_continuation():
    info = d("scheduled", park={"wake_at": NOW + 60}, park_kind="continued", since=NOW - 60)
    assert info["agent_owned"] is False


def test_premise_ignores_hex_ids_and_prefers_reason_codes():
    a = d("triage", block_kind="transient", park={"reason_code": "worker_failures_exhausted",
                                                  "error": "stale_lock=hermes-hub:1861942"}, park_kind="gave_up")
    b = d("triage", block_kind="transient", park={"reason_code": "worker_failures_exhausted",
                                                  "error": "stale_lock=hermes-hub:2002020 run 01a17f93-3883-77bc"},
          park_kind="gave_up")
    assert a["premise"] == b["premise"]
    c = d("blocked", block_kind="transient", park={"reason": "run 01a17f93-3883-77bc-83b0-da01a927d1b2 died"})
    e = d("blocked", block_kind="transient", park={"reason": "run 01a17f99-1111-77bc-83b0-da01a927ffff died"})
    assert c["premise"] == e["premise"]


@pytest.mark.parametrize("cmd,want", [
    ("[ -f ~/.done ]", ("probe", ".done")),
    ("s=$(sqlite3 ~/.hermes/kanban.db \"select status from tasks where id='t_01a17f2a6cc05d46'\"); [ $s = done ]",
     ("card", "t_01a17f2a6cc05d46")),
    ("test -e /tmp/x", ("probe", "x")),
])
def test_probe_labels_skip_shell_noise(cmd, want):
    assert kwi.producer_from_wake_check(cmd) == want


def test_stale_remote_rows_have_no_wait(kanban_root, monkeypatch):
    rows = [{"session": "s1", "id": "t_r", "key": "hub/default/default/t_r", "board": "default", "status": "blocked",
             "block_kind": "transient", "stale": True, "park": {"outcome": "blocked", "ended_at": 1, "reason": "x"}}]
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: rows)
    assert ksa._remote_wait(rows[0]) is None
    assert [r for r in ksa.parked_waits() if r["remote"]] == []


# ---- review fixes: main's park payloads carry an inferred ``awaiting`` on every park ----------------------------
def test_inferred_clock_awaiting_is_judged_by_the_wake_mechanism():
    # kanban_awaiting._infer on a plain schedule / continuation: {"kind": "clock", "inferred": True}, no ref.
    park = {"status": "scheduled", "wake_at": NOW + 600, "continuation": 2,
            "awaiting": {"kind": "clock", "inferred": True, "premise": "abc"}}
    info = d("scheduled", park=park, park_kind="continued", since=NOW - 9 * H)
    assert info["agent_owned"] is False and info["wait_state"] == "waiting"
    assert info["waiting_on"].startswith("the clock until ")


def test_inferred_external_awaiting_falls_back_to_the_probe():
    cmd = "python3 aws_hub_ec2_probe.py"
    park = {"wake_check": cmd, "awaiting": {"kind": "external", "ref": cmd, "inferred": True}}
    info = d("scheduled", park=park, since=NOW - 3 * H)
    assert info["agent_owned"] is True and info["agent_owned_certain"] is False and info["wait_state"] == "stalled"
    ci = {"wake_check": "gh run watch 9", "awaiting": {"kind": "external", "ref": "gh run watch 9", "inferred": True}}
    assert d("scheduled", park=ci, since=NOW - 9 * H)["agent_owned"] is False


def test_inferred_card_awaiting_and_declared_clock_are_live_producers():
    card = d("scheduled", park={"awaiting": {"kind": "card", "ref": "t_abc12345", "inferred": True}},
             since=NOW - 9 * H)
    assert card["agent_owned"] is False and card["waiting_on"] == "card t_abc12345"
    soak = d("scheduled", park={"wake_at": NOW + 600, "awaiting": {"kind": "clock", "why": "24h soak"}},
             since=NOW - 9 * H)
    assert soak["agent_owned"] is False and "24h soak" in soak["waiting_on"]
    remote = d("scheduled", park={"awaiting": {"kind": "card", "ref": "t_abc12345", "gateway": "hub"}})
    assert remote["waiting_on"] == "card t_abc12345 on hub"


def test_tracker_does_not_rewake_for_a_status_it_already_handled(tmp_path, monkeypatch):
    """A tracker woken because its owner went to review re-parks with the same default probe: the probe (run with
    the dispatcher's HERMES_KANBAN_TASK/HERMES_KANBAN_DB) is not met again until the owner's status changes."""
    db = tmp_path / "kanban.db"
    probe = "hermes kanban wait-check task t_owner"
    _board(db, [{"id": "t_owner", "status": "review", "session": "s"},
                {"id": "t_tracker", "status": "scheduled", "session": "s",
                 "events": [("condition_met", {"wake_check": probe, "output": "t_owner review"}, 1),
                            ("scheduled", {"wake_check": probe}, 2)]}])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db))
    ns = SimpleNamespace(check="task", target="t_owner", board=None, until=None, since=None, gateway=None)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert kwc.run(ns) == 0  # not a probe run: plain default
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_tracker")
    assert kwc.run(ns) == 0  # a worker running wait-check by hand: plain status
    monkeypatch.setenv("HERMES_KANBAN_WAKE_PROBE", "1")
    assert kwc.run(ns) == 1  # the dispatcher's probe run: same status the tracker already handled
    with sqlite3.connect(db) as c:
        c.execute("UPDATE tasks SET status='triage' WHERE id='t_owner'")
    assert kwc.run(ns) == 0  # a new status wakes it
    assert kwc.handled_status("t_other", str(db), "t_owner") is None


def test_parked_waits_keeps_local_cards_when_the_remote_read_fails(kanban_root, monkeypatch):
    import time as _time
    _board(kanban_root / "kanban.db", [
        {"id": "t_b", "status": "blocked", "kind": "transient", "session": "s1",
         "events": [("blocked", {"kind": "transient", "reason": "x"}, int(_time.time() - 60))]}])

    def boom(stale_ok=False):
        raise sqlite3.OperationalError("replica locked")
    monkeypatch.setattr(ksa, "_remote_links", boom)
    assert [r["id"] for r in ksa.parked_waits()] == ["t_b"]


@pytest.mark.parametrize("stamp,utc", [
    ("2026-12-04T12:32:41-08:00", (2026, 12, 4, 20, 32, 41)),
    ("2026-12-04T12:32:41+05:30", (2026, 12, 4, 7, 2, 41)),
    ("2026-12-04T12:32:41Z", (2026, 12, 4, 12, 32, 41)),
    ("2026-12-04T12:32:41", (2026, 12, 4, 12, 32, 41)),  # naive: read as UTC, as before
    ("2026-12-04T12:32-08:00", (2026, 12, 4, 20, 32, 0)),
])
def test_park_fields_honours_the_scheduled_until_offset(stamp, utc):
    import calendar
    park = kwi.park_fields({"summary": f"SCHEDULED_UNTIL={stamp} waiting", "outcome": "scheduled"})
    assert park["wake_at"] == calendar.timegm((*utc, 0, 0, 0))


def test_local_continued_park_reports_its_awaiting_and_its_clock(kanban_root):
    """Hub card t_01a1fb17e589fdd8: scheduled by a ``continued`` park carrying a declared ci ``awaiting`` and a
    ``wake_at``; ``/api/session-kanban-tasks`` showed no wait for it. A continuation without awaiting shows its
    wake time. [daveey]"""
    import time as _time
    now = int(_time.time())
    ci = {"continuation": 1, "status": "scheduled", "resume_status": "ready", "wake_at": now + 3 * H,
          "awaiting": {"kind": "ci", "ref": "local core-wt consumer suite PID 4143137",
                       "why": "the consumer suite runs in the background"}}
    _board(kanban_root / "kanban.db", [
        {"id": "t_01a1fb17e589fdd8", "status": "scheduled", "session": "s1",
         "events": [("claimed", {}, now - 120), ("continued", ci, now - 60)]},
        {"id": "t_paced", "status": "scheduled", "session": "s1",
         "events": [("continued", {"continuation": 1, "status": "scheduled", "resume_status": "ready",
                                   "wake_at": now + 600}, now - 60)]},
    ])
    rows = {t["id"]: t for t in ksa.session_tasks(["s1"])}
    wait = rows["t_01a1fb17e589fdd8"]["wait"]
    assert wait["waiting_on"] == "ci local core-wt consumer suite PID 4143137", wait
    assert wait["agent_owned"] is False and wait["waiting_since"] == pytest.approx(now - 60)
    assert rows["t_paced"]["wait"]["waiting_on"].startswith("the clock until "), rows["t_paced"]["wait"]


def test_kernel_continued_park_with_awaiting_lists_its_wait(tmp_path, monkeypatch):
    """The same hub card through the real kernel: ``continue_task`` with a declared ci ``awaiting`` (as the worker's
    kanban_continue writes it), then the worker's ``worker_result``; the chat's task list shows the producer."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_continuation import continue_task
    from hermes_cli.kanban_db_connect import connect
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb.init_db()
    ksa._reset_cache_for_tests()
    conn = connect()
    try:
        tid = kb.create_task(conn, title="core change", assignee="default", session_id="s1")
        assert kb.claim_task(conn, tid) is not None
        out = continue_task(conn, tid, checkpoint="suite running in the background", resume_after_seconds=600,
                            awaiting={"kind": "ci", "ref": "local core-wt consumer suite PID 4143137",
                                      "why": "the consumer suite runs in the background"})
        assert out["ok"] and out["status"] == "scheduled", out
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "worker_result", {"exit_code": 0})
    finally:
        conn.close()
    rows = {t["id"]: t for t in ksa.session_tasks(["s1"], stale_ok=True)}  # as /api/session-kanban-tasks asks
    assert rows[tid]["wait"]["waiting_on"] == "ci local core-wt consumer suite PID 4143137", rows[tid].get("wait")
    ksa._reset_cache_for_tests()
