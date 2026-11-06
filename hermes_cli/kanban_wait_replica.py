"""Read owner task snapshots when SSH is unavailable. Never confuse unknown with pending."""
from __future__ import annotations

import json
import sqlite3
import time

from hermes_cli import kanban_capability_registry as registry
from hermes_cli.kanban_scope_heartbeat import scope_window

# Gateway task_scopes are refreshed each sync; task updated_at only changes on edits.
MAX_AGE_SECONDS = 600
# Exit code for "cannot tell, not yet": the producer card is known from the last replica, but its gateway has not
# synced for longer than MAX_AGE_SECONDS (a sleeping Mac). The wake-check dispatcher re-probes it without counting it
# as broken, up to kanban_wake_check.STALE_WAKE_AFTER. [daveey]
STALE_RC = 3
STATUSES = frozenset(("todo", "ready", "running", "scheduled", "review", "blocked", "triage", "done", "archived"))


class MissingReplica(ValueError):
    """No local observation exists for this owner-qualified card."""


class StaleReplica(ValueError):
    """The card was seen in the gateway's replica, but that replica is older than MAX_AGE_SECONDS."""

    def __init__(self, gateway, board, age, task, limit=MAX_AGE_SECONDS):
        super().__init__(f"stale replica for {gateway}/{board} (age={age:.0f}s, limit={limit:.0f}s)")
        self.gateway, self.board, self.age, self.task, self.limit = gateway, board, age, task, limit


class UnknownScope(ValueError):
    """The card was seen in the gateway's replica, but its owner published this board's scope as ``unknown``.

    The owner's collector (hermes-hub ``collect_all``/``scope_heartbeats``) writes ``state: unknown`` with no
    ``observed_at`` when it could not read the board for longer than its carry window; the task records it published
    before are kept, never tombstoned. So the last-seen status is known and nothing vouches for it now: "cannot tell,
    not yet", like a stale replica, but with no observation time to report as an age. [daveey]
    """

    def __init__(self, gateway, board, task):
        super().__init__(f"task scope unknown for {gateway}/{board}")
        self.gateway, self.board, self.task = gateway, board, task


def read_task(gateway, target, board=None):
    """Exact owner-qualified match, including completed/archived records, with fresh scope proof."""
    board = board or "default"
    db = registry._ro(registry.hub_db_path())
    try:
        db.execute("BEGIN")
        rows = db.execute("SELECT data FROM replica WHERE gw=? AND coll='tasks' AND key=?",
                          (gateway, f"default/{board}/{target}")).fetchall()
        if not rows:
            raise MissingReplica(f"{target} not found in {gateway} replica")
        tasks = [json.loads(r[0]) for r in rows]
        if any(t != tasks[0] for t in tasks[1:]):
            raise ValueError(f"ambiguous replica records for {target}")
        task = tasks[0]
        if (task.get("id") != target or task.get("board") != board or task.get("status") not in STATUSES
                or task.get("gateway") != gateway or task.get("profile") != "default"
                or task.get("owner_profile") != "default"):
            raise ValueError(f"invalid replica task {target}")
        scopes, limit = [], float(MAX_AGE_SECONDS)
        for (raw,) in db.execute("SELECT data FROM replica WHERE gw=? AND coll='gateway'", (gateway,)):
            info = json.loads(raw)
            scopes.extend(info.get("task_scopes", []))
            # A slow owner collector publishes its period: never call a heartbeat stale that the release
            # readers still count as fresh (kanban_scope_heartbeat). [daveey]
            limit = max(limit, scope_window(info))
        matched = [s for s in scopes if s.get("profile") == "default" and s.get("board") == board]
        if len(matched) == 1 and matched[0].get("state") == "unknown" and "observed_at" not in matched[0]:
            raise UnknownScope(gateway, board, task)
        if len(matched) != 1 or matched[0].get("state") != "ok":
            raise ValueError(f"missing successful task-scope observation for {gateway}/{board}")
        age = time.time() - float(matched[0].get("observed_at") or 0)
        if age > limit:
            raise StaleReplica(gateway, board, age, task, limit)
        if age < -60:
            raise ValueError(f"stale replica for {gateway}/{board} (age={age:.0f}s, limit={MAX_AGE_SECONDS}s)")
        return task
    finally:
        db.close()


