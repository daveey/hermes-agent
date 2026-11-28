"""Executable shared scenarios through real core collectors and card projections.

Run from the core root: ``python -m tests.hermes_state.session_state_v2_fixtures``.
JSON stdout is the consumer contract; --case selects one case, --output writes it.
Only source IO is substituted. No status, human-ask, wait or age classifier is mocked.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "session-state-v2"


def load_scenarios():
    data = json.loads((FIXTURES / "scenarios.json").read_text())
    if data["schema_version"] != 1:
        raise ValueError("unsupported shared scenario schema")
    return data


def materialize_case(case, data=None):
    data = data or load_scenarios()
    case = deepcopy(case)
    raw = case.get("card")
    if raw is not None:
        card = {"id": data["defaults"]["card_id"],
                **{k: v for k, v in data["defaults"].items() if k != "card_id"},
                **data["identity"], **raw}
        if case.get("human_request_from"):
            source = next(s for s in data["scenarios"] if s["id"] == case["human_request_from"])
            card["human_request"] = deepcopy(source["card"]["human_request"])
        card["key"] = f"{card['board']}/{card['id']}"
        card["session"] = card["session_id"]
        case["card"] = card
    return case


def _board(conn, card, producer, episodes, now, age):
    # A selected local source, with genuine persisted lifecycle chronology.
    conn.executescript("""
        CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, session_id TEXT,
            block_kind TEXT, human_request TEXT, awaiting TEXT, created_at REAL, updated_at REAL);
        CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT,
            payload TEXT, created_at REAL);
        CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
    """)
    for row in [r for r in (card, producer) if r and not r.get("remote")]:
        conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)", (
            row["id"], row["title"], row["status"], row.get("session_id"), row.get("block_kind"),
            json.dumps(row["human_request"]) if row.get("human_request") else None,
            json.dumps(row["awaiting"]) if row.get("awaiting") else None, now - 900, now))
    if card and not card.get("remote") and not episodes:
        kinds = [{"triage": "status", "scheduled": "scheduled"}.get(card["status"], "created")]
        for i, kind in enumerate(kinds):
            at = now - age if age is not None else now - (len(kinds) - i) * 60
            payload = {"status": card["status"]} if kind in ("created", "status", "scheduled") else {}
            if card.get("awaiting"):
                payload["awaiting"] = card["awaiting"]
            conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                         (card["id"], kind, json.dumps(payload), at))
    conn.commit()


def _card_projection(conn, card, sid, now):
    from hermes_cli import kanban_session_activity as ksa
    from hermes_cli.kanban_activity_projection import project_kanban_activity
    from hermes_cli.kanban_triage_attention import marker, annotate

    active = ksa._counts_on(conn, ksa.ACTIVE_STATUSES)
    blocked = ksa._counts_on(conn, ksa.BLOCKED_STATUSES)
    parked, held = ksa._parked_on(conn)
    keys, waiting, stale = {}, {}, {}
    if card and card["status"] in ksa.LISTED_STATUSES:
        if card.get("remote"):
            bucket = ksa._replica_bucket(card)
            target = active if bucket == "active" else blocked
            target[sid] = target.get(sid, 0) + 1
            if card.get("stale"):
                stale[sid] = 1
        else:
            bucket = "blocked" if blocked.get(sid) else "active"
            waiting[sid] = {card["id"]}
        rows = [deepcopy(card)]
        annotate(conn, rows)
        keys[sid] = {(card["key"], bucket, bool(parked.get(sid)))} | marker(rows[0])
        if not card.get("remote"):
            for timed in ksa._identity.timers.local(conn, {(sid, card['id'])}).values():
                timed['key'] = card['key']
                keys[sid].update(ksa._identity.timers.marker(timed))
    parts = (active, blocked, {}, stale, waiting, parked, held, {}, keys, {}, {}, {})
    row = {"id": sid, "profile": "default"}
    project_kanban_activity([row], parts=parts, observed_at=now, now=now, external={})
    return row


def run_case(raw_case, data=None):
    """Return scenario input, selected cards, published envelope and legacy row."""
    from hermes_state import SessionDB
    import hermes_state_outcome as so
    from hermes_cli import kanban_session_activity as ksa
    from hermes_cli import process_session_activity as psa
    from hermes_cli import subagent_session_activity as ssa
    from hermes_cli.kanban_session_outcome_tasks import select_listed_local_tasks
    from hermes_cli.kanban_session_wait_rows import _local_wait_info
    from hermes_cli.kanban_session_activity_card_details import _add_card_details
    from tools.process_registry import process_registry

    data = data or load_scenarios()
    case = materialize_case(raw_case, data)
    now, sid = data["now"], data["identity"]["session_id"]
    card, runtime = case.get("card"), case.get("runtime", {})
    with tempfile.TemporaryDirectory(prefix="session-v2-") as temp, ExitStack() as stack:
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        home_token = set_hermes_home_override(temp)
        stack.callback(reset_hermes_home_override, home_token)
        stack.enter_context(patch("time.time", return_value=now))
        stack.enter_context(patch.dict(so._providers, {}, clear=True))
        stack.enter_context(patch.object(so, "_judge", [None]))
        stack.enter_context(patch("hermes_cli.kanban_origin.gateway_names", return_value=(data["identity"]["gateway"],)))
        stack.enter_context(patch("hermes_cli.kanban_execution_observations.local_scope",
                                  return_value=(data['identity']['gateway'], data['identity']['profile'])))
        stack.enter_context(patch.object(so, "_live_turn_tokens", return_value=iter(())))
        stack.enter_context(patch("hermes_state_session_status_snapshot._live_tokens", return_value=set()))
        stack.enter_context(patch.object(so, "_cron_items", return_value=[]))
        stack.enter_context(patch.object(so, "_watch_items", return_value=[]))
        stack.enter_context(patch.object(psa, "_checkpoint_paths", return_value=[]))
        stack.enter_context(patch("hermes_state_process_checkpoint.checkpoint_items", return_value=[]))
        process = runtime.get("kind") == "process"
        proc = SimpleNamespace(id=runtime.get("id"), exited=False, pid_scope="remote", pid=None,
            host_start_time=None, session_key=sid, parent_session_id=sid, owner_task_id="default",
            task_id="default", command=runtime.get("title"), started_at=now - 60)
        stack.enter_context(patch.object(process_registry, "_running", {proc.id: proc} if process else {}))
        stack.enter_context(patch.object(process_registry, "list_sessions", return_value=[
            {"session_id": proc.id, "status": "running"}] if process else []))
        # Reset only this source cache, restoring it when leaving the helper.
        stack.enter_context(patch.object(psa, "_cache", None))
        stack.enter_context(patch.object(psa, "_details", {}))
        stack.enter_context(patch.object(psa, "_evidence", {}))
        db = stack.enter_context(SessionDB(db_path=Path(temp) / "state.db"))
        db.create_session(sid, source="desktop")
        db.append_message(sid, role="user", content="Complete the fixture work.")
        reply_text = "Implemented the fixture work; verified all checks pass."
        display_kind = None
        if case.get("turn") == "failed":
            from agent.turn_failure_copy import FAILED_TURN_DISPLAY_KIND
            reply_text, display_kind = "The turn failed.", FAILED_TURN_DISPLAY_KIND
        db.append_message(sid, role="assistant", content=reply_text, display_kind=display_kind)
        if runtime.get("kind") == "subagent":
            db.create_session(runtime["id"], source="subagent", parent_session_id=sid)
            db.set_session_title(runtime["id"], runtime["title"])
        conn = stack.enter_context(sqlite3.connect(":memory:"))
        conn.row_factory = sqlite3.Row
        _board(conn, card, case.get("producer"), case.get("episodes"), now, case.get("triage_age_s"))
        history = []
        # Each episode is refreshed and persisted, not a synthetic previous envelope.
        episodes = case.get("episodes") or [None]
        cards = []
        outcome = None
        for episode_index, episode in enumerate(episodes):
            current = deepcopy(card)
            if episode:
                status = {"blocked": "blocked", "unblocked": "ready"}[episode]
                current["status"] = status
                if status == "blocked":
                    brief = next(s for s in data["scenarios"] if s["id"] == "human_brief")
                    current.update(block_kind="needs_input", human_request=brief["card"]["human_request"])
                conn.execute("UPDATE tasks SET status=?, block_kind=?, human_request=? WHERE id=?", (
                    status, current.get("block_kind"), json.dumps(current.get("human_request")), current["id"]))
                conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                             (current['id'], episode, '{}', now - (len(episodes) - episode_index) * 60))
                conn.commit()
            found = select_listed_local_tasks(conn, [sid], ksa.LISTED_STATUSES)
            for tid, wait in _local_wait_info(conn, found, now=now, replica=None).items():
                found[tid]["wait"] = wait
            _add_card_details(conn, found, replica=None)
            cards = [{**row, "board": data["defaults"]["board"]} for row in found.values()]
            if current and current.get("remote"):
                cards.append(current)
            with patch.object(ksa, "session_outcome_tasks", return_value=cards), patch.object(
                    so, "_agreement_counts", return_value=({}, {})):
                outcome = so.refresh_outcome(sid, "card_change", db=db)
                assert so.get_outcome(sid, db=db) == outcome
                # Repeat collection must retain the published semantic revision.
                assert so.refresh_outcome(sid, "manual", db=db) == outcome
            history.append(deepcopy(outcome))
        assert outcome is not None
        row = _card_projection(conn, card, sid, now)
        row["outcome"] = outcome
        with patch.object(ssa, '_profile_paths', return_value=[('default', Path(db.db_path))]), patch.object(
                ssa, 'local_scope', return_value=(data['identity']['gateway'], data['identity']['profile'])):
            ssa.annotate_subagent_activity([row])
        if process:
            psa.annotate_background_activity([row])
        return {"id": case["id"], "input": deepcopy(raw_case), "card": card,
                "cards": cards, "outcome": outcome, "session_row": row, "history": history}


def run_all(case_id=None):
    data = load_scenarios()
    cases = [case for case in data["scenarios"] if case_id is None or case["id"] == case_id]
    if not cases:
        raise ValueError(f"unknown scenario {case_id!r}")
    return {"schema_version": data["schema_version"], "now": data["now"],
            "cases": [run_case(case, data) for case in cases]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    text = json.dumps(run_all(args.case), indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
