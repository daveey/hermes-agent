"""Stored end-of-turn outcome per chat: the sidebar paints from it instead of inferring "done" from idleness.

Every non-archived chat has exactly one stored outcome (``sessions.outcome_json``)::

    done{summary} | decision{items} | waiting{on} | running | muted | unresolved{reason}

``declare_outcome`` stores what the agent said about its own final turn (``source="declared"``);
``derive_outcome`` computes the outcome from the chat's live state and transcript without writing;
``refresh_outcome`` derives and writes when the result changed. Call sites refresh at every turn end
(Desktop/TUI and messaging platforms), on orphan reap and on mute; the trigger names the caller.

Plugins extend the derivation through two process-global hooks registered at load time:
``register_outcome_provider`` (open decision items and extra waits, e.g. user-steps) and
``register_outcome_judge`` (is the last assistant message a summary; None falls back to the heuristic).

Must never import ``hermes_state`` at module level (cycle): every function takes an optional ``db`` and
otherwise opens the default ``SessionDB`` itself.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

logger = logging.getLogger(__name__)

OUTCOME_KINDS = ("done", "decision", "waiting", "running", "muted", "unresolved")
DECLARABLE_KINDS = ("done", "decision", "waiting")
WAIT_KINDS = ("card", "process", "cron", "time", "capacity", "subagent", "goal")
UNRESOLVED_REASONS = ("notice_tail", "no_summary", "turn_failed", "interrupted", "subagent_killed", "empty")
TRIGGERS = ("turn_end", "notice", "card_change", "reap", "mute", "manual")
SUMMARY_CHARS = 400
# A delegate child with an open row but no activity for this long is a leak, not live work: the
# orphan reap ends it on the next backend start.
SUBAGENT_LIVE_WINDOW_S = 6 * 3600.0
# How far back the transcript is read: the tail classification needs only the last few rows.
_TAIL_ROWS = 40
_ERROR_FINISH_REASONS = frozenset({"error", "agent_error", "content_filter"})
# The declaration tool's own result row follows the declaration; it does not supersede it.
_OUTCOME_TOOL = "session_outcome"
_NOTICE_KIND = "kanban_notice"
# ``messages.display_kind`` of a row a person wrote: a plain message, or one steered into a running turn.
_TYPED_KINDS = frozenset({None, "steer"})
_PROGRESS_OPENERS = ("let me ", "i'll ", "i will ", "now i", "next, i", "next i ", "working on")

ProviderFn = Callable[[str], Optional[dict]]
# ``fn(sid, text, *, source=None)``; a judge without the ``source`` keyword is called as ``fn(sid, text)``.
JudgeFn = Callable[..., Optional[bool]]

_providers: dict[str, ProviderFn] = {}
_judge: list[Optional[JudgeFn]] = [None]


# ── Hooks ──────────────────────────────────────────────────────────────────


def register_outcome_provider(name: str, fn: ProviderFn) -> None:
    """Register ``fn(sid) -> {"decision_items": [...], "waiting_on": [...]} | None`` under ``name``
    (re-registering a name replaces it). Items are normalised; a raising provider is logged and skipped."""
    if not name or not callable(fn):
        raise ValueError("register_outcome_provider needs a name and a callable")
    _providers[str(name)] = fn


def register_outcome_judge(fn: Optional[JudgeFn]) -> None:
    """Set the summary judge ``fn(sid, last_assistant_text, *, source=None) -> True | False | None`` (None =
    can't tell, use the heuristic; ``source`` is the row's ``sessions.source``, and a judge that does not take
    it is called without it). One judge per process; ``None`` clears it."""
    if fn is not None and not callable(fn):
        raise ValueError("register_outcome_judge needs a callable")
    _judge[0] = fn


def _reset_hooks_for_tests() -> None:
    _providers.clear()
    _judge[0] = None


# ── Validation ─────────────────────────────────────────────────────────────


def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _choices(raw: Any) -> list[dict[str, str]]:
    out = []
    for choice in raw if isinstance(raw, list) else ():
        if isinstance(choice, dict) and _str(choice.get("label")):
            label = _str(choice.get("label"))
            out.append({"label": label, "value": _str(choice.get("value")) or label})
        elif _str(choice):
            out.append({"label": _str(choice), "value": _str(choice)})
    return out


def _decision_item(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict) or not _str(raw.get("question")):
        return None
    state = _str(raw.get("state")) or "open"
    if state != "open":
        return None
    item: dict[str, Any] = {"id": _str(raw.get("id")) or f"q:{_str(raw.get('question'))[:40]}",
                            "question": _str(raw.get("question")), "choices": _choices(raw.get("choices")),
                            "state": "open"}
    if _str(raw.get("card_id")):
        item["card_id"] = _str(raw.get("card_id"))
    return item


def _wait_item(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict) or _str(raw.get("kind")) not in WAIT_KINDS:
        return None
    kind = _str(raw.get("kind"))
    item: dict[str, Any] = {"kind": kind, "id": _str(raw.get("id")) or kind,
                            "label": _str(raw.get("label")) or _str(raw.get("id")) or kind}
    if _str(raw.get("state")):
        item["state"] = _str(raw.get("state"))
    eta = raw.get("eta")
    if isinstance(eta, (int, float)) and not isinstance(eta, bool):
        item["eta"] = float(eta)
    return item


def validate_declaration(outcome: Any) -> dict[str, Any]:
    """The canonical form of a declared outcome; ``ValueError`` names what is wrong."""
    if not isinstance(outcome, dict):
        raise ValueError("outcome must be an object")
    kind = _str(outcome.get("kind"))
    if kind not in DECLARABLE_KINDS:
        raise ValueError(f"outcome kind must be one of {', '.join(DECLARABLE_KINDS)}")
    out: dict[str, Any] = {"kind": kind}
    if "completion_receipt" in outcome:
        from hermes_completion_receipt import validate_completion_receipt

        if kind != "done":
            raise ValueError("completion_receipt is only valid on a done declaration")
        out["completion_receipt"] = validate_completion_receipt(outcome["completion_receipt"])
    if kind == "done":
        summary = _str(outcome.get("summary"))
        if not summary:
            raise ValueError("a done outcome needs a summary")
        out["summary"] = summary[:SUMMARY_CHARS]
        if outcome.get("keep_crons") is True:
            out["keep_crons"] = True
    elif kind == "decision":
        raw_items = outcome.get("items")
        items = [_decision_item(i) for i in raw_items] if isinstance(raw_items, list) else []
        if not raw_items or any(i is None for i in items):
            raise ValueError("a decision outcome needs items, each with a question")
        if any(len(i["choices"]) < 2 for i in items):
            raise ValueError("each decision item needs at least 2 choices")
        out["items"] = items
    else:
        raw_on = outcome.get("on")
        on = [_wait_item(i) for i in raw_on] if isinstance(raw_on, list) else []
        if not raw_on or any(i is None for i in on):
            raise ValueError(f"a waiting outcome needs on[] items with kind in {', '.join(WAIT_KINDS)}")
        out["on"] = on
    return out


# ── DB access ──────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _db_scope(db: Any) -> Iterator[Any]:
    if db is not None:
        yield db
        return
    from hermes_state import SessionDB

    own = SessionDB()
    try:
        yield own
    finally:
        own.close()


def _tip(db: Any, sid: str) -> str:
    return db.get_compression_tip(sid) or sid


def _lineage_ids(db: Any, sid: str) -> list[str]:
    from hermes_state_sessions import _LINEAGE_CTE_SQL

    rows = db._read_all(_LINEAGE_CTE_SQL + " SELECT id FROM lineage", (sid, sid))
    ids = [str(r["id"]) for r in rows]
    return ids or [sid]


def _parse(raw: Any) -> Optional[dict[str, Any]]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) and value.get("kind") in OUTCOME_KINDS else None


def _row(db: Any, sid: str) -> Optional[dict[str, Any]]:
    row = db._read_one("SELECT id, source, end_reason, outcome_json, outcome_at, muted FROM sessions WHERE id = ?", (sid,))
    return dict(row) if row is not None else None


def _tail(db: Any, sid: str) -> list[dict[str, Any]]:
    """The last active transcript rows, newest first."""
    rows = db._read_all(
        "SELECT id, role, content, tool_calls, tool_name, finish_reason, display_kind, display_metadata, timestamp"
        " FROM messages"
        " WHERE session_id = ? AND active = 1 AND role IN ('user', 'assistant', 'tool')"
        " ORDER BY id DESC LIMIT ?", (sid, _TAIL_ROWS))
    return [dict(r) for r in rows]


def _text(content: Any) -> str:
    if not isinstance(content, str):
        return ""
    stripped = content.strip()
    if stripped.startswith("["):
        with contextlib.suppress(ValueError, TypeError):
            parts = json.loads(stripped)
            if isinstance(parts, list):
                return "\n".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)).strip()
    return stripped


# ── Derivation ─────────────────────────────────────────────────────────────


def _live_turn(db: Any, sid: str) -> bool:
    """A live turn has a durable writer lease, a live TUI marker, or a gateway active-turn token.

    Managed workers and CLI turns need no TUI marker or gateway routing entry. Their shared
    turn-facade lease is the cross-process evidence; an idle attached session is not a turn."""
    from tui_gateway.turn_marker import marker_writer_state, read_turn_marker

    home = Path(db.db_path).parent
    marker = read_turn_marker(home, sid)
    if marker and marker_writer_state(marker) == "alive":
        return True
    if _guarded("durable turn", sid, lambda: _durable_turn_live(db, sid), False):
        return True
    return _guarded("gateway turn", sid, lambda: _gateway_turn_live(db, sid), False)


def _durable_turn_live(db: Any, sid: str) -> bool:
    """Read the compression-root writer without reclaiming it; expired authority is not liveness.

    The store rejects dead local owners. Expiry also bounds abandoned releases and unknown
    remote owners; unlike takeover we need not wait for an expired live process to acknowledge.
    """
    from hermes_state_compression import _turn_lease_abandoned

    owner = db.session_turn_lease_owner(sid)
    return (owner is not None and float(owner["expires_at"]) > time.time()
            and not _turn_lease_abandoned(db.db_path, owner["holder"]))


# [daveey] A gateway turn token older than this is a leaked marker (its CAS clear failed), not a live turn.
_GATEWAY_TURN_MAX_AGE_S = 6 * 3600.0


def _gateway_turn_live(db: Any, sid: str) -> bool:
    """[daveey] A gateway routing entry for ``sid`` carries ``active_turn_token`` (``mark_turn_active`` at
    turn start, CAS-cleared at its end) and the gateway owning that routing scope is running. A crashed
    gateway leaves the token behind until its next start recovers it, so a dead gateway reads not-live."""
    from hermes_state_routing_snapshot import active_routing_entries

    entries = active_routing_entries(db, sid)
    now = time.time()
    for routing_scope, entry in entries:
        started = _iso_epoch(entry.get("active_turn_started_at"))
        if started is None or now - started > _GATEWAY_TURN_MAX_AGE_S:
            continue
        scope = str(routing_scope or "")
        gateway_home = Path(scope).parent if scope else Path(db.db_path).parent
        if _gateway_running(gateway_home):
            return True
    return False


def _iso_epoch(value: Any) -> Optional[float]:
    from datetime import datetime

    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _gateway_running(home: Path) -> bool:
    from gateway.status import get_running_pid

    return get_running_pid(home / "gateway.pid", cleanup_stale=False) is not None


def _card_items(ids: list[str]) -> tuple[list[dict], list[dict]]:
    """(decision items, waits) from the cards these chats created or shepherd: a card parked on a
    validated ``human_request`` is a decision, every other open card is a wait."""
    from hermes_cli.kanban_human_request import needs_human
    from hermes_cli.kanban_session_activity import LISTED_STATUSES, session_tasks

    decisions, waits = [], []
    for card in session_tasks(ids, LISTED_STATUSES, stale_ok=True):
        tid = str(card.get("id") or "")
        request = card.get("human_request")
        if isinstance(request, str):
            with contextlib.suppress(ValueError):
                request = json.loads(request)
        status = str(card.get("status") or "")
        wait = card.get("wait") if isinstance(card.get("wait"), dict) else {}
        human = status in ("blocked", "triage") and (
            needs_human(request, kind=card.get("block_kind")))
        if human:
            req = request if isinstance(request, dict) else {}
            decisions.append({"id": f"card:{tid}", "question": _str(req.get("action")) or _str(card.get("title")),
                              "choices": _choices(req.get("choices")), "card_id": tid, "state": "open"})
            continue
        item = {"kind": "card", "id": tid, "label": _str(card.get("title")) or tid, "state": status}
        eta = wait.get("until") or wait.get("eta")
        if isinstance(eta, (int, float)) and not isinstance(eta, bool):
            item["eta"] = float(eta)
        waits.append(item)
    return decisions, waits


def _cron_items(ids: list[str]) -> list[dict]:
    from hermes_cli import cron_session_activity as cron

    wanted, out = set(ids), []
    for path in cron._jobs_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig") or "[]")
        except (OSError, ValueError):
            logger.debug("cron jobs unreadable at %s", path, exc_info=True)
            continue
        jobs = data.get("jobs", []) if isinstance(data, dict) else data
        for job in jobs if isinstance(jobs, list) else ():
            if not (isinstance(job, dict) and cron._job_is_live(job)):
                continue
            if str(job.get("owner_session_id") or "").strip() in wanted:
                jid = str(job.get("id") or job.get("name") or "cron")
                out.append({"kind": "cron", "id": jid, "label": str(job.get("name") or jid), "state": "scheduled"})
    return out


def _process_items(sid: str, ids: list[str]) -> list[dict]:
    from hermes_cli.process_session_activity import annotate_background_activity

    row = annotate_background_activity([{"id": sid, "_lineage_ids": ids}])[0]
    return [{"kind": "process", "id": str(p.get("id")), "label": str(p.get("command") or p.get("id"))[:120],
             "state": "running"} for p in row.get("background_processes") or ()]


def _goal_items(sid: str, ids: list[str]) -> list[dict]:
    from hermes_cli.goal_session_activity import annotate_goal_activity

    row = annotate_goal_activity([{"id": sid, "_lineage_ids": ids}])[0]
    return [{"kind": "goal", "id": sid, "label": "standing goal", "state": "running"}] if row.get("goal_active") else []


def _subagent_items(db: Any, ids: list[str]) -> list[dict]:
    from hermes_state_common import _placeholders, _sql_json_extract

    rows = db._read_all(
        f"SELECT id, title FROM sessions WHERE parent_session_id IN ({_placeholders(ids)}) AND ended_at IS NULL"
        f" AND (source = 'subagent' OR {_sql_json_extract('model_config', '$._delegate_from')} IS NOT NULL)"
        " AND COALESCE(last_activity_at, started_at) >= ?", (*ids, time.time() - SUBAGENT_LIVE_WINDOW_S))
    return [{"kind": "subagent", "id": str(r["id"]), "label": r["title"] or "subagent", "state": "running"}
            for r in rows]


def _provider_items(sid: str) -> tuple[list[dict], list[dict]]:
    decisions, waits = [], []
    for name, fn in list(_providers.items()):
        try:
            result = fn(sid)
        except Exception:
            logger.warning("session outcome provider %s failed for %s", name, sid, exc_info=True)
            continue
        if not isinstance(result, dict):
            continue
        decisions += [i for i in map(_decision_item, result.get("decision_items") or ()) if i]
        waits += [i for i in map(_wait_item, result.get("waiting_on") or ()) if i]
    return decisions, waits


def _guarded(label: str, sid: str, fn: Callable[[], Any], empty: Any) -> Any:
    """One live-work source; a failed read is logged and counts as nothing from that source."""
    try:
        return fn()
    except Exception:
        logger.warning("session outcome: %s unavailable for %s", label, sid, exc_info=True)
        return empty


def _declared(stored: Optional[dict]) -> Optional[dict]:
    if not stored:
        return None
    if stored.get("source") == "declared":
        return stored
    nested = stored.get("declared")
    return nested if isinstance(nested, dict) and nested.get("source") == "declared" else None


def _declaration_holds(declared: dict, tail: list[dict]) -> bool:
    """A declaration covers the turn that made it: no newer user or notice row was written after it,
    and the transcript does not end on a tool result other than the declaration's own."""
    at = declared.get("at") or 0
    for row in tail:
        if row["role"] == "user" and float(row.get("timestamp") or 0) > at:
            return False
    last = tail[0] if tail else None
    if last is None:
        return True
    if last["role"] == "user":
        return False
    return not (last["role"] == "tool" and (last.get("tool_name") or "") != _OUTCOME_TOOL)


