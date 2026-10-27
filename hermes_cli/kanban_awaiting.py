"""No producer, no park: every timed park records who makes its condition true. [daveey]

A park (``kanban_schedule``, ``kanban_block`` kind=time or a parent-less dependency,
``kanban_continue`` with ``resume_after_seconds``, and their CLI twins) used to be defined only
by its wake mechanism (clock, probe, parent). "Waiting on something only an agent can do, with
no agent assigned" was therefore legal and invisible: on 10-26/27 a hub card parked for 31 h on
an IAM grant "applied by the supervisor" while the MacBook held the admin rights, and another
hid a pending ask for David behind a wake probe
(``~/.hermes/kanban/evidence/orphan-waits-20261027/5whys.md``).

Each park now carries ``awaiting = {kind, ref, why}`` on its parking event:

* ``card``: ``ref`` is a task id (``gateway`` names another board's host). The card must exist
  (cross-gateway: ``confirm_remote`` runs ``hermes kanban wait-check`` before the write txn) and
  must not itself be an orphan.
* ``ci`` / ``deploy``: ``ref`` is a run / job / release id or URL.
* ``external``: ``ref`` names the vendor or ticket and ``why`` gives the evidence.
* ``clock``: a real window (a soak, a cooldown, a release slot).
* ``human``: refused. A wait on David is a visible ``blocked`` card with ``human_request``;
  an ask he already answered (ask-gate ledger) is never re-asked.

A park with no explicit ``awaiting`` is classified from its text: a pending step for a human is
refused (``human_wait``), and a wall only an agent action clears (grant, permission, access) is
refused (``agent_action_wait``) unless the producing card is declared. Anything else is recorded
as an inferred ``clock`` / ``external`` wait so the checker can tell legacy guesses from declared
producers. The third park in a row on one premise is not parked again: it becomes one
agent-owned owner card for a supervisor and the card waits on it (``repark_escalated``).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Optional

KINDS = ("card", "ci", "deploy", "external", "clock", "human")
AGENT_ACTION_WAIT = "agent_action_wait"
HUMAN_WAIT = "human_wait"
INVALID_AWAITING = "invalid_awaiting"
CODES = (AGENT_ACTION_WAIT, HUMAN_WAIT, INVALID_AWAITING)
# Parks in a row on one premise before the next one escalates instead (the 3rd park = 2nd re-park).
REPARK_LIMIT = 2
ESCALATION_PREFIX = "repark-escalation:"
ESCALATED_EVENT = "repark_escalated"
_TASK_ID = re.compile(r"\bt_[0-9a-f]{8,16}\b")
_RUN_ID = re.compile(r"\d{4,}|https?://\S+|\b[0-9a-f]{7,40}\b")
_TICKET = re.compile(r"\d{3,}|https?://\S+")
# A short continuation (pacing a long job) is not judged by its checkpoint prose.
CONTINUE_TEXT_GUARD_SECONDS = 2 * 3600
# A cross-gateway card confirmation (made before the write txn) stays valid this long.
REMOTE_CONFIRM_TTL = 300
_REMOTE_CONFIRMED: dict[tuple[str, str], tuple[float, str]] = {}

# Shapes from the 10-26/27 replays (session-idle-check HIDDEN_ASK / AGENT_WALL), narrowed so ordinary
# vendor/HTTP prose ("403 Forbidden", "AWS support to grant the quota") is not an agent wall.
HIDDEN_ASK = re.compile(
    r"\b(?:david|the user|a human)\b(?:['\u2019]s)?\W+(?:\w+\W+){0,6}?(?:decision|approval|answer|reply|confirmation|"
    r"go-ahead|sign-?off)\b|\bdon['\u2019]?t re-?ask\b|\balready asked (?:him|david|in chat)\b|"
    r"\bwaiting (?:on|for) (?:david|the user|a human)\b", re.I)
AGENT_WALL = re.compile(
    r"\bdenied\b|\bunauthorizedoperation\b|\baccess ?denied|\bnot authori[sz]ed to perform\b|"
    r"\b(?:missing|needs?|lacks?|without|insufficient)\s+(?:the\s+|a\s+)?(?:[\w:.*/-]+\s+){0,2}permissions?\b|"
    r"\bpermissions?\s+(?:denied|request|grant)|"
    r"\blacks? (?:the )?(?:access|role|scope|credential)|"
    r"\bneeds? (?:the |a )?(?:scoped )?(?:grant|access|credentials?|role)\b|"
    r"\bgrant\b[^.]{0,80}\bappl|\bsupervisor owns\b|\bsupervisor\b[^.;]{0,60}\bgrant|"
    r"\bapply the iam\b|\biam change\b", re.I)
_NEGATED = re.compile(r"\b(no|not|never|without|nor)\b")
_ANSWERED = re.compile(r"\b(recorded|made|given|answered|received|resolved|done|obtained|approved)\b")
# Only unambiguous citations of a decision already made; "once/after/with David's approval" is still pending.
_PAST_CITE = re.compile(r"\b(per|following|given|as)\s*$")
_WALL_CLEARED = re.compile(r"\b(fixed|resolved|earlier|previously|no longer|used to)\b|"
                           r"\b(?:was|were|been|got|is now)\s+(?:fixed|resolved|granted|applied|cleared|lifted)\b|"
                           r"\bno\b[^.;]{0,40}\b(needed|required)\b", re.I)
_STOPWORDS = frozenset("about after again already being before could david their there these those which while "
                       "would should waiting until where other parked approval decision status".split())


class AwaitingError(ValueError):
    """A park refused because nothing would ever produce its condition."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class RepeatedPark(Exception):
    """The park repeats its premise a third time; the caller escalates instead of parking."""

    def __init__(self, record: dict):
        super().__init__(ESCALATED_EVENT)
        self.record = record


