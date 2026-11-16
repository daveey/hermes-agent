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
* ``ci`` / ``deploy``: ``ref`` is a run / job / release id or URL. A GitHub Actions ci run with a job queued
  over 15 min and no runner is refused (``ci_run_unstartable``), and a card parked on one is woken
  (``kanban_ci_progress``).
* ``external``: ``ref`` names the vendor or ticket and ``why`` gives the evidence.
* ``clock``: a real cooldown or release slot. Observation-window parks are refused (``soak_wait``):
  check now and report the actual elapsed window. Quota resets, short continuation pacing (<=2 h),
  and an explicit ``soak_requested_by: David — "<his words>"`` line on the card are exempt.
* ``resource``: an internal resource gate (a governor's pool or host-pressure hold) whose own
  ``wake_check`` passes once the reading drops below its threshold. It needs that probe, never
  escalates into an "Unstick" owner card (nobody can act on a transient load reading), and a
  repeat park on the same gate with no new evidence backs its probe cadence off and says so in
  the reason (``resource_apply``) instead. Only an operator process (the gate) may declare one, never
  a card's own worker. Its ``wake_at`` deadline still wakes the card: the gate then re-checks
  admission and re-parks it (a counted repeat) if the hold stands.
* ``human``: refused. A wait on David is a visible ``blocked`` card with ``human_request``;
  an ask he already answered (ask-gate ledger) is never re-asked.

A park with no explicit ``awaiting`` is classified from its text: a pending step for a human is
refused (``human_wait``), and a wall only an agent action clears (grant, permission, access) is
refused (``agent_action_wait``) unless the producing card is declared. A card's own worker parking it
on a wake check that names no card or CI run (an opaque probe: "python3 -c ...", "test -f ...") is
refused too
(``opaque_probe_wait``): a probe proves only that something polls, which is what the read side
(``kanban_wait_info``) already reports as a stall; on 10-27 a card waited 9 h on a KeepAlive browser
nobody would stop. Short continues (<= 2 h, pacing a job) are exempt. Anything else is recorded
as an inferred ``clock`` / ``external`` wait so the checker can tell legacy guesses from declared
producers. Inference never names the card's own children or descendants: a parent parked on its
child cycles (the child stays todo until the parent finishes, and the checker walks that edge
back). A declared producer or wake probe that would cycle the same way is refused at schedule
time. The third park in a row on one premise is not parked again: it becomes one
agent-owned owner card for a supervisor and the card waits on it (``repark_escalated``). A park
on the same opaque probe counts as the same premise however its reason is worded.
"""
from __future__ import annotations

from hermes_cli import kanban_lockguard as _kanban_lockguard
import hashlib
import json
import os
import re
import shlex
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Optional

KINDS = ("card", "ci", "deploy", "external", "clock", "human", "resource")
RESOURCE = "resource"
# A repeat resource park doubles its probe cadence per park in a row on the same gate, up to this.
RESOURCE_BACKOFF_MAX_SECONDS = 3600
AGENT_ACTION_WAIT = "agent_action_wait"
HUMAN_WAIT = "human_wait"
INVALID_AWAITING = "invalid_awaiting"
OPAQUE_PROBE_WAIT = "opaque_probe_wait"
# The declared producer card is already done: what the park waits for has been produced (it would wake at once).
PRODUCER_DONE = "producer_done"
# The CI run the park names has a job queued past the limit with no runner (``kanban_ci_progress``).
CI_RUN_UNSTARTABLE = "ci_run_unstartable"
SOAK_WAIT = "soak_wait"
CODES = (AGENT_ACTION_WAIT, HUMAN_WAIT, INVALID_AWAITING, OPAQUE_PROBE_WAIT, PRODUCER_DONE, CI_RUN_UNSTARTABLE,
         SOAK_WAIT)
# Parks in a row on one premise before the next one escalates instead (the 3rd park = 2nd re-park).
REPARK_LIMIT = 2
# Fires of one opaque probe, each followed by a re-park on it, before the next park escalates: a probe
# that keeps firing while the worker's own check disagrees is broken, not a natural window.
PROBE_FIRE_REPARK_LIMIT = 6
# Premise-bearing rows (fires, parks with a wait) scanned for those cycles: 2 per cycle plus slack.
# Premise-less rows (pacing continues, legacy parks) do not count against it, only against the raw cap.
PROBE_FIRE_WINDOW = 4 * PROBE_FIRE_REPARK_LIMIT
PROBE_FIRE_SCAN_MAX = 10 * PROBE_FIRE_WINDOW
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
# 10-28: also the merge queue's own wording ("needs daveey's approving review (or his OK on Discord...)").
HIDDEN_ASK = re.compile(
    r"\b(?:david|daveey|the user|a human)\b(?:['\u2019]s)?\W+(?:\w+\W+){0,6}?(?:decision|approval|approving review|"
    r"answer|reply|confirmation|go-ahead|sign-?off)\b|\bhis ok\b|\bdon['\u2019]?t re-?ask\b|"
    r"\balready asked (?:him|david|daveey|in chat)\b|"
    r"\bwaiting (?:on|for) (?:david|daveey|the user|a human)\b", re.IGNORECASE)
AGENT_WALL = re.compile(
    r"\bdenied\b|\bunauthorizedoperation\b|\baccess ?denied|\bnot authori[sz]ed to perform\b|"
    r"\b(?:missing|needs?|lacks?|without|insufficient)\s+(?:the\s+|a\s+)?(?:[\w:.*/-]+\s+){0,2}permissions?\b|"
    r"\bpermissions?\s+(?:denied|request|grant)|"
    r"\blacks? (?:the )?(?:access|role|scope|credential)|"
    r"\bneeds? (?:the |a )?(?:scoped )?(?:grant|access|credentials?|role)\b|"
    r"\bgrant\b[^.]{0,80}\bappl|\bsupervisor owns\b|\bsupervisor\b[^.;]{0,60}\bgrant|"
    r"\bapply the iam\b|\biam change\b", re.IGNORECASE)
_NEGATED = re.compile(r"\b(no|not|never|without|nor)\b")
_ANSWERED = re.compile(r"\b(recorded|made|given|answered|received|resolved|done|obtained|approved)\b")
# Only unambiguous citations of a decision already made; "once/after/with David's approval" is still pending.
_PAST_CITE = re.compile(r"\b(per|following|given|as)\s*$")
_WALL_CLEARED = re.compile(r"\b(fixed|resolved|earlier|previously|no longer|used to)\b|"
                           r"\b(?:was|were|been|got|is now)\s+(?:fixed|resolved|granted|applied|cleared|lifted)\b|"
                           r"\bno\b[^.;]{0,40}\b(needed|required)\b", re.IGNORECASE)
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
    "ssh) and park with awaiting={kind:'card', ref:'<owner id>', gateway:'<gw>'}; (2) kanban_block "
    "needs=[...] when another gateway holds the access; or (3) kanban_supervisor_request for archive, "
    "unlink, resume or retry_limit on your creator tree. kanban_block kind='assignment' routes only when "
    "a different profile is configured (kanban.orchestrator_profile or a creator other than the assignee).")


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
    if value.get("auto_card_probe") is True:
        out["auto_card_probe"] = True
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


_FULL_SHA = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{40}(?![0-9A-Fa-f])")


def run_heads(awaiting: dict, wake_check: Optional[str]) -> list[str]:
    """Sorted full (40-hex) commit SHAs a ``ci``/``deploy`` park names in its ``ref`` or ``wake_check``."""
    text = f"{awaiting.get('ref', '')} {wake_check or ''}"
    return sorted({m.lower() for m in _FULL_SHA.findall(text)})


def premise_hash(awaiting: dict, reason: Optional[str], wake_check: Optional[str] = None) -> str:
    """Stable id of what a park waits on: the declared producer (with its why), else the reason.

    A ``ci``/``deploy`` park also hashes the full commit SHAs its ``ref``/``wake_check`` name: red CI, a fix
    pushed as a new head and a park on that head is new evidence, not the same premise. A PR-URL ref with
    no per-head ``why`` folded every head into one premise and escalated the third fix round (PR #1806,
    t_01a1d19e0179a093). The same head re-parked still counts, so a stuck head escalates. [daveey]"""
    if awaiting.get("inferred"):
        basis = " ".join((reason or "").lower().split())[:400]
    elif awaiting.get("kind") == RESOURCE:
        # The gate, not its reading: "pool at 87%" and "pool at 88%" are one premise with no new evidence.
        basis = "|".join(awaiting.get(k, "") for k in ("kind", "gateway", "ref"))
    else:
        basis = "|".join(awaiting.get(k, "") for k in ("kind", "gateway", "ref", "why"))
        heads = run_heads(awaiting, wake_check) if awaiting.get("kind") in ("ci", "deploy") else []
        if heads:  # SHA-less parks keep their old premise
            basis += "|heads:" + ",".join(heads)
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


def _descendant_ids(conn: sqlite3.Connection, task_id: str) -> set[str]:
    """Child ids reachable from ``task_id`` (the card cannot wait on these)."""
    seen: set[str] = set()
    stack = [task_id]
    while stack:
        node = stack.pop()
        for (cid,) in conn.execute(
                "SELECT child_id FROM task_links WHERE parent_id = ?", (node,)):
            if cid and cid not in seen:
                seen.add(cid)
                stack.append(cid)
    return seen


def _scheduled_probe_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    """Task ids named by the latest park's wake probe, if that park is still the wait."""
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind IN ('scheduled','continued') "
        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    if not row:
        return []
    try:
        payload = json.loads(row[0] or "{}")
    except ValueError:
        return []
    probe = payload.get("wake_check")
    if isinstance(probe, dict):
        probe = probe.get("cmd") or ""
    return _TASK_ID.findall(probe or "")


def dependency_cycle_reason(conn: sqlite3.Connection, task_id: str, ref: str) -> Optional[str]:
    """Why parking ``task_id`` on ``ref`` would cycle, or None.

    Own children and deeper descendants always cycle: they stay gated until this card
    finishes, and the idle checker walks that parent edge back. A card that is not a
    descendant still cycles when the edges the checker walks (todo parents, a scheduled
    card-await, a wake probe's task ids) lead back here.
    """
    if not ref or not _TASK_ID.fullmatch(str(ref)):
        return None
    if ref == task_id:
        return f"{task_id} cannot wait on itself"
    if ref in _descendant_ids(conn, task_id):
        return (f"{ref} is a descendant of {task_id}; a card must not wait on its own child "
                "(the child cannot finish until this card does, so the wait never ends)")
    from hermes_cli.kanban_awaiting_board import cross_board_cycle
    return cross_board_cycle(conn, task_id, {"kind": "card", "ref": ref})


def park_cycle_error(conn: sqlite3.Connection, task_id: str, record: Optional[dict],
                     wake_check: Optional[str]) -> Optional[str]:
    """Refusal text when this park's producer or wake probe would cycle, else None."""
    from hermes_cli.kanban_awaiting_board import other_board_path
    refs: list[str] = []
    foreign_ref = record.get("ref") if record and other_board_path(conn, record) else None
    if isinstance(record, dict) and record.get("kind") == "card" and not record.get("gateway") and not foreign_ref:
        ref = record.get("ref") or ""
        if _TASK_ID.fullmatch(ref):
            refs.append(ref)
    refs.extend(ref for ref in _TASK_ID.findall(wake_check or "") if ref != foreign_ref)
    for ref in dict.fromkeys(refs):
        if ref == task_id:
            continue
        why = dependency_cycle_reason(conn, task_id, ref)
        if why:
            return why
    return None


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
            from hermes_cli.kanban_awaiting_board import producer_connection
            try:
                with producer_connection(conn, awaiting) as owner:
                    inner = card_orphan_reason(owner, awaiting.get("ref", ""), _depth=_depth + 1)
            except (AwaitingError, sqlite3.Error) as exc:
                return f"{task_id} waits on an unreadable producer board: {exc}"
            return f"{task_id} waits on {awaiting.get('ref')}, and {inner}" if inner else None
        if not awaiting or awaiting.get("inferred"):
            wall = text_wall(reason)
            if wall:
                return f"{task_id} is itself parked on {wall[1]}"
    return None


def producer_done_error(ref: str, completed: str) -> AwaitingError:
    return AwaitingError(PRODUCER_DONE, (
        f"the producer card {ref} is already done (completed {completed}): what you wait for has been produced. "
        f"Read its result (kanban_show {ref}) and act on it now, or name what you are still waiting on."))


def _completed_at(conn: sqlite3.Connection, ref: str) -> Optional[str]:
    """``completed_at`` (UTC ISO) when ``ref`` is a done card on this board, else None."""
    row = conn.execute("SELECT status, completed_at FROM tasks WHERE id = ?", (ref,)).fetchone()
    if not row or row[0] != "done":
        return None
    if not row[1]:
        return "at an unrecorded time"
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(row[1])))


