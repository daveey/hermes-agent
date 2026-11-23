"""What an open, parked kanban card waits on, who produces it, and whether that wait has stalled. [daveey]

A park used to be described only by its wake mechanism (a clock, a probe, a parent). The sidebar showed "waiting on
N cards" while nothing was going to produce what those cards waited for, and nobody noticed for hours. This module
names the producer of each wait and marks the ones only an agent can end:

- ``waiting_on``: a short label of what the card waits on, from the park's structured ``awaiting``
  (``{kind: card|ci|deploy|external|clock|human, ref, why}``, recorded by the core "no producer, no park" guard)
  or, for older parks, from the wake check (``hermes kanban wait-check task t_x`` -> ``card t_x``).
- ``agent_owned``: nothing outside the agents will end this wait. Transient/capability/assignment blocks, triage
  (gave up / block loop), continuation_exhausted, and a schedule with no named producer.
- ``wait_state``: ``human`` (a validated ask, or legacy device-access attention with ``owner_step``;
  attention is not approval/consent), ``stalled`` (agent-owned for longer than
  ``STALL_AFTER_S``), or ``waiting`` (anything else parked).
- ``premise``: a stable key of the reason (status, kind and the reason text, never timestamps), so a wake that
  fires "once per premise" does not fire again when the same card re-parks on the same reason.

Pure functions over plain values: used by the session list (local boards and the hub replica), the task strip API
and the chat-closeout plugin's owner wake.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import time
from typing import Any, Optional


STALL_AFTER_S = 2 * 3600
# A contained card whose authority finished is stranded once this long has passed (a reopen-and-release attempt
# right after the authority completes is recovery, not a strand).
CONTAINED_STRAND_AFTER_S = 30 * 60

# Producers that change the awaited condition without an agent acting (a card that runs, a CI run, a deploy, a
# vendor/ticket, the clock, an internal resource gate whose own probe resumes the card once its reading drops).
LIVE_PRODUCER_KINDS = ("card", "ci", "deploy", "external", "clock", "resource")
# Block kinds only an agent clears (core kanban_block_policy.AGENT_OWNED_KINDS, plus assignment routing). Untyped
# (None/legacy) blocks are agent-owned too, but uncertain: they only count once stalled.
AGENT_BLOCK_KINDS = ("transient", "capability", "assignment", "needs_input")
# Events that put a card into its current parked state; the newest one dates the wait and carries its reason.
# ``continued`` is a continuation checkpoint parking the card ``scheduled`` with a ``wake_at``.
PARK_EVENT_KINDS = ("scheduled", "blocked", "gave_up", "continuation_exhausted", "block_loop_detected",
                    "triage_swept", "supervisor_escalated", "continued", "status", "dependency_wait",
                    "agent_block_escalated", "ask_rechecked")
# Triage reached this way is agent work (the worker gave up / ran out of budget / was escalated to a supervisor).
# Triage after a block loop waits on a human or a shared blocker; triage from intake (no park event) belongs to
# its specifier.
AGENT_TRIAGE_EVENTS = ("gave_up", "continuation_exhausted", "supervisor_escalated", "triage_swept", "ask_rechecked")
# A replicated card carries only its last run's outcome; these outcomes mean a run failed into triage.
_FAILED_RUN_OUTCOMES = ("gave_up", "reclaimed", "crashed", "timed_out", "oom_killed", "continuation_exhausted")

_WAIT_CHECK_TASK = re.compile(r"wait-check\s+task\s+(\S+)")
_WAIT_CHECK_BOARD = re.compile(r"wait-check\s+board-empty\s+(\S+)")
_GATEWAY = re.compile(r"--gateway[=\s]+(\S+)")
_CI = re.compile(r"\bgh\s+(?:run|pr\s+checks|workflow)\b|actions/runs|/check-runs|/statuses\b")
_SCRIPT = re.compile(r"([\w.-]+\.(?:py|sh|mjs|js))\b")
_TASK_ID = re.compile(r"\bt_[0-9a-f]{6,}\b")
_SHELL_NOISE = {"[", "[[", "test", "!", "if", "then", "set", "export", "env", "timeout", "sh", "bash", "-c", "exec"}
# A naive stamp reads as UTC; ``Z`` / ``+HH:MM`` / ``+HHMM`` (and minus) are honoured (``park_fields``).
_SCHEDULED_UNTIL = re.compile(r"SCHEDULED_UNTIL=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)")


def epoch_s(value: Any) -> Optional[float]:
    """Seconds since the epoch; millisecond stamps (some boards wrote them) are scaled down."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return v / 1000.0 if v > 1e11 else v


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def parse_awaiting(value: Any) -> Optional[dict[str, Any]]:
    """A valid ``awaiting`` object (kind, ref, why, gateway strings; ``inferred`` when core guessed it from the park's
    text rather than the parker declaring it) or None."""
    data = _json(value)
    if not isinstance(data, dict) or not isinstance(data.get("kind"), str):
        return None
    return {"kind": data["kind"].strip().lower(), "ref": str(data.get("ref") or "").strip(),
            "why": str(data.get("why") or "").strip(), "gateway": str(data.get("gateway") or "").strip(),
            "inferred": bool(data.get("inferred"))}


