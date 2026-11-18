"""[daveey] One delayed replay for a failed automation turn.

An automation turn is a turn no person typed: a Kanban notice (``display_kind="kanban_notice"``, which the
chat-closeout and owner-wake plugins use too) or a background-process report (``"process_complete"``). Before
this module such a turn that failed after its notice row was written ended on the user-turn boundary "Your request
was not processed. Send it again ..." that nobody could act on, and nothing ran it again: the notice ledger acks
a notice once its row is in the transcript, and the refused-turn replay (``_hold_refused_turn``, 2185d7299c)
excludes automation turns so a failing delivery can never loop.

This module is the retry owner between the two, bounded so it cannot loop either:

- a failed automation turn (or a wake whose agent build failed) schedules ONE replay, at least
  ``REPLAY_MIN_DELAY_S`` later, or at the recorded ``failure_resets_at`` when the failure is a typed quota or
  route exhaustion with a known reset;
- a second failure of the same chat's automation turns while one replay is pending joins it (one timer per chat);
- a later turn in the chat that succeeds cancels the replay when the failed delivery is already in the transcript
  (that turn's context carried it); a delivery that never reached the transcript (an agent build failure) is kept;
- the replay is marked (``display_metadata[REPLAY_METADATA_KEY]``): its own failure gives up, records it and
  schedules nothing, except when a typed quota/route wall that still holds refused it (a ``failure_resets_at``
  still ahead, within ``WALL_HORIZON_S``): then the wall, not the work, failed, so it waits for the new reset, at
  most ``WALL_REPLAYS_MAX`` replays in all (``rewall_due``; hub chat 20261101_012339_5a8ef0, Nov 17: the replay
  fired at the recorded reset, the provider still refused and rebooked an hour, the replay gave up and the notice
  waited 8.5 h for a manual "resume", provider-169ae0ed1f).

The timers live in this process: a backend restart drops a pending replay (the boot sweeps and the notice
guarantee own that case). The boundary row an automation turn leaves says what failed and what happens next
(``failure_line``), never "send it again".
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from tui_gateway.update_pause import SUCCESSOR_YIELD_UNKNOWN_S

logger = logging.getLogger(__name__)

AUTOMATION_KINDS = frozenset({"kanban_notice", "process_complete"})
REPLAY_METADATA_KEY = "automation_replay"
# The replay never runs sooner than this after the failure (the card's floor, and longer than a lease blip).
REPLAY_MIN_DELAY_S = 60.0
# The replay found a turn running: it looks again this much later, at most this many times, then gives up.
BUSY_RECHECK_S = 60.0
# [daveey] "busy" also means a dead writer's paused turn is left to its successor (``update_pause``), for up to
# ``SUCCESSOR_YIELD_UNKNOWN_S`` from the pause stamp. The rechecks outlast that window by one interval, so a
# replay that first fired just after the stamp is still pending when the window ends (10 x 60 s gave up at
# 600 s of a 660 s window; review t_01a1cfcca83f9266).
BUSY_RECHECKS_MAX = math.ceil((SUCCESSOR_YIELD_UNKNOWN_S + BUSY_RECHECK_S) / BUSY_RECHECK_S)
# [daveey] A replay refused by a quota/route wall that still holds waits for the new reset: at most this many
# replays in all, and only for a reset at most this far out (a notice is stale after a day).
WALL_REPLAYS_MAX = 3
WALL_HORIZON_S = 86400.0
# Tests switch this off (tests/tui_gateway/conftest.py) so no real timer outlives the test that armed it.
ENABLED = True

_TASK_ID = re.compile(r"\bt_[0-9a-f]{8,}\b")
_CAUSE_MAX_CHARS = 200
_NOTE_HEAD = "[Hermes: automatic retry of "
_NOTE_TAIL = "Check the current state before redoing anything.]\n\n"
_RETRY_PREFIX = "Retry: "


@dataclass
class _Item:
    text: str
    display_kind: str
    display_metadata: dict
    persisted: bool  # the delivery's row is in the transcript, so a later successful turn carries it
    cause: str
    failed_at: float
    attempt: int = 0  # replays this delivery already had (a wall-refused replay re-armed: ``rewall_due``)


@dataclass
class _Entry:
    items: list = field(default_factory=list)
    due: float = 0.0
    timer: Any = None
    busy_rechecks: int = 0


_lock = threading.Lock()
_entries: dict[str, _Entry] = {}


@dataclass(frozen=True)
class Plan:
    """What happens to one failed automation turn: ``retry`` at ``due``, or not; ``line`` is its boundary copy."""
    retry: bool
    due: float | None
    line: str
    cause: str


def is_automation(display_kind: Any) -> bool:
    return display_kind in AUTOMATION_KINDS


def is_replay(display_metadata: Any) -> bool:
    return isinstance(display_metadata, dict) and bool(display_metadata.get(REPLAY_METADATA_KEY))


def replay_attempt(display_metadata: Any) -> int:
    """Which replay this turn is (1 for the first), 0 for a turn that is not a replay."""
    if not is_replay(display_metadata):
        return 0
    marker = display_metadata.get(REPLAY_METADATA_KEY)
    attempt = marker.get("attempt") if isinstance(marker, dict) else None
    return attempt if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0 else 1


def rewall_due(display_metadata: Any, resets_at: Any, now: float) -> float | None:
    """[daveey] When a replay that failed gets another one: only when a typed quota/route wall that still holds
    refused it (``resets_at`` ahead of ``now``, at most ``WALL_HORIZON_S`` out) and fewer than
    ``WALL_REPLAYS_MAX`` replays ran. Then it is due at that reset; any other replay failure gives up (None)."""
    reset = _number(resets_at)
    if reset is None or reset <= now or reset > now + WALL_HORIZON_S:
        return None
    if replay_attempt(display_metadata) >= WALL_REPLAYS_MAX:
        return None
    return replay_due(now, reset)


def _unwrap_replay(text: Any, display_metadata: Any, failed_at: float) -> tuple[str, dict, float, int]:
    """A failed replay's deliveries as they were before ``replay_payload`` wrapped them: ``(text, metadata,
    first failed_at, replays spent)``, so a re-armed replay carries one note, not one per attempt."""
    meta = dict(display_metadata) if isinstance(display_metadata, dict) else {}
    marker = meta.pop(REPLAY_METADATA_KEY, None)
    attempt = replay_attempt({REPLAY_METADATA_KEY: marker}) if marker else 0
    first = _number(marker.get("failed_at")) if isinstance(marker, dict) else None
    shown = meta.get("display_text")
    if isinstance(shown, str) and shown.startswith(_RETRY_PREFIX):
        meta["display_text"] = shown[len(_RETRY_PREFIX):]
    body = text if isinstance(text, str) else ""
    if body.startswith(_NOTE_HEAD) and _NOTE_TAIL in body:
        body = body.split(_NOTE_TAIL, 1)[1]
    return body, meta, (first if first is not None else failed_at), attempt


def failure_cause(error: Any) -> str:
    """The failure in one short line (first non-empty line, whitespace collapsed, capped)."""
    text = str(error or "").strip()
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = " ".join(line.split()).rstrip(".")
    if len(line) > _CAUSE_MAX_CHARS:
        line = line[:_CAUSE_MAX_CHARS - 1].rstrip() + "…"
    return line or "the turn failed"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def replay_due(now: float, resets_at: Any = None) -> float:
    """``now + REPLAY_MIN_DELAY_S``, or the recorded reset when that is later (typed quota/route exhaustion)."""
    due = now + REPLAY_MIN_DELAY_S
    reset = _number(resets_at)
    return max(due, reset) if reset is not None else due


def stale_code_reason() -> str | None:
    """Why this process is being replaced by one on newer code (its host loops are retired), else None. Code skew
    alone does not count: a skewed process that is not retiring runs the replay itself, on the same stale code, so
    only a retired one hands it to a successor. Never raises."""
    try:
        from hermes_cli.host_loop_lease import host_loops_retired
        return host_loops_retired()
    except Exception:  # health: allow BLE001 -- an unreadable state is no proof of a successor
        return None


def trusted_resets_at(failure: Any) -> Any:
    """[daveey] A build failure's ``failure_resets_at`` (``failure`` is the ``agent_failure`` dict or the value
    itself), or None when this process is retired (``host_loops_retired``): a successor on the checkout's code
    runs the replay, and that code may already route around the quota or route wall, so the replay and the
    deferred close-out take the short delay instead of waiting days for that wall's reset (hub :9148 booked Nov 17
    for a wake that failed on pre-fix code, t_01a1d65520e78e7d). A skewed process that is not retiring keeps the
    reset: a short-delay replay would run on the same stale code, fail again and give up (t_01a1d6b19bf73072)."""
    resets_at = failure.get("failure_resets_at") if isinstance(failure, dict) else failure
    if resets_at is None:
        return None
    stale = stale_code_reason()
    if stale:
        logger.info("automation-replay: ignoring the recorded reset %s: %s", resets_at, stale)
        return None
    return resets_at


def retiring_backend_refuses(label: str, key: str) -> bool:
    """[daveey] True when this backend is being replaced (superseded always-on, boot commit behind the checkout):
    it starts no automation turn on its stale code, and the caller reports ``"busy"`` so a later pass, run by
    the successor, delivers it (hub :9148's sweep wake during its drain, t_01a1d65520e78e7d)."""
    try:
        from hermes_cli.host_loop_lease import host_loops_retired
        retired = host_loops_retired()
    except ImportError:
        return False
    if retired:
        logger.info("%s: not waking %s here: this backend is retiring (%s)", label, key, retired)
    return bool(retired)


def replay_failed_wake(key: str, text: str, session: dict, display_kind: str | None,
                       display_metadata: dict | None, label: str) -> None:
    """[daveey] A wake that carried an automation delivery (sweep-wake, owner-wake, a notice) failed before its turn
    could start (agent build): ONE delayed replay, at the build failure's recorded quota/route reset when it has one
    (``agent_failure.failure_resets_at``, unless this process is retired: ``trusted_resets_at``), else
    ``REPLAY_MIN_DELAY_S``. The delivery is not in the transcript, so a later successful turn does not cancel it.
    A replay's own failure gives up. A chat close-out wake is included: ``notice_guarantee`` stamps its row
    ``closeout.state = "deferred"`` with a ``retry_at`` after this replay's due time (the durable fallback when this
    in-process timer dies with its backend), and the replay keeps the close-out's own ``closeout`` metadata, so it
    cannot start another close-out. (Was ``session_notifications._replay_failed_wake``.)"""
    meta = display_metadata if isinstance(display_metadata, dict) else {}
    if not is_automation(display_kind):
        return
    cause = failure_cause(session.get("agent_error") or "agent build failed")
    what = subject(display_kind, meta, text)
    now = time.time()
    resets_at = trusted_resets_at(session.get("agent_failure"))
    if is_replay(meta):
        due = rewall_due(meta, resets_at, now)
        if due is None:
            logger.warning("automation-replay: %s", failure_line(what, cause, gave_up=True,
                                                                 retries=replay_attempt(meta)))
            return
        body, inner, failed_at, attempt = _unwrap_replay(text, meta, now)
        schedule(key, text=body, display_kind=str(display_kind), display_metadata=inner, due=due,
                 cause=f"{label}: {cause}", persisted=False, attempt=attempt, failed_at=failed_at)
        return
    due = replay_due(now, resets_at)
    schedule(key, text=text, display_kind=str(display_kind), display_metadata=meta, due=due,
             cause=f"{label}: {cause}", persisted=False)


def _task_ids(display_metadata: Any, text: Any) -> list[str]:
    ids: list[str] = []
    meta = display_metadata if isinstance(display_metadata, dict) else {}
    for raw in meta.get("task_ids") or ():
        if isinstance(raw, str) and raw and raw not in ids:
            ids.append(raw)
    if not ids:
        for source in (meta.get("display_text"), text):
            for match in _TASK_ID.findall(str(source or "")):
                if match not in ids:
                    ids.append(match)
            if ids:
                break
    return ids


def subject(display_kind: Any, display_metadata: Any, text: Any) -> str:
    """What failed, in words: ``the notice for t_…`` / ``the background-job report``."""
    if display_kind == "process_complete":
        return "the background-job report"
    ids = _task_ids(display_metadata, text)
    if not ids:
        return "the Kanban notice"
    shown = ", ".join(ids[:3]) + (f" and {len(ids) - 3} more" if len(ids) > 3 else "")
    return f"the notice for {shown}"


def format_when(ts: float, now: float | None = None) -> str:
    """``HH:MM PT`` (David's clock), with the date when it is not today there."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        zone, label = ZoneInfo("America/Los_Angeles"), "PT"
    except Exception:  # health: allow BLE001 -- no tz database: UTC is still a correct time
        from datetime import UTC
        zone, label = UTC, "UTC"
    when = datetime.fromtimestamp(ts, zone)
    today = datetime.fromtimestamp(time.time() if now is None else now, zone)
    if when.date() != today.date():
        return f"{when:%b} {when.day} {when:%H:%M} {label}"
    return f"{when:%H:%M} {label}"


def failure_line(what: str, cause: str, *, due: float | None = None, gave_up: bool = False,
                 now: float | None = None, attempt: int = 1, retries: int = 1) -> str:
    head = f"Couldn't process {what}: {cause}."
    if due is not None and attempt > 1:
        return (f"{head} The quota wall still holds: retrying at {format_when(due, now)} "
                f"(attempt {attempt} of {WALL_REPLAYS_MAX}).")
    if due is not None:
        return f"{head} Retrying once at {format_when(due, now)}."
    return f"{head} Gave up after {'one retry' if retries <= 1 else f'{retries} retries'}."


def plan_turn_failure(display_kind: Any, display_metadata: Any, text: Any, result: Any,
                      now: float | None = None) -> Plan | None:
    """The plan for a failed automation turn (None for any other turn). Pure: ``schedule`` acts on it."""
    if not ENABLED or not is_automation(display_kind) or not isinstance(result, dict):
        return None
    now = time.time() if now is None else now
    cause = failure_cause(result.get("error") or result.get("final_response"))
    what = subject(display_kind, display_metadata, text)
    if is_replay(display_metadata):
        attempt = replay_attempt(display_metadata)
        due = rewall_due(display_metadata, result.get("failure_resets_at"), now)
        if due is None:
            return Plan(False, None, failure_line(what, cause, gave_up=True, now=now, retries=attempt), cause)
        return Plan(True, due, failure_line(what, cause, due=due, now=now, attempt=attempt + 1), cause)
    # Any failure gets its one replay, "non-retryable" ones too: the classifier judges a provider call, and the
    # turns this owns failed on Hermes-side walls it reads as client errors (the chat ownership lease refusal of
    # the 19 lost notices was "Non-retryable client error"). One bounded replay costs one turn at most.
    due = replay_due(now, result.get("failure_resets_at"))
    return Plan(True, due, failure_line(what, cause, due=due, now=now), cause)


def _arm(key: str, entry: _Entry, delay: float, fire: Callable[[str], None]) -> None:
    timer = threading.Timer(max(delay, 0.0), lambda: fire(key))
    timer.daemon = True
    entry.timer = timer
    timer.start()


def _cancel_timer(entry: _Entry) -> None:
    cancel = getattr(entry.timer, "cancel", None)
    if callable(cancel):
        try:
            cancel()
        except Exception:  # health: allow BLE001 -- a timer that cannot cancel still finds no entry when it fires
            logger.debug("automation-replay: timer cancel failed", exc_info=True)
    entry.timer = None


def schedule(session_key: str, *, text: str, display_kind: str, display_metadata: dict | None, due: float,
             cause: str, persisted: bool, now: float | None = None,
             fire: Callable[[str], None] | None = None, attempt: int = 0, failed_at: float | None = None) -> bool:
    """Arm (or join) this chat's one pending replay. True when it is pending after the call. ``attempt`` is the
    replays the delivery already had (a wall-refused replay re-armed), ``failed_at`` its first failure."""
    key = str(session_key or "")
    if not ENABLED or not key or not isinstance(text, str) or not text.strip() or not is_automation(display_kind):
        return False
    now = time.time() if now is None else now
    item = _Item(text, display_kind, dict(display_metadata or {}), persisted, cause,
                 now if failed_at is None else failed_at, attempt)
    fire = fire or _fire
    with _lock:
        entry = _entries.get(key)
        if entry is not None:
            entry.items.append(item)
            # A later quota/route reset wins: the joined deliveries wait for it together. A plain failure a few
            # seconds later joins the timer already armed (no re-arm per delivery).
            if due > entry.due + REPLAY_MIN_DELAY_S:
                entry.due = due
                _cancel_timer(entry)
                _arm(key, entry, max(due - now, REPLAY_MIN_DELAY_S), fire)
            logger.info("automation-replay: %s joined the pending replay of %s (%d deliveries)",
                        subject(display_kind, display_metadata, text), key, len(entry.items))
            return True
        entry = _Entry(items=[item], due=due)
        _entries[key] = entry
        _arm(key, entry, max(due - now, REPLAY_MIN_DELAY_S), fire)
    logger.info("automation-replay: %s failed in %s (%s); one replay at %s", subject(display_kind, display_metadata,
                text), key, cause, format_when(due, now))
    return True


def cancel_carried(*session_keys: str) -> bool:
    """A turn in the chat succeeded: drop the pending deliveries its context already carried. True if any went."""
    dropped = False
    with _lock:
        for key in dict.fromkeys(k for k in session_keys if k):
            entry = _entries.get(key)
            if entry is None:
                continue
            kept = [item for item in entry.items if not item.persisted]
            if len(kept) == len(entry.items):
                continue
            dropped = True
            entry.items = kept
            if not kept:
                _cancel_timer(entry)
                del _entries[key]
            logger.info("automation-replay: a later turn in %s succeeded and carries the failed delivery; %s",
                        key, "replay cancelled" if not kept else f"{len(kept)} unpersisted delivery(ies) still pending")
    return dropped


def pending(session_key: str) -> bool:
    with _lock:
        return str(session_key or "") in _entries


def reset() -> None:
    """Drop every pending replay (tests)."""
    with _lock:
        for entry in _entries.values():
            _cancel_timer(entry)
        _entries.clear()


def replay_payload(items: list) -> tuple[str, str, dict]:
    """``(text, display_kind, display_metadata)`` of the one turn that replays ``items``."""
    first = items[0]
    failed_at = min(item.failed_at for item in items)
    causes = "; ".join(dict.fromkeys(item.cause for item in items))
    attempt = max(item.attempt for item in items) + 1
    which = ("This is the only retry." if attempt == 1 else
             f"This is retry {attempt} of {WALL_REPLAYS_MAX}: a quota wall refused the earlier ones.")
    note = (f"{_NOTE_HEAD}{len(items)} automated deliver{'y' if len(items) == 1 else 'ies'} "
            f"that failed at {format_when(failed_at)} ({causes}). {which} {_NOTE_TAIL}")
    text = note + "\n\n---\n\n".join(item.text for item in items)
    marker = {"attempt": attempt, "failed_at": failed_at, "cause": causes, "deliveries": len(items)}
    if len(items) == 1:
        meta = {**first.display_metadata, REPLAY_METADATA_KEY: marker}
        if isinstance(meta.get("display_text"), str) and meta["display_text"]:
            meta["display_text"] = f"{_RETRY_PREFIX}{meta['display_text']}"
        return text, first.display_kind, meta
    kind = "kanban_notice" if any(item.display_kind == "kanban_notice" for item in items) else first.display_kind
    shown = [str(item.display_metadata.get("display_text") or "") for item in items]
    task_ids: list[str] = []
    for item in items:
        for tid in _task_ids(item.display_metadata, item.text):
            if tid not in task_ids:
                task_ids.append(tid)
    meta: dict = {REPLAY_METADATA_KEY: marker}
    if any(shown):
        meta["display_text"] = _RETRY_PREFIX + " · ".join(s for s in shown if s)
    if task_ids:
        meta["task_ids"] = task_ids
    return text, kind, meta


def install_failure_copy(st: Any, text: Any) -> None:
    """The failed-turn boundary of an automation turn (``conversation_loop._close_durable_failed_turn``) says what
    failed and whether it is retried, never "send it again": the agent asks this hook for the copy, and the plan it
    makes is the one ``schedule_for_turn`` acts on. ``st`` is a ``prompt_turn._TurnRun``, read defensively."""
    if not is_automation(getattr(st, "display_kind", None)):
        return
    st.automation_text = text if isinstance(text, str) else ""

    def copy(result: Any) -> str | None:
        st.automation_plan = plan_turn_failure(st.display_kind, getattr(st, "display_metadata", None),
                                               st.automation_text, result)
        return st.automation_plan.line if st.automation_plan is not None else None
    try:
        st.agent._failed_turn_notice_override = copy
    except Exception:  # health: allow BLE001 -- an agent that refuses the hook keeps the stock copy
        logger.debug("automation-replay: could not install the failure copy", exc_info=True)


def remove_failure_copy(st: Any) -> None:
    agent = getattr(st, "agent", None)
    if agent is not None and getattr(agent, "_failed_turn_notice_override", None) is not None:
        try:
            agent._failed_turn_notice_override = None
        except Exception:  # health: allow BLE001 -- the next turn installs its own copy (or none)
            logger.debug("automation-replay: could not remove the failure copy", exc_info=True)


def plan_for_turn(st: Any, result: Any) -> None:
    """The plan of a failed automation turn whose boundary did not make one (no durable tail to close, a raise)."""
    kind = getattr(st, "display_kind", None)
    if getattr(st, "automation_plan", None) is None and is_automation(kind):
        st.automation_plan = plan_turn_failure(kind, getattr(st, "display_metadata", None),
                                               getattr(st, "automation_text", ""),
                                               result if isinstance(result, dict) else {})


def schedule_for_turn(session: dict, st: Any) -> None:
    """Arm the one delayed replay a failed automation turn planned (a replay's own failure plans none)."""
    plan = getattr(st, "automation_plan", None)
    if plan is None:
        return
    st.automation_plan = None
    key = str(session.get("session_key") or getattr(st, "marker_key", "") or "")
    if not plan.retry:
        logger.warning("automation-replay: %s in %s", plan.line, key)
        return
    text, meta = getattr(st, "automation_text", ""), getattr(st, "display_metadata", None)
    attempt, failed_at = 0, None
    if is_replay(meta):  # a wall refused the replay (``rewall_due``): re-arm the deliveries it wrapped
        text, meta, failed_at, attempt = _unwrap_replay(text, meta, time.time())
    schedule(key, text=text, display_kind=st.display_kind, display_metadata=meta, due=plan.due, cause=plan.cause,
             persisted=True, attempt=attempt, failed_at=failed_at)


def _fire(key: str) -> None:
    with _lock:
        entry = _entries.pop(key, None)
    if entry is None or not entry.items:
        return  # cancelled by a later successful turn
    text, kind, meta = replay_payload(entry.items)
    what = ", ".join(dict.fromkeys(subject(i.display_kind, i.display_metadata, i.text) for i in entry.items))
    try:
        from tui_gateway import server
        verdict = server._wake_session_turn(key, text, display_kind=kind, display_metadata=meta,
                                            label="automation-replay")
    except Exception:  # health: allow BLE001 -- the replay is best effort; logged as given up below
        logger.warning("automation-replay: replay of %s raised", key, exc_info=True)
        verdict = "error"
    if verdict == "started":
        logger.info("automation-replay: replayed %s in %s", what, key)
        return
    if verdict == "busy" and entry.busy_rechecks < BUSY_RECHECKS_MAX:
        with _lock:
            if key not in _entries:  # a newer failure may have armed its own entry meanwhile: join it
                entry.busy_rechecks += 1
                _entries[key] = entry
                _arm(key, entry, BUSY_RECHECK_S, _fire)
                return
            current = _entries[key]
            current.items[:0] = entry.items
        return
    logger.warning("automation-replay: gave up on %s in %s after one retry (%s); nothing more is scheduled",
                   what, key, verdict)