def _lost_request(tail: list[dict]) -> Optional[str]:
    """Why a person's latest request got no answer, or None. It outranks live work: an open card says nothing about
    the message that failed, and a chat painted "waiting" hides it from the only person who can resend it. Automated
    rows after the failure (a process completion refused the same way) don't close it; only a real reply does."""
    from agent.turn_failure_copy import AUTOMATION_INTERRUPTED_NOTICE, FAILED_TURN_DISPLAY_KIND

    failed = False
    for row in tail:
        kind = row.get("display_kind")
        if row["role"] == "assistant":
            if kind == FAILED_TURN_DISPLAY_KIND:
                if _text(row.get("content")).strip() == AUTOMATION_INTERRUPTED_NOTICE:
                    return None
                failed = True
            elif _text(row.get("content")).strip() and not row.get("tool_calls"):
                return None
        elif row["role"] == "user" and kind in _TYPED_KINDS:
            return "turn_failed" if failed else "interrupted"
    return None


def _completion_note(row: dict) -> Optional[dict]:
    """The display metadata of a completion written into the chat as a note because no viewer was attached
    (``gateway/session_completion_notes.py``, #134889 LC-7), else None. A note never runs a turn by design, so it
    is not a request awaiting an answer: a chat that ends on one was not interrupted."""
    if row.get("role") != "user":
        return None
    meta = row.get("display_metadata")
    if isinstance(meta, str):
        with contextlib.suppress(ValueError, TypeError):
            meta = json.loads(meta)
    # The notice guarantee also deliberately suppresses turns for validated human
    # notices. Card ownership still supplies decision/wait outcomes independently;
    # an incidental recipient resumes classification of its own preceding turn.
    if isinstance(meta, dict) and row.get("display_kind") == _NOTICE_KIND and meta.get("outcome_informational") is True:
        return meta
    return meta if isinstance(meta, dict) and meta.get("completion_note") else None