def confirm_remote(awaiting: Optional[dict], *, hint: str = "") -> None:
    """Confirm a cross-gateway producer card exists, BEFORE any write txn (ssh can take 40 s).
    ``resolve`` then accepts the card from the process-local confirmation. A CI wait's run is read from GitHub
    here too (``kanban_ci_progress``: refused when it is not starting); ``hint`` is the park's wake check and
    reason, which may name the repo of a bare run id."""
    if awaiting and awaiting.get("kind") == "ci":
        from hermes_cli import kanban_ci_progress
        kanban_ci_progress.confirm(awaiting, hint=hint)
        return
    if not awaiting or awaiting.get("kind") != "card" or not awaiting.get("gateway"):
        return
    ref = awaiting.get("ref") or ""
    if not _TASK_ID.fullmatch(ref):
        return  # resolve reports the bad ref
    from hermes_cli import kanban_wait_check as kwait
    rc, out = kwait.check_remote(awaiting["gateway"], "task", ref, board=awaiting.get("board"))
    # rc 3: the gateway is offline, but the card is in its last replica. Accept it on that observation (``observed``
    # keeps the last-seen status and the staleness), so a card can still park on a card of a sleeping Mac.
    if rc not in (0, 1, 3):
        raise AwaitingError(INVALID_AWAITING, f"cannot confirm producer card {ref} on {awaiting['gateway']}: "
                                              f"{out[-200:]}")
    words = out.split()
    status = words[1] if len(words) >= 2 and words[0] == ref else ""
    if status in ("archived", "triage") or "orphan producer" in out or "agent-owned park:" in out:
        raise AwaitingError(AGENT_ACTION_WAIT, f"the producer card is not live: {ref} on {awaiting['gateway']} "
                                               f"is {status}, so nobody is producing it. {_REMEDY}")
    if status == "done":
        # Its auto probe (``wait-check task``) would exit 0 on the first run: the park wakes at once. Other rc-0
        # statuses (review, blocked on David) stay parkable: a tracker's re-park probe reads them as handled.
        raise producer_done_error(ref, f"per {awaiting['gateway']}'s board")
    _REMOTE_CONFIRMED[(awaiting["gateway"], ref)] = (time.monotonic(), out[-240:] if rc == 3 else out[-120:])