_REMEDY = (
    "A wait needs a producer; agent-action conditions are work, not waits. Either (1) create the owner "
    "card on the gateway that holds the capability (kanban_create there, or `hermes kanban create` over "
    "ssh) and park with awaiting={kind:'card', ref:'<owner id>', gateway:'<gw>'}; or (2) kanban_block "
    "kind='assignment' so a capable supervisor is routed.")


# --- parsing ---------------------------------------------------------------------------------

def normalize(value: Any) -> Optional[dict]:
    """``awaiting`` from a tool/CLI argument (dict, JSON text, or ``kind:ref``), validated for shape."""
    if value is None or value == "" or value == {}:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{"):
            try:
                value = json.loads(text)
            except ValueError as exc:
                raise AwaitingError(INVALID_AWAITING, f"awaiting is not valid JSON: {exc}") from exc
        else:
            kind, _, ref = text.partition(":")
            value = {"kind": kind, "ref": ref}
    if not isinstance(value, dict):
        raise AwaitingError(INVALID_AWAITING, "awaiting must be an object {kind, ref, why}")
    kind = str(value.get("kind") or "").strip().lower()
    if kind not in KINDS:
        raise AwaitingError(INVALID_AWAITING, f"awaiting.kind must be one of {list(KINDS)} (got {kind!r})")
    out = {"kind": kind}
    for key in ("ref", "why", "gateway", "board"):
        raw = value.get(key)
        if raw is not None and not isinstance(raw, str):
            raise AwaitingError(INVALID_AWAITING, f"awaiting.{key} must be a string")
        if raw and raw.strip():
            out[key] = raw.strip()[:600]
    if kind == "card" and ":" in out.get("ref", "") and "gateway" not in out:
        gw, _, tid = out["ref"].rpartition(":")
        out["gateway"], out["ref"] = gw, tid
    if out.get("gateway") == "local":
        out.pop("gateway")
    return out


def premise_hash(awaiting: dict, reason: Optional[str]) -> str:
    """Stable id of what a park waits on: the declared producer (with its why), else the reason."""
    if awaiting.get("inferred"):
        basis = " ".join((reason or "").lower().split())[:400]
    else:
        basis = "|".join(awaiting.get(k, "") for k in ("kind", "gateway", "ref", "why"))
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


# --- prose ----------------------------------------------------------------------------------

def _hidden_ask(why: str) -> Optional[str]:
    for m in HIDDEN_ASK.finditer(why):
        before, after = why[max(0, m.start() - 24):m.start()].lower(), why[m.end():m.end() + 40].lower()
        if _NEGATED.search(before[-14:]) or _PAST_CITE.search(before) or _ANSWERED.search(after):
            continue
        return m.group(0)
    return None


def _agent_wall(why: str) -> Optional[str]:
    for clause in re.split(r"[.;\n]+", why):
        m = AGENT_WALL.search(clause)
        if m and not _WALL_CLEARED.search(clause):
            return m.group(0)
    return None


def text_wall(text: Optional[str]) -> Optional[tuple[str, str]]:
    """``(code, matched words)`` when park prose names a hidden human ask or an agent-only wall."""
    why = " ".join((text or "").split())
    ask = _hidden_ask(why)
    if ask:
        return HUMAN_WAIT, f"a pending step for a human ('{ask}')"
    wall = _agent_wall(why)
    if wall:
        return AGENT_ACTION_WAIT, f"a wall only an agent action clears ('{wall}')"
    return None


# --- producers -------------------------------------------------------------------------------

def _latest_park(conn: sqlite3.Connection, task_id: str) -> tuple[Optional[dict], str]:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind IN ('scheduled','continued') "
        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    try:
        payload = json.loads(row[0] or "{}") if row else {}
    except ValueError:
        payload = {}
    return payload.get("awaiting"), str(payload.get("reason") or "")


def card_orphan_reason(conn: sqlite3.Connection, task_id: str, *, _depth: int = 0) -> Optional[str]:
    """Why a local card cannot produce anything (None when it is a live producer)."""
    row = conn.execute("SELECT status, human_request, block_kind FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return f"{task_id} does not exist on this board"
    status = row[0]
    if status == "archived":
        return f"{task_id} is archived"
    if status == "triage":
        return f"{task_id} sits in triage, where nobody is working it"
    if status == "blocked" and not row[1] and row[2] not in ("assignment", "dependency"):
        return f"{task_id} is itself blocked with no human ask (kind={row[2]!r}), so nobody is producing it"
    if status == "scheduled":
        awaiting, reason = _latest_park(conn, task_id)
        if awaiting and awaiting.get("kind") == "card" and not awaiting.get("gateway") and _depth < 4:
            inner = card_orphan_reason(conn, awaiting.get("ref", ""), _depth=_depth + 1)
            return f"{task_id} waits on {awaiting.get('ref')}, and {inner}" if inner else None
        if not awaiting or awaiting.get("inferred"):
            wall = text_wall(reason)
            if wall:
                return f"{task_id} is itself parked on {wall[1]}"
    return None


def confirm_remote(awaiting: Optional[dict]) -> None:
    """Confirm a cross-gateway producer card exists, BEFORE any write txn (ssh can take 40 s).
    ``resolve`` then accepts the card from the process-local confirmation."""
    if not awaiting or awaiting.get("kind") != "card" or not awaiting.get("gateway"):
        return
    ref = awaiting.get("ref") or ""
    if not _TASK_ID.fullmatch(ref):
        return  # resolve reports the bad ref
    from hermes_cli import kanban_wait_check as kwait
    rc, out = kwait.check_remote(awaiting["gateway"], "task", ref, board=awaiting.get("board"))
    if rc not in (0, 1):
        raise AwaitingError(INVALID_AWAITING, f"cannot confirm producer card {ref} on {awaiting['gateway']}: "
                                              f"{out[-200:]}")
    status = out.split()[-1] if out.split() else ""
    if status in ("archived", "triage"):
        raise AwaitingError(AGENT_ACTION_WAIT, f"the producer card is not live: {ref} on {awaiting['gateway']} "
                                               f"is {status}, so nobody is producing it. {_REMEDY}")
    _REMOTE_CONFIRMED[(awaiting["gateway"], ref)] = (time.monotonic(), out[-120:])


def _check_card(conn: sqlite3.Connection, task_id: str, awaiting: dict, text: str) -> None:
    ref = awaiting.get("ref") or ""
    if not _TASK_ID.fullmatch(ref):
        raise AwaitingError(INVALID_AWAITING, f"awaiting kind='card' needs ref=<task id> (got {ref!r})")
    if ref == task_id:
        raise AwaitingError(INVALID_AWAITING, "a card cannot wait on itself")
    gateway = awaiting.get("gateway")
    if gateway:
        seen = _REMOTE_CONFIRMED.get((gateway, ref))
        if not seen or time.monotonic() - seen[0] > REMOTE_CONFIRM_TTL:
            raise AwaitingError(INVALID_AWAITING, (
                f"producer card {ref} on {gateway} was not confirmed; park through kanban_schedule/kanban_block "
                "or `hermes kanban schedule --awaiting`, which confirm it with `hermes kanban wait-check`."))
        awaiting["observed"] = seen[1]
        return
    why = card_orphan_reason(conn, ref)
    if why:
        raise AwaitingError(AGENT_ACTION_WAIT, f"the producer card is not live: {why}. {_REMEDY}")


def _check_run(conn, task_id, awaiting, text) -> None:
    if not _RUN_ID.search(awaiting.get("ref", "")):
        raise AwaitingError(INVALID_AWAITING,
                            f"awaiting kind={awaiting['kind']!r} needs ref=<run/job/release id or URL>")


def _check_external(conn, task_id, awaiting, text) -> None:
    if not awaiting.get("ref") or not awaiting.get("why"):
        raise AwaitingError(INVALID_AWAITING, "awaiting kind='external' needs ref=<vendor or ticket> and why=<evidence>")
    wall = text_wall(text)
    if wall and not _TICKET.search(awaiting["ref"]):
        raise wall_error(task_id, wall, text)


def _check_clock(conn, task_id, awaiting, text) -> None:
    wall = text_wall(text)
    if wall:
        raise wall_error(task_id, wall, text)


def _check_human(conn, task_id, awaiting, text) -> None:
    raise human_error(task_id, text)


_CHECKS: dict[str, Callable[[sqlite3.Connection, str, dict, str], None]] = {
    "card": _check_card, "ci": _check_run, "deploy": _check_run, "external": _check_external,
    "clock": _check_clock, "human": _check_human,
}


# --- ask-gate ledger --------------------------------------------------------------------------

def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9]{4,}", (text or "").lower()) if w not in _STOPWORDS}


def answered_ask(task_id: str, text: str) -> Optional[dict]:
    """The ask-gate ledger row (``state/ask-ledger.sqlite``) that already answers this ask: same card,
    or at least 3 distinctive words covering half the answered topic. Terminal (retired) answers
    count too: they are the permanent ones."""
    from hermes_constants import get_hermes_home
    path = Path(os.environ.get("ASK_GATE_LEDGER") or get_hermes_home() / "state" / "ask-ledger.sqlite")
    if not path.is_file():
        return None
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            rows = c.execute("SELECT topic, answer, card_id, ts FROM answered ORDER BY ts DESC LIMIT 500").fetchall()
        finally:
            c.close()
    except sqlite3.Error:
        return None
    toks = _tokens(text)
    for topic, answer, card_id, ts in rows:
        topic_toks = _tokens(topic or "")
        shared = toks & topic_toks
        if (card_id and card_id == task_id) or (len(shared) >= 3 and len(shared) * 2 >= len(topic_toks)):
            return {"topic": topic, "answer": answer, "card_id": card_id, "ts": ts}
    return None


def human_error(task_id: str, text: str) -> AwaitingError:
    hit = answered_ask(task_id, text)
    if hit:
        return AwaitingError(HUMAN_WAIT, (
            f"David already answered this ask ({(hit['topic'] or '')[:120]!r}: {(hit['answer'] or '')[:160]!r}). "
            "Act on that answer; never re-ask and never park on it."))
    return AwaitingError(HUMAN_WAIT, (
        "a wait on a human is not a park: nobody sees a scheduled card. Make it a visible ask with "
        "kanban_block(kind='needs_input', human_request={action, why_human, interaction, scope, evidence}), "
        "or drop the dependency on David if the agent can proceed."))


def wall_error(task_id: str, wall: tuple[str, str], text: str, *, named: Optional[str] = None) -> AwaitingError:
    if wall[0] == HUMAN_WAIT:
        return human_error(task_id, text)
    hint = (f" If {named} is the card doing it, declare it: awaiting={{kind:'card', ref:'{named}'}}." if named else "")
    return AwaitingError(AGENT_ACTION_WAIT, (
        f"this park waits on {wall[1]}, and no producing card is declared, so nothing would ever make it "
        f"true. {_REMEDY}{hint}"))


# --- the gate --------------------------------------------------------------------------------

def resolve(conn: sqlite3.Connection, task_id: str, *, awaiting: Any, reason: Optional[str],
            wake_check: Optional[str] = None, clock_only: bool = False, op: str = "schedule",
            wake_in: Optional[int] = None) -> dict:
    """The ``awaiting`` record for a park, or ``AwaitingError``. Read-only."""
    declared = normalize(awaiting)
    if declared is None:
        return _infer(conn, task_id, reason=reason, wake_check=wake_check, clock_only=clock_only,
                      op=op, wake_in=wake_in)
    text = " ".join(x for x in (reason, declared.get("why"), declared.get("ref")) if x)
    _CHECKS[declared["kind"]](conn, task_id, declared, text)
    return declared


def _live_card_ref(conn: sqlite3.Connection, task_id: str, text: str) -> Optional[str]:
    for ref in dict.fromkeys(_TASK_ID.findall(text or "")):
        if ref != task_id and card_orphan_reason(conn, ref) is None:
            return ref
    return None


def _infer(conn, task_id, *, reason, wake_check, clock_only, op, wake_in) -> dict:
    text = reason or ""
    producer = _live_card_ref(conn, task_id, f"{text} {wake_check or ''}")
    if not (op == "continue" and (wake_in or 0) <= CONTINUE_TEXT_GUARD_SECONDS):
        wall = text_wall(text)
        # A visible ask on the card waited on is not hidden; an agent wall needs a DECLARED owner card.
        if wall and not (wall[0] == HUMAN_WAIT and producer):
            raise wall_error(task_id, wall, text, named=producer)
    if producer:
        return {"kind": "card", "ref": producer, "inferred": True}
    if wake_check and not clock_only:
        return {"kind": "external", "ref": wake_check[:200], "inferred": True}
    return {"kind": "clock", "inferred": True}


def prior_same_premise(conn: sqlite3.Connection, task_id: str, premise: str) -> int:
    """Trailing parks of this card on ``premise``, newest first, stopping at the first other
    premise or at the last escalation (the supervisor acted since)."""
    count = 0
    for kind, payload in conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind IN ('scheduled','continued',?) "
            "ORDER BY id DESC LIMIT 20", (task_id, ESCALATED_EVENT)):
        if kind == ESCALATED_EVENT:
            break
        try:
            got = (json.loads(payload or "{}").get("awaiting") or {}).get("premise")
        except (ValueError, AttributeError):
            got = None
        if got is None:
            continue  # a continuation with no wait, or a legacy park
        if got != premise:
            break
        count += 1
    return count