def _unfinished_note(row: dict) -> bool:
    """An async delegation's note whose subagent did not finish (failed, interrupted by a restart, timed out)."""
    meta = _completion_note(row)
    if meta is None or row.get("display_kind") != "async_delegation_complete":
        return False
    from tools.process_registry_notifications import async_delegation_display_unfinished

    return async_delegation_display_unfinished(str(meta.get("display_text") or ""))


def _without_finished_notes(tail: list[dict]) -> list[dict]:
    """[daveey] ``tail`` with its trailing completion notes for finished work dropped: the chat's own last turn is
    then its last word. The newest note for unfinished work stops the drop and heads the tail, so ``_tail_reason``
    names it."""
    i = 0
    while i < len(tail) and _completion_note(tail[i]) is not None:
        if _unfinished_note(tail[i]):
            return tail[i:]
        i += 1
    return tail[i:] if 0 < i < len(tail) else tail


def _tail_reason(tail: list[dict]) -> Optional[str]:
    last = tail[0]
    if _unfinished_note(last):
        # A subagent the chat was waiting on stopped before it finished; with no viewer the note ran no turn.
        return "subagent_killed"
    if last.get("display_kind") == _NOTICE_KIND:
        # The orphan reap's notice to a parent whose subagent a restart killed (tui_gateway/reap_notices.py).
        return "subagent_killed" if '"subagent_killed"' in str(last.get("display_metadata") or "") else "notice_tail"
    if last["role"] in ("user", "tool") or (last["role"] == "assistant" and last.get("tool_calls")):
        return "interrupted"
    if (last.get("finish_reason") or "").strip().lower() in _ERROR_FINISH_REASONS:
        return "turn_failed"
    return None