def orphan_reason(task):
    """Facts preserved by the replica that make a producer non-live."""
    status = task["status"]
    evidence = task.get("wait_observation")
    if isinstance(evidence, dict) and evidence.get("orphan_reason"):
        return evidence["orphan_reason"]
    if status in ("archived", "triage"):
        return status
    if status == "blocked" and not task.get("human_request") and task.get("block_kind") not in ("assignment", "dependency"):
        return "blocked with no human ask or routed owner"
    return None


def observe_wait(conn, target, *, now):
    """Owner-only evidence collected in the same snapshot as the task row."""
    from hermes_cli.kanban_awaiting import card_orphan_reason
    from hermes_cli.kanban_session_activity import _local_wait_info
    info = _local_wait_info(conn, [target], now=now).get(target) or {}
    orphan = card_orphan_reason(conn, target)
    return {"orphan_reason": orphan,
            "parked_agent": bool(orphan or (info.get("agent_owned") and
                                 (info.get("agent_owned_certain") or info.get("wait_state") == "stalled")))}


def stale_message(target, stale):
    """``<id> <last seen status> (...)``: words 0-1 keep the shape every reader parses; ``stale_age=`` and
    ``gateway=`` are what the wake-check dispatcher reads to cap the wait. Never starts with ``error:``."""
    status = stale.task.get("status")
    orphan = " orphan producer;" if orphan_reason(stale.task) else ""
    return (f"{target} {status} (replica stale:{orphan} gateway={stale.gateway} not synced for {stale.age:.0f}s, "
            f"stale_age={stale.age:.0f}s, limit={stale.limit:.0f}s; last seen {status}, unknown now: not yet)")


def unknown_message(target, unknown):
    """Like ``stale_message``: words 0-1 keep the reader shape; ``source_state=unknown`` and ``gateway=`` are what the
    wake-check dispatcher reads (``kanban_wake_check.source_unknown``). No ``stale_age=``: the owner published no
    observation time, so none is invented. Never starts with ``error:``."""
    status = unknown.task.get("status")
    orphan = " orphan producer;" if orphan_reason(unknown.task) else ""
    return (f"{target} {status} (replica scope unknown:{orphan} gateway={unknown.gateway} could not read board "
            f"{unknown.board} (source_state=unknown); last seen {status}, unknown now: not yet)")


def check(gateway, target, *, board=None, until=(), since=(), live_timeout=None):
    try:
        source = "replica"
        try:
            task = read_task(gateway, target, board)
        except MissingReplica:
            if live_timeout is None:
                raise
            from hermes_cli.kanban_wait_live import read_task as read_live
            task = read_live(gateway, target, board=board, timeout_seconds=live_timeout)
            source = "live owner"
        except StaleReplica as stale:
            return STALE_RC, stale_message(target, stale)
        except UnknownScope as unknown:
            return STALE_RC, unknown_message(target, unknown)
        status = task["status"]
        orphan = orphan_reason(task)
        suffix = f" ({source}; orphan producer)" if orphan else f" ({source})"
        if status in since:
            return 1, f"{target} {status} (unchanged since park){suffix}"
        if status in until:
            return 0, f"{target} {status}{suffix}"
        if status in ("scheduled", "blocked") and "parked-agent" in until:
            evidence = task.get("wait_observation")
            if not isinstance(evidence, dict) or type(evidence.get("parked_agent")) is not bool:
                raise ValueError(f"{target} scheduled producer evidence unavailable; update owner core")
            if evidence["parked_agent"]:
                return 0, f"{target} {status} ({source}; orphan producer)"
        return 1, f"{target} {status}{suffix}"
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError) as exc:
        return 2, f"error: replica {gateway}: {exc}"