def _awaiting_label(awaited: dict[str, Any]) -> str:
    where = f" on {awaited['gateway']}" if awaited.get("gateway") else ""
    return f"{awaited['kind']} {awaited['ref']}{where}"


def _clock_label(wake_at: float) -> str:
    return "the clock until " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(wake_at))


def owner_device_step(wake_check: Any) -> Optional[str]:
    """Recognize legacy paired-device availability probes, not passive capture producers.

    This is a compatibility classification, not a claim that unlocking fixes a transport.
    New owner-device waits must use a validated human_request with a resolved_check.
    Do not inspect arbitrary scripts or execute the probe while deriving attention.
    """
    if not isinstance(wake_check, str):
        return None
    try:
        tokens = shlex.split(wake_check)
    except ValueError:
        return None
    if not tokens:
        return None
    executable = tokens[0].rsplit("/", 1)[-1]
    if re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", executable):
        tokens = tokens[1:]
        while tokens and tokens[0] in ("-S", "-I", "-B", "-u", "-E", "-s", "-O", "-OO"):
            tokens = tokens[1:]
    if tokens and "://" not in tokens[0] and re.fullmatch(
            r"probe_(?:paired_)?(?:phone|iphone|ipad|device)_(?:available|visibility)\.py",
            tokens[0].rsplit("/", 1)[-1]):
        return ("Confirm paired-device access for the phone/device, or report that this availability "
                "check is wrong. Unlocking or proximity alone may not make it visible.")
    return None


def producer_from_wake_check(cmd: Any) -> Optional[tuple[str, str]]:
    """``(kind, ref)`` named by a legacy wake check: a wait-check on a card (or a probe that reads one card's
    status), a CI read, or an opaque probe."""
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    gw = _GATEWAY.search(cmd)
    where = f" on {gw.group(1)}" if gw else ""
    m = _WAIT_CHECK_TASK.search(cmd)
    if m:
        return "card", f"{m.group(1)}{where}"
    m = _WAIT_CHECK_BOARD.search(cmd)
    if m:
        return "card", f"board {m.group(1)}{where}"
    if _CI.search(cmd):
        return "ci", "CI run"
    tid = _TASK_ID.search(cmd)
    if tid and ("kanban" in cmd or "status" in cmd):
        return "card", tid.group(0)
    script = _SCRIPT.search(cmd)
    if script:
        return "probe", script.group(1)
    for token in cmd.replace("$(", " ").replace("`", " ").split():
        word = token.rsplit("/", 1)[-1].strip("'\";()")
        if word and word not in _SHELL_NOISE and not word.startswith("-") and "=" not in word:
            return "probe", word[:40]
    return "probe", "shell check"


def _norm(text: Any) -> str:
    """Reason text without volatile parts (uuids, hex ids, numbers, whitespace), for the premise hash."""
    s = str(text or "").lower()
    s = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{20,}", "#", s)
    s = re.sub(r"\b[0-9a-f]*\d[0-9a-f]{5,}\b", "#", s)
    s = re.sub(r"\d+", "#", s)
    return re.sub(r"\s+", " ", s).strip()[:300]


def premise_key(kind: str, status: str, reason: Any = "", awaiting: Optional[dict] = None,
                reason_code: Any = None) -> str:
    """Stable id of WHY a card waits: structured fields first (kind, status, reason code, awaiting kind/ref); the
    free-text reason only when nothing structured names it."""
    structured = [kind, status, str(reason_code or ""),
                  [awaiting.get("kind"), _norm(awaiting.get("ref")), awaiting.get("gateway") or ""]
                  if awaiting else None]
    text = "" if (reason_code or awaiting) else _norm(reason)
    basis = json.dumps([structured, text], sort_keys=True)
    return f"{kind}:{hashlib.sha1(basis.encode()).hexdigest()[:12]}"