def looks_like_summary(text: str) -> bool:
    """Heuristic fallback when no judge can tell: a closing report is more than a line, is not a
    question back to the user, and does not announce more work."""
    text = (text or "").strip()
    if len(text) < 40:
        return False
    lower = text.lower()
    if lower.startswith(_PROGRESS_OPENERS):
        return False
    return not text.splitlines()[-1].rstrip().endswith("?")


def _call_judge(judge: JudgeFn, sid: str, text: str, source: Optional[str]) -> Optional[bool]:
    try:
        return judge(sid, text, source=source)
    except TypeError:  # a judge written before ``source`` was passed
        return judge(sid, text)


def _judge_summary(sid: str, text: str, source: Optional[str]) -> Optional[bool]:
    """The registered judge's verdict, or None when there is none or it can't tell."""
    judge = _judge[0]
    if judge is None:
        return None
    try:
        verdict = _call_judge(judge, sid, text, source)
    except Exception:
        logger.warning("session outcome judge failed for %s; using the heuristic", sid, exc_info=True)
        return None
    return verdict if verdict is True or verdict is False else None


def _prior_verdict(stored: Optional[dict], msg_id: Any) -> Optional[bool]:
    """A judge's earlier verdict on this same last assistant message, kept by a refresh that has no judge (the
    CLI ``outcome-audit --refresh``, the web ``?refresh=1``): the heuristic never overrides it."""
    if not stored or msg_id is None or not stored.get("judged") or stored.get("judged_msg") != msg_id:
        return None
    if stored.get("kind") == "done":
        return True
    return False if stored.get("reason") == "no_summary" else None