def gate(conn: sqlite3.Connection, task_id: str, *, awaiting: Any, reason: Optional[str],
         wake_check: Optional[str] = None, clock_only: bool = False, op: str = "schedule",
         wake_in: Optional[int] = None) -> dict:
    """``resolve`` plus re-park escalation: the record to store on the parking event.

    Raises ``RepeatedPark`` on the third park in a row on one premise. Card waits (their producer
    is accountable) and inferred clock waits (pacing continues, operator drags) never escalate."""
    record = resolve(conn, task_id, awaiting=awaiting, reason=reason, wake_check=wake_check,
                     clock_only=clock_only, op=op, wake_in=wake_in)
    if record["kind"] == "card" or (record["kind"] == "clock" and record.get("inferred")):
        return record
    record["premise"] = premise_hash(record, reason)
    if prior_same_premise(conn, task_id, record["premise"]) >= REPARK_LIMIT:
        raise RepeatedPark(record)
    return record


# --- re-park escalation ------------------------------------------------------------------------

def _supervisor_profile(task) -> Optional[str]:
    """A capable supervisor other than the worker that keeps parking, or None (the card goes to triage)."""
    from hermes_cli.kanban_assignment_routing import _supervisor
    name, _required = _supervisor(task)
    return name


def escalate(conn: sqlite3.Connection, task_id: str, *, record: dict, reason: Optional[str],
             expected_run_id: Optional[int] = None) -> Optional[str]:
    """Turn a repeated park into one agent-owned issue for a supervisor; the card waits in ``todo``
    on it (a dependency with a producer). Never a park or a human ask. Caller holds the write txn.
    Returns the owner card id, or None when the card is no longer the caller's."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_wake_check as kwc

    task = kb.get_task(conn, task_id)
    if task is None:
        return None
    sql = ("UPDATE tasks SET status = 'todo', block_kind = NULL, human_request = NULL, claim_lock = NULL, "
           "claim_expires = NULL, worker_pid = NULL WHERE id = ? AND status IN ('running','ready','blocked','todo')")
    params: tuple = (task_id,)
    if expected_run_id is not None:
        sql += " AND current_run_id = ?"
        params = (task_id, int(expected_run_id))
    if conn.execute(sql, params).rowcount != 1:
        return None
    key = f"{ESCALATION_PREFIX}{task_id}:{record['premise']}"
    existing = conn.execute("SELECT id, status FROM tasks WHERE idempotency_key = ?", (key,)).fetchone()
    owner = existing[0] if existing and existing[1] not in ("done", "archived") else None
    if owner is None:
        supervisor = _supervisor_profile(task)
        owner = kb.create_task(
            conn, title=f"Unstick {task_id}: parked 3x on the same premise",
            body=(f"Card {task_id} ({task.title}) tried to park a third time on the same premise with no new "
                  f"evidence.\n\nPremise: {json.dumps({k: v for k, v in record.items() if k != 'premise'})}\n"
                  f"Latest reason: {(reason or '(none)')[:1500]}\n\n"
                  "This is agent-owned work, not a wait and not a question for David. Remove the cause: "
                  "do the action (grant, install, fix) where the capability lives, or route the card to the "
                  "profile/gateway that can, or prove the premise false. Then complete this card with the "
                  f"evidence; {task_id} waits on it and resumes when it is done."),
            assignee=supervisor, created_by=task.assignee, workspace_kind="scratch", triage=supervisor is None,
            idempotency_key=None if existing else key, creator_task_id=task.id, session_id=task.session_id,
            tenant=task.tenant, priority=task.priority,
        )
    kb._link(conn, owner, task_id)
    run_id = kb._end_or_synthesize_run(conn, task_id, outcome="blocked", status="blocked",
                                       summary=f"{ESCALATED_EVENT} -> {owner}", synthesize=True)
    kb._append_event(conn, task_id, ESCALATED_EVENT, {
        "owner_task_id": owner, "awaiting": record, "reason": reason}, run_id=run_id)
    kwc.set_check(conn, task_id, None, None)
    return owner


def card_probe(awaiting: Optional[dict]) -> Optional[str]:
    """The wake probe for a card wait that brought none: ``hermes kanban wait-check task``."""
    if not awaiting or awaiting.get("kind") != "card" or not _TASK_ID.fullmatch(awaiting.get("ref") or ""):
        return None
    argv = ["hermes", "kanban", "wait-check", "task", awaiting["ref"]]
    if awaiting.get("gateway"):
        argv += ["--gateway", awaiting["gateway"]]
    if awaiting.get("board"):
        argv += ["--board", awaiting["board"]]
    return shlex.join(argv)


def escalated_owner(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """The owner card when this card's latest event is a re-park escalation."""
    row = conn.execute("SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                       (task_id,)).fetchone()
    if not row or row[0] != ESCALATED_EVENT:
        return None
    try:
        return json.loads(row[1] or "{}").get("owner_task_id")
    except ValueError:
        return None


def escalation_message(task_id: str, owner: Optional[str]) -> str:
    return (f"{ESCALATED_EVENT}: {task_id} parked on the same premise {REPARK_LIMIT + 1} times with no new "
            f"evidence, so it was not parked again. It now waits on owner card {owner}, which must remove "
            "the cause (with no capable supervisor configured it lands in triage for the orchestrator). "
            "End your turn.")