def describe_contained(info: dict[str, Any], *, now: Optional[float] = None) -> dict[str, Any]:
    """Wait info for a held card from its ``contained`` episode (``kanban_contained_wait.read``). The wait object
    carries ``contained: {episode, authority: {id, status}, producer: {id, status} | None}`` (unknown status counts as
    open). Authority open: waits on its release. Authority done/archived with the producer still open: waits on the
    producer. Authority finished for CONTAINED_STRAND_AFTER_S and no open producer: agent-owned and stalled at once,
    dated from the authority's completion, premise (contained, episode, authority status, authority completion)."""
    now = time.time() if now is None else now
    authority, producer = info["authority"], info.get("producer")
    done_at = epoch_s(authority.get("at"))
    shown = {"episode": info["episode"], "authority": {"id": authority["id"], "status": authority["status"]},
             "producer": {"id": producer["id"], "status": producer["status"]} if producer else None}
    base = {"wait_state": "waiting", "waiting_since": done_at if done_at is not None else epoch_s(info.get("contained_at")), "agent_owned": False, "agent_owned_certain": True,
            "premise": None, "contained": shown}
    if authority["status"] not in ("done", "archived"):
        return {**base, "waiting_on": f"release by card {authority['id']} ({authority['status']})"}
    if producer and producer["status"] not in ("done", "archived") and not producer.get("released"):
        return {**base, "waiting_on": f"producer card {producer['id']} ({producer['status']}), then its release"}
    if done_at is None or now - done_at < CONTAINED_STRAND_AFTER_S:
        return {**base, "waiting_on": f"release by card {authority['id']} ({authority['status']})"}
    key = json.dumps(["contained", info["episode"], authority["status"], done_at])
    exits = ("reopen it and resume the episode, or resume as producer after a new completed producer run"
             if authority["status"] == "done" and not producer else
             "no supported exit exists" if not producer else "resume as producer after a new completed run")
    return {**base, "wait_state": "stalled", "agent_owned": True,
            "waiting_on": f"an agent to release it: authority {authority['id']} is {authority['status']} ({exits})",
            "premise": f"contained:{hashlib.sha1(key.encode()).hexdigest()[:12]}"}


def _human_attention(status: str, human: bool, payload: dict, since: Any) -> Optional[dict[str, Any]]:
    step = owner_device_step(payload.get("wake_check")) if status == "scheduled" and not human else None
    if not human and not step:
        return None
    result = {"wait_state": "human", "waiting_on": "you", "waiting_since": since, "agent_owned": False,
              "agent_owned_certain": True, "premise": None}
    if step:
        result.update(waiting_on="you: " + step, owner_step=step)
    return result