def _check_card(conn: sqlite3.Connection, task_id: str, awaiting: dict, text: str) -> None:
    ref = awaiting.get("ref") or ""
    if ref.startswith("capability-owner:"):
        # A cross-gateway capability owner card, named by its hermes-hub request id until the holder
        # applies it (kanban_capability_routing). Live while the request is pending or the card runs.
        from hermes_cli.kanban_capability_routing import owner_status
        rc, out = owner_status(ref, conn)  # this txn's conn: a relay's route event is not committed yet
        if rc == 2:
            raise AwaitingError(INVALID_AWAITING, f"capability owner request is not live: {out[-200:]}")
        awaiting["observed"] = out[-120:]
        return
    if not _TASK_ID.fullmatch(ref):
        raise AwaitingError(INVALID_AWAITING, f"awaiting kind='card' needs ref=<task id> (got {ref!r})")
    from hermes_cli.kanban_awaiting_board import other_board_path, producer_connection, cross_board_cycle
    other_board = other_board_path(conn, awaiting)
    if other_board:
        why = cross_board_cycle(conn, task_id, awaiting)
        if why:
            raise AwaitingError(INVALID_AWAITING, why)
    if ref == task_id and not other_board:
        raise AwaitingError(INVALID_AWAITING, "a card cannot wait on itself")
    gateway = awaiting.get("gateway")
    if not gateway and not other_board:
        why = dependency_cycle_reason(conn, task_id, ref)
        if why:
            raise AwaitingError(INVALID_AWAITING, why)
    if gateway:
        seen = _REMOTE_CONFIRMED.get((gateway, ref))
        if not seen or time.monotonic() - seen[0] > REMOTE_CONFIRM_TTL:
            raise AwaitingError(INVALID_AWAITING, (
                f"producer card {ref} on {gateway} was not confirmed; park through kanban_schedule/kanban_block "
                "or `hermes kanban schedule --awaiting`, which confirm it with `hermes kanban wait-check`."))
        awaiting["observed"] = seen[1]
        return
    with producer_connection(conn, awaiting) as owner:
        why = card_orphan_reason(owner, ref)
        if why:
            raise AwaitingError(AGENT_ACTION_WAIT, f"the producer card is not live: {why}. {_REMEDY}")
        done = _completed_at(owner, ref)
        if done:
            raise producer_done_error(ref, done)
        row = owner.execute("SELECT status FROM tasks WHERE id = ?", (ref,)).fetchone()
        awaiting["observed"] = f"{ref} {row[0]}"