def _outcome(kind: str, now: float, **fields: Any) -> dict[str, Any]:
    return {"kind": kind, "source": "derived", "at": now, **fields}


def _live_work(db: Any, sid: str, ids: list[str]) -> tuple[list[dict], list[dict]]:
    """(open decision items, named waits) across every live-work source. A recurring cron is not one: it never
    ends, so a chat that owns one would wait forever (see ``_derive``, which names it on the outcome instead)."""
    card_decisions, card_waits = _guarded("kanban cards", sid, lambda: _card_items(ids), ([], []))
    prov_decisions, prov_waits = _provider_items(sid)
    waits = (card_waits + _guarded("processes", sid, lambda: _process_items(sid, ids), [])
             + _guarded("subagents", sid, lambda: _subagent_items(db, ids), [])
             + _guarded("goal", sid, lambda: _goal_items(sid, ids), []) + prov_waits)
    return prov_decisions + card_decisions, waits


def _derive(db: Any, sid: str, *, ignore_live_turn: bool = False,
            pending_reply: Optional[str] = None) -> dict[str, Any]:
    now = time.time()
    row = _row(db, sid) or {}
    stored = _parse(row.get("outcome_json"))
    if row.get("muted"):
        return _outcome("muted", now, **({"declared": _declared(stored)} if _declared(stored) else {}))
    if not ignore_live_turn and _live_turn(db, sid):
        return _outcome("running", now)
    decisions, waits = _live_work(db, sid, _lineage_ids(db, sid))
    tail = _without_finished_notes(_tail(db, sid))
    if pending_reply is not None:  # the unsaved final reply is the last assistant message
        tail.insert(0, {"role": "assistant", "content": pending_reply, "timestamp": now})
    declared = _declared(stored)
    holds = declared is not None and _declaration_holds(declared, tail)
    carry = {"declared": declared} if holds else {}
    if decisions:
        return _outcome("decision", now, items=decisions, **carry)
    lost = None if holds else _lost_request(tail)  # a turn that declared its outcome answered the request
    if lost:
        return _outcome("unresolved", now, reason=lost, **carry)
    if waits:
        return _outcome("waiting", now, on=waits, **carry)
    crons = _guarded("cron", sid, lambda: _cron_items(_lineage_ids(db, sid)), [])
    named = {"crons": crons} if crons else {}
    if holds and declared.get("kind") in ("done", "waiting"):
        return {**{k: v for k, v in declared.items() if k != "crons"},
                **(named if declared.get("kind") == "done" else {})}
    if not tail:
        return _outcome("unresolved", now, reason="empty", **carry)
    reason = _tail_reason(tail)
    if reason:
        return _outcome("unresolved", now, reason=reason, **carry)
    text = _text(tail[0].get("content"))
    # Cron's successful no-news protocol deliberately has no prose summary. Respect that
    # terminal receipt, but not a marker in an unfinished/failed run or a pending reply.
    # Keep this after live work, notices and failure checks: silence cannot erase obligations.
    if pending_reply is None and row.get("source") == "cron" and row.get("end_reason") == "cron_complete":
        from gateway.response_filters import is_intentional_silence_response

        if is_intentional_silence_response(text):
            return _outcome("done", now, summary="Scheduled run completed with nothing to report.", **named, **carry)
    msg_id = tail[0].get("id")
    verdict = _judge_summary(sid, text, row.get("source"))
    if verdict is None:
        verdict = _prior_verdict(stored, msg_id)
    judged = {"judged": True, "judged_msg": msg_id} if verdict is not None and msg_id is not None else {}
    if verdict is None:
        verdict = looks_like_summary(text)
    if verdict:
        return _outcome("done", now, summary=text[:SUMMARY_CHARS], **judged, **named, **carry)
    return _outcome("unresolved", now, reason="no_summary", **judged, **carry)