def describe(status: Any, *, block_kind: Any = None, human: bool = False, park: Any = None,
             park_kind: Any = None, since: Any = None, awaiting: Any = None, reason: Any = None,
             now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """Wait info for one card, or None when it is not parked (running, ready, review, todo, done).

    ``park`` is the newest park event's payload (dict or JSON text) and ``park_kind`` its event kind; ``since``
    when the card parked (defaults to nothing: no age, never stalled). ``awaiting`` overrides the payload's.
    """
    status = str(status or "")
    if status not in ("scheduled", "blocked", "triage"):
        return None
    now = time.time() if now is None else now
    payload = _json(park) if park is not None else None
    payload = payload if isinstance(payload, dict) else {}
    kind = str(block_kind or payload.get("kind") or "") or None
    reason = reason if reason is not None else payload.get("reason") or payload.get("error") or ""
    reason_code = payload.get("reason_code")
    awaited = parse_awaiting(awaiting) or parse_awaiting(payload.get("awaiting"))
    since_s = epoch_s(since)
    exhausted = park_kind == "continuation_exhausted" or reason_code == "continuation_exhausted" or (
        status == "blocked" and str(reason).lstrip().startswith("continuation_exhausted"))

    def out(state_kind: str, label: str, agent_owned: bool, certain: bool = True) -> dict[str, Any]:
        stalled = agent_owned and since_s is not None and now - since_s >= STALL_AFTER_S
        return {"wait_state": "stalled" if stalled else "waiting", "waiting_on": label,
                "waiting_since": since_s, "agent_owned": agent_owned, "agent_owned_certain": certain,
                "premise": premise_key(state_kind, status, reason, awaited, reason_code) if agent_owned else None}

    attention = _human_attention(status, human, payload, since_s)
    if attention is not None:
        return attention
    if exhausted and status in ("blocked", "triage"):
        return out("continuation_exhausted", "an agent to split the work (continuation budget exhausted)", True)
    if status == "triage":
        if park_kind in AGENT_TRIAGE_EVENTS:
            label = "an agent to triage it (worker gave up)" if park_kind == "gave_up" else "an agent to triage it"
            return out("triage", label, True)
        if park_kind == "block_loop_detected":
            return out("triage:block_loop", "a human or shared blocker (block loop)", False)
        return out("triage:intake", "triage", False)
    if status == "blocked":
        if kind in AGENT_BLOCK_KINDS:
            return out(f"blocked:{kind}", f"an agent to clear a {kind} block", True)
        if kind == "dependency":
            return out("dependency", "a parent card", False)
        if kind is None:
            return out("blocked:untyped", "an agent to clear an untyped block", True, certain=False)
        if kind != "time":
            # another human kind without a validated ask: the user's move, just not visible. (An ask-less
            # needs_input is agent-owned, handled above; a validated ask returns early as ``human``.)
            return out(f"blocked:{kind}", f"an unvalidated ask ({kind})", False)
    # scheduled, or blocked kind=time: a park. Who produces what it waits for?
    wake_at = epoch_s(payload.get("wake_at"))
    if awaited and awaited["inferred"] and not (awaited["kind"] == "card" and awaited["ref"]):
        # Core's guess from the park text (every park on the "no producer, no park" core records one): an inferred
        # clock/external wait is just the legacy wake mechanism, judged below like a park with no ``awaiting``.
        awaited = None
    if awaited:
        if awaited["kind"] == "human":
            # A human ask hidden in a park: nobody sees it, so no one will produce the answer.
            return out("scheduled:hidden_ask", "a hidden ask (not visible to you)", True)
        if awaited["kind"] in LIVE_PRODUCER_KINDS and awaited["ref"]:
            return out("scheduled", _awaiting_label(awaited), False)
        if awaited["kind"] == "clock":
            # A declared window (a soak, a cooldown, a release slot): the clock produces it.
            label = _clock_label(wake_at) if wake_at else "a declared window"
            return out("scheduled", label + (f" ({awaited['why'][:80]})" if awaited["why"] else ""), False)
        return out("scheduled:no_producer", f"{awaited['kind'] or 'something'} with no named producer", True)
    found = producer_from_wake_check(payload.get("wake_check"))
    if found and found[0] in ("card", "ci"):
        return out("scheduled", f"{found[0]} {found[1]}", False)
    if found:
        # An opaque probe proves only that something polls, not that anything will make it true.
        return out("scheduled:probe", f"probe {found[1]}", True, certain=False)
    if wake_at:
        return out("scheduled", _clock_label(wake_at), False)
    if payload.get("unknown_producer"):
        return out("scheduled:unknown", "a producer the owner did not publish", False)
    return out("scheduled:no_producer", "nothing named", True, certain=False)


def park_fields(run: Any) -> Optional[dict[str, Any]]:
    """The few run fields a replicated card's wait needs (outcome, ended_at, a bounded reason, a parsed
    SCHEDULED_UNTIL), small enough to keep on cached remote rows. Recomputed into wait info at read time."""
    if not isinstance(run, dict):
        return None
    summary = str(run.get("summary") or "")
    until = _SCHEDULED_UNTIL.search(summary)
    wake_at = None
    if until:
        from datetime import UTC, datetime
        text = until.group(1)
        try:
            when = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            wake_at = int(when.timestamp())
        except ValueError:
            wake_at = None
    return {"outcome": run.get("outcome"), "ended_at": run.get("ended_at"), "reason": summary[:300],
            "wake_at": wake_at}


def describe_replica(card: dict[str, Any], *, now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """Wait info for a card on another gateway (status, block_kind, human_request and its last run, either as the
    replica's ``run`` or the cached ``park`` fields). A remote schedule names a producer only when the owner
    published one (``awaiting`` / ``wake_check``); otherwise it is shown as unknown and never counted as stalled,
    because a clock park and an orphan wait look identical from here."""
    from hermes_cli.kanban_human_request import needs_human

    status = card.get("reported_status") or card.get("status")
    raw_park = card.get("park")
    park: dict[str, Any] = (raw_park if isinstance(raw_park, dict) else park_fields(card.get("run"))) or {}
    try:
        human = needs_human(_json(card.get("human_request")), kind=card.get("block_kind"))
    except Exception:
        human = False
    published = bool(card.get("awaiting") or card.get("wake_check"))
    outcome = park.get("outcome")
    if status == "triage":
        outcome = "gave_up" if outcome in _FAILED_RUN_OUTCOMES else (
            "block_loop_detected" if outcome == "blocked" else outcome)
    contained = card.get("contained") if status == "triage" else None
    if isinstance(contained, dict):
        return describe_contained(contained, now=now)
    return describe(status, block_kind=card.get("block_kind"), human=human,
                    park={"wake_check": card.get("wake_check"), "reason": park.get("reason") or "",
                          "wake_at": park.get("wake_at"), "unknown_producer": not published},
                    park_kind=outcome, since=card.get("parked_at") or park.get("ended_at"),
                    awaiting=card.get("awaiting"), now=now)