def _check_run(conn, task_id, awaiting, text) -> None:
    if not _RUN_ID.search(awaiting.get("ref", "")):
        raise AwaitingError(INVALID_AWAITING,
                            f"awaiting kind={awaiting['kind']!r} needs ref=<run/job/release id or URL>")


def _check_external(conn, task_id, awaiting, text) -> None:
    if not awaiting.get("ref") or not awaiting.get("why"):
        raise AwaitingError(INVALID_AWAITING, "awaiting kind='external' needs ref=<vendor or ticket> and why=<evidence>")
    wall = text_wall(text)
    # A vendor ticket/URL in the ref names who clears an agent-looking wall (AWS support grants the
    # quota). It never excuses a wait on David: "PR 1777 approval" has a number, and the board then
    # showed a generic scheduled wait instead of a wait on him (hub t_b1fdca0b, 10-28).
    if wall and (wall[0] == HUMAN_WAIT or not _TICKET.search(awaiting["ref"])):
        raise wall_error(task_id, wall, text)


def _check_clock(conn, task_id, awaiting, text) -> None:
    wall = text_wall(text)
    if wall:
        raise wall_error(task_id, wall, text)


def _check_human(conn, task_id, awaiting, text) -> None:
    raise human_error(task_id, text)


def _check_resource(conn, task_id, awaiting, text) -> None:
    if not awaiting.get("ref") or not awaiting.get("why"):
        raise AwaitingError(INVALID_AWAITING, "awaiting kind='resource' needs ref=<gate and resource, e.g. "
                                              "'bb-gate governor: pool'> and why=<the reading and threshold>")
    if parked_by_own_worker(task_id):
        # Only an admission gate (an operator process such as bb_gate) declares one: it is exempt from re-park
        # escalation, so a worker naming its own wall a "resource" would hide a stuck premise.
        raise AwaitingError(INVALID_AWAITING, (
            "awaiting kind='resource' is for an admission gate parking a card behind its own hold, not a worker "
            "parking itself; name the producer (card, ci, deploy, external, clock) instead"))
    wall = text_wall(text)
    if wall:
        raise wall_error(task_id, wall, text)