def _same(a: Optional[dict], b: Optional[dict]) -> bool:
    if a is None or b is None:
        return a is b

    def strip(o: dict) -> dict:
        keep_at = o.get("source") == "declared"
        return {k: (strip(v) if isinstance(v, dict) else v) for k, v in o.items() if keep_at or k != "at"}

    return strip(a) == strip(b)


def _write(db: Any, sid: str, outcome: dict) -> None:
    db._write_rowcount("UPDATE sessions SET outcome_json = ?, outcome_at = ? WHERE id = ?",
                       (json.dumps(outcome, ensure_ascii=False, separators=(",", ":")), time.time(), sid))


# ── Public API ─────────────────────────────────────────────────────────────


def declare_outcome(sid: str, outcome: dict, *, db: Any = None) -> dict[str, Any]:
    """Validate and store the agent's own outcome for its current turn (``source="declared"``).
    Raises ``ValueError`` on an invalid outcome or an unknown session."""
    declared = {**validate_declaration(outcome), "source": "declared", "at": time.time()}
    with _db_scope(db) as handle:
        tip = _tip(handle, sid)
        if _row(handle, tip) is None:
            raise ValueError(f"unknown session {sid}")
        # Persist intent before touching the separate cron store. A failed DB
        # write must never release ownership without a completion receipt.
        _write(handle, tip, declared)
        if declared["kind"] == "done":
            from cron.session_ownership import release_for_database

            declared["released_crons"], errors = release_for_database(
                handle, _lineage_ids(handle, tip), keep=bool(declared.get("keep_crons")))
            if errors:
                declared["cron_release_error"] = "; ".join(errors)
            try:
                _write(handle, tip, declared)
            except sqlite3.Error as exc:
                # The declaration and cron writes already succeeded; report the
                # receipt failure rather than pretending nothing was committed.
                previous = declared.get("cron_release_error", "")
                declared["cron_release_error"] = (previous + "; " if previous else "") + f"Outcome receipt not persisted: {exc}"
    return declared


