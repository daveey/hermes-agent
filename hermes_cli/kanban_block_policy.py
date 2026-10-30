"""Block is never a terminal parking state for agent-owned reasons.

``capability`` / ``transient`` / ``needs_input`` (without a human_request) / untyped blocks with no human ask, no wake
probe and no routed assignee have no owner: nothing wakes them. Worker-facing
entry points (tool + CLI) refuse them with a typed error naming the fix; the
dispatcher re-spawns any that already exist (``sweep_agent_owned_blocks``);
creation refuses ``blocked`` cards that carry no human ask.
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Optional

# needs_input is agent-owned when it carries no human_request: the sidebar only shows a block as
# blocked when needs_human() holds, so an ask-less needs_input looks like work in progress.
AGENT_OWNED_KINDS = (None, "capability", "transient", "needs_input")
AGENT_BLOCK_GRACE_SECONDS = 2 * 3600
ERROR_CODE = "agent_owned_block"
_DECISION_TITLE = re.compile(r"^\s*(for you\s*:|choose\b|decide\b|approve\b)", re.IGNORECASE)


def block_policy_error(kind, *, human_request=None, wake_check=None, clock_only=False,
                       blocker_id=None, reason=None) -> Optional[str]:
    """Typed refusal text for an ownerless block, else None."""
    if kind not in AGENT_OWNED_KINDS:
        return None
    if human_request or wake_check or clock_only or blocker_id:
        return None
    if reason and "SCHEDULED_UNTIL=" in reason:
        return None
    if kind == "needs_input":
        return (
            f"{ERROR_CODE}: kind='needs_input' with no human_request is invisible to David (the "
            "sidebar shows it as active) and nothing wakes it. Pick one: (1) a step only David can "
            "take -> pass human_request (action, why_human, interaction, scope, evidence); (2) a "
            "file, permission or tool another gateway holds -> declare the capability need "
            "(needs=['host:<gateway>'] or the exact capability, e.g. 'ssh:mini') so an owner card "
            "is opened on that gateway, or kanban_create the owner card there and park on it "
            "(kind='time', awaiting={kind:'card', ref, gateway}); (3) a timed wait -> kind='time' "
            "with awaiting plus wake_check (or clock_only=true)."
        )
    return (
        f"{ERROR_CODE}: kind={kind!r} with no human_request, wake_check or clock_only leaves the "
        "card with no owner and nothing to wake it. Pick one: (1) a decision/action only David can "
        "take -> pass human_request (kind needs_input or capability); (2) a condition an agent action "
        "changes (a grant, a permission, an install, a fix) is work, not a wait -> create the owner "
        "card on the gateway that holds the capability and park on it (kind='time', "
        "awaiting={kind:'card', ref, gateway}), or kind='assignment' so a capable supervisor is "
        "routed; (3) a wait needs a producer -> kind='time' with awaiting naming it (a live card, a "
        "CI run, a vendor ticket, a real clock window) plus wake_check (or clock_only=true). "
        "If the work is actually done, call kanban_complete; if it needs another card, "
        "kanban_create it and link it as a parent."
    )


ASK_LINT_CODE = "user_ask_incomplete"
_UNBLOCK = re.compile(r"\bun-?block", re.IGNORECASE)
_ARCHIVE = re.compile(r"\barchiv", re.IGNORECASE)


RELAY_ASK_CODE = "agent_relay_ask"
# "from/in your own Hermes (Desktop|CLI) chat", "ask/tell your (Hermes) chat/agent/session to ...": the ask only
# makes David carry a message to an agent. Kept narrow: "asks the vendor's agent" or "sign in" never match.
_RELAY = re.compile(
    r"\b(?:from|in|via|using)\s+your\s+own\s+(?:hermes\s+)?(?:desktop\s+|cli\s+)?(?:or\s+cli\s+)?"
    r"(?:chat|session|agent)\b"
    r"|\b(?:ask|tell|have|get)\s+your\s+(?:own\s+)?(?:hermes\s+)?(?:desktop\s+|cli\s+)?"
    r"(?:chat|session|agent)\b",
    re.IGNORECASE)


def agent_relay_ask_error(action: str) -> Optional[str]:
    """Typed refusal for an ask that only relays a message from David to an agent (self-heal agent-c03353a3cf).

    Hub t_01a188c8ddf998ac asked David to "ask for the broker Gemini secret repair from your own Hermes chat on
    the AWS-authorized Mac"; that chat did it itself in 10 minutes. Such a block is a routing gap shown to David
    as his task, so the worker must route it to an agent instead.
    """
    if not _RELAY.search(action):
        return None
    return (f"{RELAY_ASK_CODE}: human_request.action asks David to pass a message to an agent or chat "
            "(\"from your own Hermes chat\", \"ask your agent to ...\"). Agents can do that without him: "
            "(1) declare the capability the other gateway holds with kanban_block(needs=['aws:<account>:<scope>' "
            "| 'ssh:<host>' | 'host:<gateway>']) so an owner card opens there; or (2) kanban_comment the exact "
            "request and send it to the owning chat with cross_session (action='send'), or kanban_create the "
            "owner card on that gateway (placement='<gateway>') and park on it. If no gateway holds the "
            "capability, the gap is the capability registry: fix the probe config (kanban.capabilities.probes) "
            "on the host that has the access. Keep human_request for what only David can do: approve, pay, "
            "sign in, a physical step.")


def user_ask_lint_error(human_request) -> Optional[str]:
    """Typed refusal when a block on David does not say, in its ask line, what unblock and archive mean.

    ``human_request.action`` is the one line every surface shows David verbatim (card view, For-you queue, chat
    close-out and ask-relay notices). An action like "Pick A or B" or "the safety change" left him reading
    "blocked" with no idea what to do or what his click would do (coach pattern blocked-card-missing-user-ask).
    An action that only asks him to relay a message to an agent is refused too (:func:`agent_relay_ask_error`).
    Only agent entry points (kanban_block / kanban_create tools) call this; internal trackers are not linted.
    """
    if not isinstance(human_request, dict):
        return None
    action = " ".join(str(human_request.get("action") or "").split())
    relay = agent_relay_ask_error(action)
    if relay:
        return relay
    missing = [label for label, rx in (("what unblocking the card does", _UNBLOCK),
                                       ("what archiving it does", _ARCHIVE)) if not rx.search(action)]
    if not missing:
        return None
    return (f"{ASK_LINT_CODE}: human_request.action is the ask line David sees verbatim on the card and in chat "
            f"notices, and it does not say {' or '.join(missing)}. Rewrite action as one self-contained line: "
            "the exact thing to do and where, then 'Unblock = <what happens next>; archive = <what happens / "
            "what is dropped>'. Example: \"Reply with the owner (david, household or system) for device "
            "'barley' in this chat. Unblock = the agent records it and enforces privacy; archive = leave it "
            "unowned.\"")


def ask_lint_check(human_request) -> tuple:
    """``_check`` args for the tool entry points: ``(ok, message, rejection)``."""
    err = user_ask_lint_error(human_request)
    code = RELAY_ASK_CODE if err and err.startswith(RELAY_ASK_CODE) else ASK_LINT_CODE
    return err is None, err or "", code


def creation_lint_error(title, initial_status, human_request, *, triage=False) -> Optional[str]:
    """Refuse decision cards / blocked cards that carry no human ask."""
    if human_request:
        return None
    if initial_status == "blocked" and not triage:
        return (f"{ERROR_CODE}: initial_status='blocked' requires human_request (the ask for David); "
                "otherwise create the card ready/todo or use parents for a dependency.")
    if title and _DECISION_TITLE.match(title) and initial_status == "blocked":
        return f"{ERROR_CODE}: decision-style card needs human_request."
    return None


def sweep_agent_owned_blocks(conn: sqlite3.Connection, *, now: Optional[int] = None,
                             grace: int = AGENT_BLOCK_GRACE_SECONDS) -> list[str]:
    """One triage respawn, with evidence, for agent-owned blocks older than *grace*."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db import _json_dict
    now = int(now if now is not None else time.time())
    escalated: list[str] = []
    rows = conn.execute(
        "SELECT id, block_kind FROM tasks WHERE status = 'blocked' AND human_request IS NULL "
        "AND current_run_id IS NULL AND (block_kind IS NULL OR block_kind IN ('capability','transient','needs_input'))"
    ).fetchall()
    for row in rows:
        tid = row["id"]
        ev = conn.execute(
            "SELECT id, payload, created_at FROM task_events WHERE task_id = ? "
            "AND kind IN ('blocked','block_loop_detected') ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        if ev is not None:
            started, ev_id = int(ev["created_at"]), int(ev["id"])
            payload = _json_dict(ev["payload"])
        else:
            t = conn.execute("SELECT created_at FROM tasks WHERE id = ?", (tid,)).fetchone()
            started, ev_id, payload = int(t["created_at"] or 0), 0, {}
        if now - started < grace or payload.get("blocker_id"):
            continue
        if conn.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'agent_block_escalated' "
                        "AND id > ? LIMIT 1", (tid, ev_id)).fetchone():
            continue
        evidence = (f"Agent-owned block ({row['block_kind'] or 'untyped'}) with no human ask, wake "
                    f"probe or worker for {(now - started) // 3600}h. Reason: "
                    f"{payload.get('reason') or '(none recorded)'}. Re-check whether the cause is "
                    "gone; then finish, schedule with a wake_check, route as assignment, or give a "
                    "real human_request.")
        with kb.write_txn(conn):
            changed = conn.execute(
                "UPDATE tasks SET status = 'triage', block_kind = 'transient' "
                "WHERE id = ? AND status = 'blocked' AND human_request IS NULL", (tid,)).rowcount
            if changed:
                kb._append_event(conn, tid, "agent_block_escalated", {
                    "reason": evidence, "kind": row["block_kind"], "blocked_at": started})
                escalated.append(tid)
    return escalated