_CHECKS: dict[str, Callable[[sqlite3.Connection, str, dict, str], None]] = {
    "card": _check_card, "ci": _check_run, "deploy": _check_run, "external": _check_external,
    "clock": _check_clock, "human": _check_human, "resource": _check_resource,
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
        c = _kanban_lockguard.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
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
        "or drop the dependency on David if the agent can proceed. If the ask already exists elsewhere (a "
        "pending approval already delivered to Discord, Slack or an inbox, a PR review request), still block "
        "this card that way so the board shows the wait on David: name that approval/PR in action and put a "
        "`Checked: <command> - <its pending state>` line in evidence. ask-gate's "
        "approval_reconciliation.preflight reads the approval store through the adapter registered for it (if "
        "any): pending lets the block through as the visible wait (the block files no new approval request), "
        "approved means reuse the "
        "recorded approval, denied means stop. Never file a second approval request for the same scope."))


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
    # [daveey] A probe that matches comment authors misses relayed replies (hub:gw:<gw>): author_probe.
    from hermes_cli.kanban_comment_origin import check_park
    check_park(conn, task_id, wake_check)
    declared = normalize(awaiting)
    from hermes_cli import kanban_soak_wait
    if declared is None:
        inferred = _infer(conn, task_id, reason=reason, wake_check=wake_check, clock_only=clock_only,
                          op=op, wake_in=wake_in)
        kanban_soak_wait.check(conn, task_id, inferred, reason, short_continue=_short_continue(op, wake_in))
        return inferred
    kanban_soak_wait.check(conn, task_id, declared, reason, short_continue=_short_continue(op, wake_in))
    text = " ".join(x for x in (reason, declared.get("why"), declared.get("ref")) if x)
    if declared["kind"] == RESOURCE and not (wake_check and str(wake_check).strip()):
        raise AwaitingError(INVALID_AWAITING, (
            "awaiting kind='resource' needs a wake_check that passes once the resource drops below its threshold: "
            "that probe is what resumes the card"))
    _CHECKS[declared["kind"]](conn, task_id, declared, text)
    return declared


def _live_card_ref(conn: sqlite3.Connection, task_id: str, text: str) -> Optional[str]:
    for ref in dict.fromkeys(_TASK_ID.findall(text or "")):
        if ref == task_id or dependency_cycle_reason(conn, task_id, ref):
            continue  # self, own child/descendant, or a card that already waits back
        if _completed_at(conn, ref):
            continue  # a finished card produces nothing later; named as background, it would wake the park at once
        if card_orphan_reason(conn, ref) is None:
            return ref
    return None


def _carries_visible_ask(conn: sqlite3.Connection, task_id: str) -> bool:
    """True when the card is itself blocked on a human with a visible ask (human_request or needs_input)."""
    row = conn.execute("SELECT status, human_request, block_kind FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return bool(row) and row[0] in ("blocked", "triage") and (bool(row[1]) or row[2] == "needs_input")


def _short_continue(op: str, wake_in: Optional[int]) -> bool:
    return op == "continue" and (wake_in or 0) <= CONTINUE_TEXT_GUARD_SECONDS


def opaque_probe(wake_check: Optional[str]) -> Optional[str]:
    """The probe's name when a wake check names no card or CI run, else None. Same classifier as the
    read side (``kanban_wait_info``), so the gate refuses exactly what the checker would call a stall."""
    from hermes_cli.kanban_wait_info import producer_from_wake_check
    found = producer_from_wake_check(wake_check)
    return found[1] if found and found[0] == "probe" else None


def parked_by_own_worker(task_id: str) -> bool:
    """True when the card's own worker parks it (tool or CLI inside the worker process, which carries
    ``HERMES_KANBAN_TASK``). An operator on the dashboard or a terminal is accountable for the probe it
    sets; a worker parking itself on one is the 10-27 orphan shape."""
    return bool(task_id) and os.environ.get("HERMES_KANBAN_TASK") == task_id


def opaque_probe_error(probe: str) -> AwaitingError:
    return AwaitingError(OPAQUE_PROBE_WAIT, (
        f"this park waits on probe {probe!r} and names no producer. A probe proves only that something "
        "polls, not that anything will ever make it pass. Say who changes that state: "
        "awaiting={kind:'external', ref:'<who or what changes it>', why:'<evidence it will>'} (or "
        "kind 'card'/'ci'/'deploy' when a card or run produces it). If only an action changes it (stop a "
        "service, free a resource, retire tabs), that is work, not a wait: do it now, or create the owner "
        "card and park with awaiting={kind:'card', ref:'<owner id>'}. If it needs David, "
        "kanban_block(kind='needs_input', human_request={...}) instead."))


def _infer(conn, task_id, *, reason, wake_check, clock_only, op, wake_in) -> dict:
    text = reason or ""
    producer = _live_card_ref(conn, task_id, f"{text} {wake_check or ''}")
    short = _short_continue(op, wake_in)
    if not short:
        wall = text_wall(text)
        # A visible ask on the card waited on is not hidden; an agent wall needs a DECLARED owner card.
        # A merely live card (running, scheduled) does not carry David's ask, so it excuses nothing.
        if wall and not (wall[0] == HUMAN_WAIT and producer and _carries_visible_ask(conn, producer)):
            raise wall_error(task_id, wall, text, named=producer)
    if producer:
        return {"kind": "card", "ref": producer, "inferred": True}
    if wake_check and not clock_only:
        probe = None if short or not parked_by_own_worker(task_id) else opaque_probe(wake_check)
        if probe:
            raise opaque_probe_error(probe)
        return {"kind": "external", "ref": probe_ref(wake_check), "inferred": True}
    return {"kind": "clock", "inferred": True}


_INTERPRETER = re.compile(r"(?:python[\d.]*|bash|sh|zsh|node|ruby|perl|uv|nice|nohup|timeout|exec)$")


def probe_ref(wake_check: str, limit: int = 200) -> str:
    """A readable name for an inferred probe wait: the script basename plus its args.

    ``wake_check[:200]`` cut long absolute paths mid-word (``.../hosts/daveey-min``), which read as a
    host process that does not exist (bb t_01a19f806a228604). Leading ``env``, ``VAR=val`` and
    interpreter words are dropped; an unparseable or interpreter-only command keeps the old prefix."""
    try:
        words = shlex.split(wake_check)
    except ValueError:
        return wake_check[:limit]
    i = 0
    while i < len(words) and (words[i] == "env" or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[i])
                              or _INTERPRETER.match(os.path.basename(words[i])) or words[i].startswith("-")
                              and i and _INTERPRETER.match(os.path.basename(words[i - 1]))):
        i += 1
    if i >= len(words) or "/" not in words[i]:
        return wake_check[:limit]
    return shlex.join([os.path.basename(words[i]), *words[i + 1:]])[:limit]