def derive_outcome(sid: str, *, db: Any = None, ignore_live_turn: bool = False,
                   pending_reply: Optional[str] = None) -> dict[str, Any]:
    """The outcome the chat is in now. Pure: reads only, never writes. ``ignore_live_turn`` skips the
    running step (a caller inside the turn); ``pending_reply`` is the not-yet-saved final assistant
    text, judged as the last assistant message."""
    with _db_scope(db) as handle:
        return _derive(handle, _tip(handle, sid), ignore_live_turn=ignore_live_turn, pending_reply=pending_reply)


def refresh_outcome(sid: str, trigger: str, *, db: Any = None) -> dict[str, Any]:
    """Derive, write when it changed, and return the outcome now stored."""
    if trigger not in TRIGGERS:
        raise ValueError(f"unknown outcome trigger {trigger!r}")
    with _db_scope(db) as handle:
        tip = _tip(handle, sid)
        row = _row(handle, tip)
        derived = _derive(handle, tip)
        if row is None:
            return derived
        stored = _parse(row.get("outcome_json"))
        if _same(stored, derived):
            return stored
        _write(handle, tip, derived)
        logger.debug("session outcome %s -> %s (%s)", tip, derived.get("kind"), trigger)
        return derived


def get_outcome(sid: str, *, db: Any = None) -> Optional[dict[str, Any]]:
    """The stored outcome of the chat's live tip, or None when none was stored yet."""
    with _db_scope(db) as handle:
        row = _row(handle, _tip(handle, sid))
        return _parse(row.get("outcome_json")) if row else None


def set_muted(sid: str, muted: bool, *, db: Any = None) -> dict[str, Any]:
    """Set the server-side mute flag on the whole compression lineage, then refresh (``trigger="mute"``).
    Raises ``ValueError`` for an unknown session."""
    with _db_scope(db) as handle:
        if not handle._set_lineage_column("muted", sid, int(bool(muted))):
            raise ValueError(f"unknown session {sid}")
        return refresh_outcome(sid, "mute", db=handle)


def refresh_outcomes_quietly(sids: Iterable[str], trigger: str, *, db: Any = None) -> int:
    """Call-site helper (turn end, reap, notices): refresh each id, log failures, never raise.
    Returns how many refreshed."""
    done = 0
    for sid in [s for s in dict.fromkeys(sids) if s]:
        try:
            refresh_outcome(sid, trigger, db=db)
            done += 1
        except Exception:
            logger.warning("session outcome refresh (%s) failed for %s", trigger, sid, exc_info=True)
    return done


def stamp_outcome_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """List rows: ``outcome`` (parsed, or None) and ``muted`` (bool) from the stored columns."""
    for row in rows:
        row["outcome"] = _parse(row.get("outcome_json"))
        row["muted"] = bool(row.get("muted"))
    return rows
