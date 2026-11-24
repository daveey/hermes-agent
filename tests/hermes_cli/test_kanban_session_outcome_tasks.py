"""Lean listed-card reader for session outcome (W1): not the sidebar session_tasks path."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import hermes_state_outcome as so
from hermes_cli import kanban_generation as gen
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_session_activity_busy as busy
from hermes_cli.kanban_generation import BoardLifecycleBusy

HUMAN_REQUEST = {
    "action": "Approve the production deploy?",
    "why_human": "Only David can approve a deploy.",
    "interaction": "approval",
    "scope": "deploy",
    "evidence": "CI is green.",
    "choices": [{"label": "Ship it", "value": "yes"}, {"label": "Hold", "value": "no"}],
}


def _lean():
    reader = getattr(ksa, "session_outcome_tasks", None)
    if reader is None or reader is ksa.session_tasks:
        pytest.fail("missing session_outcome_tasks lean reader")
    return reader


@pytest.fixture
def kanban_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    monkeypatch.setattr(ksa, "_hub_db_path", lambda: None)
    ksa._reset_cache_for_tests()
    busy.reset()
    yield tmp_path
    ksa._reset_cache_for_tests()
    busy.reset()


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT NOT NULL, "
        "session_id TEXT, assignee TEXT, created_at REAL, started_at REAL, block_kind TEXT, "
        "human_request TEXT, current_run_id INTEGER, last_heartbeat_at REAL)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS kanban_notify_subs (task_id TEXT, platform TEXT, chat_id TEXT, "
        "delivery_metadata TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, "
        "payload TEXT, created_at REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS task_runs (id INTEGER PRIMARY KEY, task_id TEXT, started_at REAL, "
                 "summary TEXT)")
    return conn


def _insert_task(conn, tid, status, session_id, *, title=None, block_kind=None, request=None, **extra):
    human = request if isinstance(request, str) else (json.dumps(request) if request is not None else None)
    cols = {"id": tid, "title": title or tid, "status": status, "session_id": session_id,
            "block_kind": block_kind, "human_request": human}
    cols.update(extra)
    names = ", ".join(cols)
    conn.execute(f"INSERT INTO tasks ({names}) VALUES ({', '.join('?' for _ in cols)})", tuple(cols.values()))


def _sub(conn, tid, chat, reason):
    meta = json.dumps({"reason": reason}) if reason is not None else None
    conn.execute("INSERT INTO kanban_notify_subs VALUES (?, 'tui', ?, ?)", (tid, chat, meta))


def _items(ids):
    return so._card_items(list(ids))


@pytest.mark.parametrize("status", ["running", "blocked"])
def test_outcome_reads_current_cards_while_sidebar_reader_is_borrowed(kanban_root, monkeypatch, status):
    from hermes_cli.kanban_reader_lifecycle import cached_entry

    path = kanban_root / "kanban.db"
    conn = _connect(path)
    _insert_task(conn, "t_live", status, "s1", block_kind="needs_input" if status == "blocked" else None,
                 request=HUMAN_REQUEST if status == "blocked" else None)
    conn.commit()
    conn.close()
    monkeypatch.setattr(ksa, "_board_db_paths", lambda: [path])
    monkeypatch.setattr(ksa, "_remote_cards", lambda *a, **k: [])
    monkeypatch.setattr(so, "_provider_items", lambda sid: ([], []))
    monkeypatch.setattr(so, "_process_items", lambda *a: [])
    monkeypatch.setattr(so, "_subagent_items", lambda *a: [])
    monkeypatch.setattr(so, "_goal_items", lambda *a: [])
    _key, entry = cached_entry(path)
    # Hold the real sidebar borrower through the entire outcome read. No timer
    # releases it; successful decisions/waits must come from an independent reader.
    with entry[2]:
        decisions, waits, failed = so._live_work(None, "s1", ["s1"])
    assert not failed, "ordinary shared-reader contention became card_read_failed"
    if status == "blocked":
        assert [d["card_id"] for d in decisions] == ["t_live"]
    else:
        assert [w["id"] for w in waits] == ["t_live"]


@pytest.mark.parametrize("read_raises", [False, True])
def test_lean_reader_releases_owned_connection_on_return_and_error(kanban_root, monkeypatch, read_raises):
    from hermes_cli import kanban_session_outcome_tasks as lean

    path = kanban_root / "kanban.db"
    conn = _connect(path)
    _insert_task(conn, "t_live", "running", "s1")
    conn.commit()
    conn.close()
    baseline = gen._handles(str(path.resolve()))
    opened = []  # Retain strong references: cyclic GC must not provide cleanup.
    real_open = ksa._open_ro

    def capture_open(*args, **kwargs):
        reader = real_open(*args, **kwargs)
        opened.append(reader)
        return reader

    monkeypatch.setattr(ksa, "_open_ro", capture_open)
    if read_raises:
        def fail_selection(*args):
            raise ValueError("selection failed")
        monkeypatch.setattr(lean, "select_listed_local_tasks", fail_selection)
    try:
        if read_raises:
            with pytest.raises(ValueError, match="selection failed"):
                lean._read_lean_session_tasks(path, ["s1"], ("running",))
        else:
            rows = lean._read_lean_session_tasks(path, ["s1"], ("running",))
            assert [row["id"] for row in rows] == ["t_live"]
        assert len(opened) == 1
        assert gen._handles(str(path.resolve())) == baseline, "owned generation handle leaked"
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            opened[0].execute("SELECT 1")
    finally:
        for reader in opened:
            reader.close()


def test_outcome_reader_is_not_the_sidebar_session_tasks():
    assert getattr(ksa, "session_outcome_tasks", ksa.session_tasks) is not ksa.session_tasks


def test_lean_bypasses_enrichment_while_full_still_enriches(kanban_root, monkeypatch):
    path = kanban_root / "kanban.db"
    conn = _connect(path)
    _insert_task(conn, "t_run", "running", "s1", title="Run")
    conn.commit()
    conn.close()
    hits: list[str] = []

    def rec(name, result=None):
        def fn(*a, **k):
            hits.append(name)
            return result if result is not None else (a[0] if name == "add_cards" and a else None)
        return fn

    monkeypatch.setattr(ksa, "_add_card_details", rec("_add_card_details"))
    monkeypatch.setattr("hermes_cli.kanban_execution_source.assemble", rec("assemble", {}))
    monkeypatch.setattr(ksa._receipts, "attach_done", rec("attach_done"))
    monkeypatch.setattr("hermes_cli.kanban_execution_observations.add_cards", rec("add_cards"))

    decisions, waits = _items(["s1"])
    assert decisions == []
    assert [w["id"] for w in waits] == ["t_run"]
    assert not hits, f"lean path called enrichment {hits}"

    listed = ksa.session_tasks(["s1"])
    assert [t["id"] for t in listed] == ["t_run"]
    assert set(hits) >= {"_add_card_details", "assemble", "attach_done", "add_cards"}


@pytest.mark.parametrize("request_value", [
    json.dumps(HUMAN_REQUEST),
    dict(HUMAN_REQUEST),
])
def test_decision_parity_valid_json_text_and_dict(kanban_root, monkeypatch, request_value):
    path = kanban_root / "kanban.db"
    conn = _connect(path)
    if isinstance(request_value, dict):
        _insert_task(conn, "t_ask", "blocked", "s1", title="Ask", block_kind="needs_input",
                     request=json.dumps(request_value))
    else:
        _insert_task(conn, "t_ask", "blocked", "s1", title="Ask", block_kind="needs_input",
                     request=request_value)
    conn.commit()
    conn.close()
    monkeypatch.setattr(ksa, "_remote_cards", lambda *a, **k: [
        {"key": "hub/default/default/t_remote", "id": "t_remote", "board": "default", "gateway": "hub",
         "session": "s1", "status": "blocked", "title": "Remote ask", "block_kind": "needs_input",
         "human_request": request_value}])
    decisions, waits = _items(["s1"])
    assert waits == []
    questions = {d["question"] for d in decisions}
    assert HUMAN_REQUEST["action"] in questions
    assert {d["card_id"] for d in decisions} == {"t_ask", "t_remote"}


@pytest.mark.parametrize("request_value,block_kind,status,expect", [
    ("{not json", "needs_input", "blocked", "wait"),
    ({"action": "incomplete"}, "needs_input", "blocked", "wait"),
    (HUMAN_REQUEST, "transient", "blocked", "wait"),
    (HUMAN_REQUEST, "needs_input", "running", "wait"),
    (HUMAN_REQUEST, "needs_input", "triage", "decision"),
    (HUMAN_REQUEST, "needs_input", "blocked", "decision"),
    (None, None, "review", "wait"),
])
def test_decision_parity_invalid_request_block_kind_and_status(kanban_root, request_value, block_kind, status, expect):
    path = kanban_root / "kanban.db"
    conn = _connect(path)
    _insert_task(conn, "t1", status, "s1", title="Card", block_kind=block_kind, request=request_value)
    conn.commit()
    conn.close()
    decisions, waits = _items(["s1"])
    if expect == "decision":
        assert waits == [] and decisions[0]["card_id"] == "t1"
        assert decisions[0]["question"] == HUMAN_REQUEST["action"]
    else:
        assert decisions == [] and waits[0]["id"] == "t1" and waits[0]["state"] == status


def test_local_park_event_wait_is_derived(kanban_root):
    path = kanban_root / "kanban.db"
    conn = _connect(path)
    _insert_task(conn, "t_sched", "scheduled", "s1", title="Soak")
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'scheduled', ?, 100.0)",
        ("t_sched", json.dumps({"wake_at": 9_999_999_999, "awaiting": {"kind": "clock", "ref": "slot"}})))
    conn.commit()
    conn.close()
    cards = {c["id"]: c for c in _lean()(["s1"])}
    assert cards["t_sched"]["wait"]["wait_state"] == "waiting"
    assert "clock" in cards["t_sched"]["wait"]["waiting_on"] or "slot" in cards["t_sched"]["wait"]["waiting_on"]
    decisions, waits = _items(["s1"])
    assert decisions == []
    # t_01a20c1b37830eaf: a clock park has no running external job.
    assert waits[0]["id"] == "t_sched" and waits[0]["state"] == "scheduled"
    assert waits[0]["wait_kind"] == "clock"
    assert waits[0]["waited_since"] == 100.0
    assert waits[0]["expected_s"] == 9_999_999_899.0
    assert waits[0]["estimate_source"].startswith("wakes ")


def test_fresh_and_stale_remote_waits_and_numeric_eta(kanban_root, monkeypatch):
    reader = _lean()
    fresh = {"key": "hub/default/default/t_fresh", "id": "t_fresh", "board": "default", "gateway": "hub",
             "session": "s1", "status": "scheduled", "title": "Fresh", "stale": False,
             "park": {"wake_at": 9_999_999_999, "outcome": "scheduled"},
             "awaiting": {"kind": "clock", "ref": "window"}}
    stale = {"key": "hub/default/default/t_stale", "id": "t_stale", "board": "default", "gateway": "hub",
             "session": "s1", "status": "scheduled", "title": "Stale", "stale": True,
             "park": {"wake_at": 9_999_999_999}}
    eta_card = {"key": "hub/default/default/t_eta", "id": "t_eta", "board": "default", "gateway": "hub",
                "session": "s1", "status": "running", "title": "Eta",
                "wait": {"until": 1700000000.5, "eta": 12}}
    monkeypatch.setattr(ksa, "_remote_cards", lambda *a, **k: [fresh, stale, eta_card])
    real_wait = ksa._remote_wait

    def remote_wait(card):
        if card.get("stale"):
            return None
        if card["id"] == "t_eta":
            return {"until": 1700000000.5, "eta": 12}
        return real_wait(card)

    monkeypatch.setattr(ksa, "_remote_wait", remote_wait)
    cards = {c["id"]: c for c in reader(["s1"], stale_ok=True)}
    assert "wait" in cards["t_fresh"]
    assert "wait" not in cards["t_stale"]
    assert cards["t_eta"]["wait"]["until"] == 1700000000.5
    decisions, waits = _items(["s1"])
    by_id = {w["id"]: w for w in waits}
    assert "t_fresh" in by_id and "eta" not in by_id["t_fresh"]
    assert by_id["t_eta"]["eta"] == 1700000000.5
    assert decisions == []


def test_creator_subscriber_union_creator_precedence_and_exclusions(kanban_root):
    path = kanban_root / "kanban.db"
    conn = _connect(path)
    _insert_task(conn, "t_mine", "running", "s_creator", title="Mine")
    _insert_task(conn, "t_shep", "ready", "s_other", title="Shepherded")
    _insert_task(conn, "t_touch", "running", "s_other", title="Touched")
    _insert_task(conn, "t_refused", "running", "s_other", title="Refused")
    _insert_task(conn, "t_both", "todo", "s_creator", title="Both")
    _sub(conn, "t_shep", "s_creator", "shepherd")
    _sub(conn, "t_touch", "s_creator", "touch")
    _sub(conn, "t_refused", "s_creator", "send_refused")
    _sub(conn, "t_both", "s_creator", "shepherd")
    conn.commit()
    conn.close()
    rows = _lean()(["s_creator"])
    got = {r["id"]: r for r in rows}
    assert set(got) == {"t_mine", "t_shep", "t_both"}
    assert got["t_mine"]["via"] == "created"
    assert got["t_shep"]["via"] == "subscribed"
    assert got["t_both"]["via"] == "created"
    assert ksa.session_tasks(["s_creator"])  # public sidebar still lists the same ids
    assert {t["id"] for t in ksa.session_tasks(["s_creator"])} == set(got)


def test_watch_park_and_lineage_ids(kanban_root):
    path = kanban_root / "kanban.db"
    conn = _connect(path)
    _insert_task(conn, "t_watch", "scheduled", "s_root", title="Watch")
    _insert_task(conn, "t_live", "running", "s_root", title="Live")
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'scheduled', ?, 1)",
        ("t_watch", json.dumps({"watch": True, "wake_at": 9_999_999_999})))
    conn.commit()
    conn.close()
    assert {r["id"] for r in _lean()(["s_tip"])} == set()
    assert {r["id"] for r in _lean()(["s_root", "s_tip"])} == {"t_live"}
    decisions, waits = _items(["s_root", "s_tip"])
    assert [w["id"] for w in waits] == ["t_live"] and decisions == []


def test_remote_session_origin_session_dedup_and_pending_release(kanban_root, monkeypatch):
    reader = _lean()
    cards = [
        {"key": "hub/default/default/t_dup", "id": "t_dup", "board": "default", "gateway": "hub",
         "session": "s_tip", "origin_session": "s_root", "status": "running", "title": "Dup"},
        {"key": "hub/default/default/t_dup", "id": "t_dup", "board": "default", "gateway": "hub",
         "session": "s_other", "origin_session": "s_root", "status": "ready", "title": "Dup again"},
        {"key": "mini/default/default/t_origin", "id": "t_origin", "board": "default", "gateway": "mini",
         "session": "s_other", "origin_session": "s_root", "status": "todo", "title": "Origin only"},
        {"key": "hub/default/default/t_pend", "id": "t_pend", "board": "default", "gateway": "hub",
         "session": "s_tip", "status": "done_pending_release", "title": "Pending", "unfinished": True},
        {"key": "hub/default/default/t_done", "id": "t_done", "board": "default", "gateway": "hub",
         "session": "s_tip", "status": "done_pending_release", "title": "Released", "unfinished": False},
        {"key": "hub/default/default/t_unk", "id": "t_unk", "board": "default", "gateway": "hub",
         "session": "s_tip", "status": "unknown", "title": "Unknown", "unfinished": True},
    ]
    monkeypatch.setattr(ksa, "_remote_cards", lambda *a, **k: cards)
    got = reader(["s_root", "s_tip"], stale_ok=True)
    ids = [r["id"] for r in got]
    assert ids.count("t_dup") == 1
    assert {r["id"] for r in got} == {"t_dup", "t_origin", "t_pend", "t_unk"}
    decisions, waits = _items(["s_root", "s_tip"])
    assert {w["id"] for w in waits} == {"t_dup", "t_origin", "t_pend", "t_unk"}
    assert decisions == []


def test_empty_list_with_open_counts_is_valueerror(kanban_root, monkeypatch):
    path = kanban_root / "kanban.db"
    conn = _connect(path)
    _insert_task(conn, "t_open", "running", "s1")
    conn.commit()
    conn.close()
    assert ksa.active_task_counts().get("s1", 0) == 1
    monkeypatch.setattr(ksa, "session_outcome_tasks", lambda *a, **k: [])
    with pytest.raises(ValueError, match="empty but sidebar counts retain open work"):
        so._card_items(["s1"])


def test_unreadable_counts_fail_closed(kanban_root, monkeypatch):
    monkeypatch.setattr(ksa, "session_outcome_tasks", lambda *a, **k: [])

    def boom():
        raise sqlite3.OperationalError("counts unavailable")

    monkeypatch.setattr(ksa, "active_task_counts", boom)
    with pytest.raises(sqlite3.OperationalError, match="counts unavailable"):
        so._card_items(["s1"])


class _Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(busy.time, "monotonic", c.monotonic)
    monkeypatch.setattr(busy.time, "sleep", c.sleep)
    busy.reset()
    yield c
    busy.reset()


def test_busy_alternation_does_not_poison_and_copies_retained_rows(clock):
    path = Path("board")
    ids, statuses = ["s1"], ("running",)

    def full_read(*a, **k):
        return [{"id": "t1", "note": "full-note", "wait": {"until": 1.0}}]

    def lean_read(*a, **k):
        return [{"id": "t1", "wait": {"until": 1.0}}]

    def busy_read(*a, **k):
        raise BoardLifecycleBusy("database is locked: board lifecycle is busy")

    def explode(*a, **k):
        raise sqlite3.OperationalError("no such table: tasks")

    full = busy.session_tasks(path, ids, statuses, full_read, deadline=clock.now + 3)
    lean = busy.session_tasks(path, ids, statuses, lean_read, deadline=clock.now + 3, memo_namespace="lean")
    assert full[0]["note"] == "full-note"
    assert "note" not in lean[0]

    full_stale = busy.session_tasks(path, ids, statuses, busy_read, deadline=clock.now + 3)
    lean_stale = busy.session_tasks(path, ids, statuses, busy_read, deadline=clock.now + 3, memo_namespace="lean")
    assert full_stale[0]["note"] == "full-note" and full_stale[0]["stale"] is True
    assert "note" not in lean_stale[0] and lean_stale[0]["stale"] is True

    lean_stale[0]["id"] = "mutated"
    lean_stale[0]["wait"]["until"] = 99.0
    full_stale[0]["note"] = "mutated-note"
    again_lean = busy.session_tasks(path, ids, statuses, busy_read, deadline=clock.now + 3, memo_namespace="lean")
    again_full = busy.session_tasks(path, ids, statuses, busy_read, deadline=clock.now + 3)
    assert again_lean[0]["id"] == "t1" and again_lean[0]["wait"]["until"] == 1.0
    assert again_full[0]["note"] == "full-note"

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        busy.session_tasks(path, ids, statuses, explode, deadline=clock.now + 3, memo_namespace="lean")


def test_busy_successful_empty_cold_and_shared_deadline(clock):
    path = Path("empty")
    ids, statuses = ["s1"], ("running",)

    def empty_read(*a, **k):
        return []

    def busy_read(*a, **k):
        raise BoardLifecycleBusy()

    assert busy.session_tasks(path, ids, statuses, empty_read, deadline=clock.now + 3,
                              memo_namespace="lean") == []
    stale = busy.session_tasks(path, ids, statuses, busy_read, deadline=clock.now + 3, memo_namespace="lean")
    assert stale == [] or all(t.get("stale") for t in stale)

    cold = Path("cold")
    with pytest.raises(sqlite3.OperationalError):
        busy.session_tasks(cold, ids, statuses, busy_read, deadline=clock.now, memo_namespace="lean")


def test_public_session_tasks_still_uses_the_full_memo_namespace(kanban_root, monkeypatch):
    path = kanban_root / "kanban.db"
    conn = _connect(path)
    _insert_task(conn, "t1", "running", "s1", title="Run")
    conn.commit()
    conn.close()
    seen: list[str] = []
    real = busy.session_tasks

    def wrapped(path, ids, statuses, read, *, deadline=None, **kw):
        seen.append(kw.get("memo_namespace", ""))
        return real(path, ids, statuses, read, deadline=deadline, **kw)

    monkeypatch.setattr(busy, "session_tasks", wrapped)
    ksa.session_tasks(["s1"])
    assert seen and seen[0] == ""
    seen.clear()
    _lean()(["s1"])
    assert seen and seen[0] == "lean"


def test_busy_reset_clears_both_namespaces(clock):
    path, ids, statuses = Path("rb"), ["s1"], ("running",)

    def ok(*a, **k):
        return [{"id": "t1"}]

    def busy_read(*a, **k):
        raise BoardLifecycleBusy()

    busy.session_tasks(path, ids, statuses, ok, deadline=clock.now + 3)
    busy.session_tasks(path, ids, statuses, ok, deadline=clock.now + 3, memo_namespace="lean")
    busy.reset()
    for ns in ("", "lean"):
        with pytest.raises(sqlite3.OperationalError):
            busy.session_tasks(path, ids, statuses, busy_read, deadline=clock.now, memo_namespace=ns)


def test_busy_memo_bound_applies_per_namespace_and_evicts_oldest(clock, monkeypatch):
    monkeypatch.setattr(busy, "TASKS_MEMO_MAX", 2)

    def ok(*a, **k):
        return [{"id": "t"}]

    def busy_read(*a, **k):
        raise BoardLifecycleBusy()

    for ns in ("", "lean"):
        for n in range(3):
            busy.session_tasks(Path(f"b{n}"), ["s"], ("running",), ok, deadline=clock.now + 3, memo_namespace=ns)
    assert len(busy._tasks_memo) == 2 and len(busy._lean_tasks_memo) == 2
    for ns in ("", "lean"):
        with pytest.raises(sqlite3.OperationalError):
            busy.session_tasks(Path("b0"), ["s"], ("running",), busy_read, deadline=clock.now, memo_namespace=ns)
        assert busy.session_tasks(Path("b2"), ["s"], ("running",), busy_read, deadline=clock.now,
                                  memo_namespace=ns)[0]["stale"] is True