# A declared clock window that states when it ends (an ISO-8601 time in ``ref``, e.g. a quota
# reset) is not a stuck premise until that time passes: re-parks inside it never escalate.
_ISO_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})")
CLOCK_WINDOW_MAX_SECONDS = 14 * 86400


def clock_window_end(record: dict, now: float) -> Optional[float]:
    """End of a declared ``clock`` wait whose ref names a future time (at most 14 days out), else None."""
    if record.get("kind") != "clock" or record.get("inferred"):
        return None
    from datetime import datetime
    for m in _ISO_TIME.finditer(record.get("ref") or ""):
        try:
            end = datetime.fromisoformat(m.group(0)).timestamp()
        except ValueError:
            continue
        if now < end <= now + CLOCK_WINDOW_MAX_SECONDS:
            return end
    return None


def probe_premise(wake_check: Optional[str], *, op: str = "schedule", wake_in: Optional[int] = None) -> Optional[str]:
    """Premise key of an opaque probe (whitespace-normalised command), so re-parks on one probe count
    as one premise whatever the reason says. None for card/CI probes and short pacing continues."""
    if _short_continue(op, wake_in) or not opaque_probe(wake_check):
        return None
    return _probe_key(wake_check)


def _probe_key(cmd: Any) -> str:
    return hashlib.sha256(("probe|" + " ".join(str(cmd).split())).encode()).hexdigest()[:16]


def prior_same_premise(conn: sqlite3.Connection, task_id: str, premise: str, *,
                       probe: Optional[str] = None) -> int:
    """Trailing parks of this card on ``premise`` (or on the same opaque ``probe``), newest first,
    stopping at the first other premise or at the last escalation (the supervisor acted since).
    A deploy park behind a healthy core update window (``kanban_deploy_window``) is not counted.

    A ``condition_met`` wake by this same probe also ends the run: the probe is live and its condition
    really held (two confirming exit-0 runs), so a park after it waits on a natural window that closed
    again (an idle Mac whose user came back), which is new evidence, not a stuck premise. Escalating it
    froze t_f6d930b7 behind owner cards nothing could run and dropped the working idle trigger. [daveey]"""
    from hermes_cli.kanban_deploy_window import is_healthy
    from hermes_cli.kanban_wake_check import CONDITION_MET_EVENT
    from hermes_cli.kanban_shared_blockers import CLEARED_EVENT
    count = 0
    for kind, payload in conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? AND kind IN ('scheduled','continued',?,?,?) "
            "ORDER BY id DESC LIMIT 20", (task_id, ESCALATED_EVENT, CONDITION_MET_EVENT, CLEARED_EVENT)):
        if kind == ESCALATED_EVENT:
            break
        try:
            data = json.loads(payload or "{}")
            got = data.get("awaiting") or {}
        except (ValueError, AttributeError):
            data, got = {}, {}
        if kind == CLEARED_EVENT:
            # Only a real wake interrupts the premise streak; hold-retirement annotations do not. ``woke``
            # covers the blocked branch (no status) and a parent-gated ``todo`` wake; status is the legacy form.
            if data.get("woke") is True or data.get("status") in ("ready", "review", "todo"):
                break
            continue
        if kind == CONDITION_MET_EVENT:
            if probe and isinstance(data, dict) and data.get("wake_check") and _probe_key(data["wake_check"]) == probe:
                break
            continue
        if not isinstance(got, dict) or got.get("premise") is None:
            continue  # a continuation with no wait, or a legacy park
        if got["premise"] != premise and not (probe and got.get("probe") == probe):
            break
        if not is_healthy(got):
            count += 1
    return count


def probe_fire_reparks(conn: sqlite3.Connection, task_id: str, premise: str, probe: Optional[str]) -> int:
    """Trailing fire->re-park cycles on one opaque ``probe``: ``condition_met`` wakes by that probe, each
    followed by a re-park on the same premise/probe, newest first. Stops at an escalation, a park on
    another premise, another probe's fire or any other card progress (``kanban_no_progress``).

    ``prior_same_premise`` ends its run at each such fire (a natural window that closed again), so a probe
    that is looser than the worker's own check would otherwise cycle forever, one worker run per fire.

    Premise-less rows (inferred-clock pacing continues, legacy parks) are skipped without using up
    ``PROBE_FIRE_WINDOW``: counting them let a few pacing continues per cycle push the 6th fire out of
    the window, so the cap silently never tripped. Only ``PROBE_FIRE_SCAN_MAX`` bounds the raw read.
    A human comment does not reset the count, unlike ``kanban_no_progress``: a note is not a probe fix,
    and only event kinds in ``CARD_PROGRESS_EVENTS`` are read here. [daveey]"""
    if not probe:
        return 0
    from hermes_cli.kanban_no_progress import CARD_PROGRESS_EVENTS
    from hermes_cli.kanban_wake_check import CONDITION_MET_EVENT
    other = tuple(k for k in CARD_PROGRESS_EVENTS if k != CONDITION_MET_EVENT)
    kinds = ("scheduled", "continued", ESCALATED_EVENT, CONDITION_MET_EVENT, *other)
    fires = seen = 0
    for kind, payload in conn.execute(
            f"SELECT kind, payload FROM task_events WHERE task_id = ? AND kind IN ({','.join('?' * len(kinds))}) "
            f"ORDER BY id DESC LIMIT {PROBE_FIRE_SCAN_MAX}", (task_id, *kinds)):
        if kind in other or kind == ESCALATED_EVENT:
            break
        try:
            data = json.loads(payload or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if kind == CONDITION_MET_EVENT:
            if not (data.get("wake_check") and _probe_key(data["wake_check"]) == probe):
                break
            fires += 1
        else:
            got = data.get("awaiting") or {}
            if not isinstance(got, dict) or got.get("premise") is None:
                continue  # a continuation with no wait, or a legacy park: not in the window
            if got["premise"] != premise and got.get("probe") != probe:
                break
        seen += 1
        if seen >= PROBE_FIRE_WINDOW:
            break
    return fires


def gate(conn: sqlite3.Connection, task_id: str, *, awaiting: Any, reason: Optional[str],
         wake_check: Optional[str] = None, clock_only: bool = False, op: str = "schedule",
         wake_in: Optional[int] = None, blocker_id: Optional[str] = None) -> dict:
    """``resolve`` plus re-park escalation: the record to store on the parking event.

    Raises ``RepeatedPark`` on the third park in a row on one premise. Card waits (their producer
    is accountable), inferred clock waits (pacing continues, operator drags) and re-parks after the
    same probe fired (``prior_same_premise``: a natural window that closed again) do not escalate
    until ``PROBE_FIRE_REPARK_LIMIT`` such cycles (``probe_fire_reparks``: a broken probe), nor does
    a deploy wait behind a healthy core update window (``kanban_deploy_window``), whose
    evidence lands on the record as ``deploy_window`` for the caller's wake floor."""
    from hermes_cli import kanban_deploy_window as kdw
    record = resolve(conn, task_id, awaiting=awaiting, reason=reason, wake_check=wake_check,
                     clock_only=clock_only, op=op, wake_in=wake_in)
    if record["kind"] == "card" or (record["kind"] == "clock" and record.get("inferred")):
        return record
    record["premise"] = premise_hash(record, reason, wake_check)
    if record["kind"] == RESOURCE:
        # A governor/pool hold is internal and transient: its probe resumes the card when the reading drops.
        # Escalating it made "Unstick" cards for a premise nobody can act on (bb t_01a19f5cca30ed12), and on a
        # Claude-pool hold they burned the quota the hold waits on. A repeat records its count for the backoff.
        repeat = prior_same_premise(conn, task_id, record["premise"])
        if repeat:
            record["repeat"] = repeat
        return record
    if clock_window_end(record, time.time()) is not None:
        # A stated window (a weekly quota reset) ends by itself; an escalation card cannot shorten it
        # and, for a Claude-pool hold, burns the quota it waits on (bb t_01a19f847b7f5abb). No ``probe``
        # key either, so a later non-window park on the same probe starts its own count.
        return record
    probe = probe_premise(wake_check, op=op, wake_in=wake_in)
    if probe:
        record["probe"] = probe
    kdw.annotate(conn, task_id, record, now=int(time.time()))
    if kdw.is_healthy(record):
        return record
    from hermes_cli import kanban_passive_intake as passive
    if (not passive.eligible(task_id, wake_check, record, blocker_id)
            and prior_same_premise(conn, task_id, record["premise"], probe=probe) >= REPARK_LIMIT):
        raise RepeatedPark(record)
    fired = probe_fire_reparks(conn, task_id, record["premise"], probe)
    if fired >= PROBE_FIRE_REPARK_LIMIT:
        record["fired_reparks"] = fired
        raise RepeatedPark(record)
    return record


def resource_apply(record: Optional[dict], reason: Optional[str],
                   check_every: Optional[int]) -> tuple[Optional[str], Optional[int]]:
    """``(reason, check_every)`` for a park: a repeat resource park backs off and surfaces its gate.

    The n-th park in a row on one resource gate with no new evidence re-checks ``2**n`` times less often (up
    to ``RESOURCE_BACKOFF_MAX_SECONDS``), and the reason names the gate and its reading, so the board shows
    "waiting on the pool" rather than an opaque probe. Other parks pass through unchanged."""
    if not isinstance(record, dict) or record.get("kind") != RESOURCE:
        return reason, check_every
    repeat = int(record.get("repeat") or 0)
    if repeat <= 0:
        return reason, check_every
    from hermes_cli import kanban_wake_check as kwc
    base = kwc.normalize_every(check_every)
    every = max(base, min(base * 2 ** min(repeat, 8), RESOURCE_BACKOFF_MAX_SECONDS))
    record["backoff_check_every"] = every
    note = (f"[resource wait on {record.get('ref')}: park {repeat + 1} in a row on this gate with no new "
            f"evidence; re-checking every {every // 60} min"
            + (f". Gate: {record['why'][:300]}" if record.get("why") else "") + "]")
    return (f"{reason} {note}" if reason else note), every


# --- re-park escalation ------------------------------------------------------------------------

def _supervisor_profile(task) -> Optional[str]:
    """A capable distinct supervisor, or None (escalation falls back to the owner's profile)."""
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
    # Q2 (t_01a1d0ec4ab93f32): a voluntary ending's escalation; the receipt its enclosing ending staged rides this op.
    from hermes_cli.kanban_source_receipt import with_source_receipt
    if with_source_receipt(conn, task_id, lambda: conn.execute(sql, params).rowcount) != 1:
        return None
    key = f"{ESCALATION_PREFIX}{task_id}:{record['premise']}"
    existing = conn.execute("SELECT id, status FROM tasks WHERE idempotency_key = ?", (key,)).fetchone()
    owner = existing[0] if existing and existing[1] not in ("done", "archived") else None
    if owner is None:
        supervisor = _supervisor_profile(task) or task.assignee or "default"
        fired = record.get("fired_reparks")
        title = (f"Unstick {task_id}: probe fired {fired}x, re-parked each time" if fired
                 else f"Unstick {task_id}: parked 3x on the same premise")
        lead = (f"Card {task_id} ({task.title}): probe fired {fired} times and the worker re-parked each time: "
                "fix the probe or the worker's check." if fired else
                f"Card {task_id} ({task.title}) tried to park a third time on the same premise with no new "
                "evidence.")
        owner = kb.create_task(
            conn, title=title,
            body=(f"{lead}\n\nPremise: {json.dumps({k: v for k, v in record.items() if k != 'premise'})}\n"
                  f"Latest reason: {(reason or '(none)')[:1500]}\n\n"
                  "This is agent-owned work, not a wait and not a question for David. Remove the cause: "
                  "do the action (grant, install, fix) where the capability lives, or route the card to the "
                  "profile/gateway that can, or prove the premise false. Then complete this card with the "
                  f"evidence; {task_id} waits on it and resumes when it is done."),
            assignee=supervisor, created_by=task.assignee, workspace_kind="scratch", triage=False,
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
    if not awaiting.get("inferred"):
        awaiting["auto_card_probe"] = True
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


def escalated_fired(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    """``fired_reparks`` of this card's latest event when it is a fire-cap escalation, else None."""
    row = conn.execute("SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                       (task_id,)).fetchone()
    if not row or row[0] != ESCALATED_EVENT:
        return None
    try:
        got = (json.loads(row[1] or "{}").get("awaiting") or {}).get("fired_reparks")
    except (ValueError, AttributeError):
        return None
    return got if isinstance(got, int) and not isinstance(got, bool) and got > 0 else None


def escalation_message(task_id: str, owner: Optional[str], fired: Optional[int] = None) -> str:
    """What the parking worker is told; ``fired`` (``escalated_fired``) selects the fire-cap wording."""
    what = (f"its wake probe fired {fired} times and it re-parked each time (fix the probe or the worker's "
            "check)" if fired else
            f"parked on the same premise {REPARK_LIMIT + 1} times with no new evidence")
    return (f"{ESCALATED_EVENT}: {task_id} {what}, so it was not parked again. It now waits on owner card "
            f"{owner}, which must remove the cause (with no capable supervisor configured it lands in triage "
            "for the orchestrator). End your turn.")
