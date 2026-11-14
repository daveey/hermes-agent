"""A pickup-stall alarm's run re-places exactly the cards it lists, off the stalled machine. [daveey]

t_01a1cea829121562 (2026-11-12): the alarm (:mod:`hermes_cli.kanban_pickup_stall`) about machine M runs on another
machine and tells its agent to re-place cards that cannot wait for M. That agent had no supported way to: worker
shells are fenced from ``hermes kanban move``, ``kanban_move(task_id=<other>)`` was granted only to a card's creator
task or its ``repin-owner`` supervisor, and the stalled cards were created by other cards or chats. The red sweep
re-placed them by hand.

The grant (:func:`refusal`) is the alarm's own run moving one target with ``kanban_move(task_id=<target>,
runs_on=["pool:worker"], reason=...)``. Every condition must hold, each read on the board the move would write:

* the caller is an open alarm the detector filed: ``created_by`` the detector, ``created_on`` M, idempotency key
  ``pickup-stall:<M>:...`` (M is read from the key, never from the arguments);
* the target is one the alarm lists: the card its per-card key names, or a ``- <id> (`` line in its body or in a
  detector comment (``_refresh_targets``);
* the target is still ready and unclaimed, with no current run, and still pinned ``machine:<M>`` (only off M);
* ``runs_on`` is exactly the pool queue ``[POOL_LABEL]`` (:data:`hermes_cli.kanban_placement.POOL_LABEL`) while
  some pool machine other than M can take the card (active, unheld, copies cards, carries the target's
  ``capability:<requires>``, ``profile:<assignee>`` and ``skill:<skills>``, the Board's ``requiredLabels``). A free
  slot is not required: the card waits in the single queue until any worker has one (t_01a1d95c41620e6c);
* or exactly one ``["machine:<X>"]``, X is not M, X is eligible now by the alarm placement rules, and the move
  carries a ``host_reason`` naming state on X (:mod:`hermes_cli.kanban_move_pool`). A capacity stall is a
  condition on M, not state on X: on 11-13 three alarm moves pinned published work to machine:worker-1/-4/-8 with
  no host reason, and two stalled again there.

The move itself is the ordinary queued move (``kanban_move.move_queued``) with its own guards. Nothing here reads
or writes a registry hold, so a hold is never lifted or shortened. A target another gateway created is moved through
the operator call (``kanban_operator_call``), and the same scope is checked on its request side for an alarm caller
(:func:`operator_refusal`).
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Mapping, Optional

_LISTED = re.compile(r"^- (t_[0-9A-Za-z_]+) \(", re.MULTILINE)
CLOSED = ("done", "archived")


def howto(own: str) -> str:
    """The supported re-place command, as alarm prose (``_tail`` and the hold text name it)."""
    pool = pool_label()
    return (f"To re-place a listed card off {own}, call the kanban_move TOOL from this alarm's run (not the shell: "
            f"`hermes kanban move` is fenced in worker shells): kanban_move(task_id=\"<listed card>\", "
            f"runs_on=[\"{pool}\"], reason=\"...\") queues it for any pool worker other than {own}: it waits in the "
            "single queue until one has a free slot. Never pin one machine for capacity: a stall is a condition on "
            f"{own}, not state on another machine, so runs_on=[\"machine:<X>\"] is refused without a host_reason "
            "naming state on X (a worktree, a stamp, unpublished work, a live process, Mac-only or hub-only work). "
            "When the card was created on another gateway, kanban_move answers foreign_card: send "
            "the same move as kanban_supervisor_request(ops=[{\"op\": \"operator\", \"action\": \"move\", "
            f"\"task_id\": \"<listed card>\", \"runs_on\": [\"{pool}\"], \"reason\": \"...\"}}]). Only the cards "
            f"this alarm lists, only while they are ready and pinned to {own}, and never back to {own}.")


def held_default_howto(own: str) -> str:
    """:func:`howto` for a ``held-default`` alarm (:mod:`hermes_cli.kanban_pickup_stall_held`): its targets are
    default-placement cards created on ``own``, not pins."""
    pool = pool_label()
    return (f"To re-place a listed card off {own}: the cards were created on {own}, so send "
            "kanban_supervisor_request(ops=[{\"op\": \"operator\", \"action\": \"move\", \"task_id\": \"<listed card>\", "
            f"\"runs_on\": [\"{pool}\"], \"reason\": \"{own} held; default placement stranded\"}}]); {own}'s "
            "dispatcher applies it (a hold stops claims, not operator calls). Only the cards this alarm lists, only "
            f"while they are ready, unclaimed and still default placement (runs_on unset), and never back to {own}.")


def is_held_default(alarm: Any, own: str) -> bool:
    """``alarm`` is the host-wide held-creator alarm about ``own`` (key ``pickup-stall:<own>:held-default:``)."""
    from hermes_cli import kanban_pickup_stall as kps
    from hermes_cli.kanban_pickup_stall_held import SLUG
    return (alarm["idempotency_key"] or "").startswith(f"{kps.KEY_PREFIX}{own}:{SLUG}:")


def _cols(row: Any) -> set:
    return set(row.keys()) if row is not None else set()


def alarm_own(alarm: Any) -> Optional[str]:
    """M for an open alarm the detector filed (``alarm``: a ``tasks`` row), else None."""
    from hermes_cli import kanban_pickup_stall as kps
    if alarm is None or alarm["created_by"] != kps.DETECTOR or alarm["status"] in CLOSED:
        return None
    key = alarm["idempotency_key"] or ""
    if not key.startswith(kps.KEY_PREFIX):
        return None
    own = key[len(kps.KEY_PREFIX):].split(":", 1)[0]
    if not own or ("created_on" in _cols(alarm) and alarm["created_on"] != own):
        return None
    return own


def listed_targets(conn: sqlite3.Connection, alarm: Any, own: str) -> set:
    """The cards ``alarm`` lists: its per-card key's card, and ``- <id> (`` lines its body or detector comments
    carry (comments another gateway wrote do not count)."""
    from hermes_cli import kanban_pickup_stall as kps
    rest = (alarm["idempotency_key"] or "")[len(kps.KEY_PREFIX) + len(own) + 1:]
    out = {rest.rsplit(":", 1)[0]}  # a per-card alarm's card (a host-wide slug names no card)
    out.update(_LISTED.findall(alarm["body"] or ""))
    try:
        has_origin = "origin_gateway" in {r[1] for r in conn.execute("PRAGMA table_info(task_comments)")}
        sql = ("SELECT body" + (", origin_gateway" if has_origin else "") + " FROM task_comments "
               "WHERE task_id = ? AND author = ?")
        for row in conn.execute(sql, (alarm["id"], kps.DETECTOR)).fetchall():
            if has_origin and row[1] not in (None, own):
                continue
            out.update(_LISTED.findall(row[0] or ""))
    except sqlite3.OperationalError:
        pass
    return out


def _requires(target: Any) -> list:
    import json
    try:
        requires = json.loads(target["requires"]) if target["requires"] else []
    except (TypeError, ValueError):
        requires = []
    return [r for r in requires if isinstance(r, str)] if isinstance(requires, list) else []


def _stored(target: Any, col: str) -> Any:
    """``target``'s stored ``col`` (NULL when the row has no such column, e.g. an older replica)."""
    return target[col] if col in _cols(target) else None


def _string_list(raw: Any) -> Optional[list]:
    """The Board's ``stringList`` (worker_placement.ts): NULL is ``[]``; anything else must parse as a JSON list of
    strings, else None (the Board then answers ``task_metadata_invalid`` and never pool-places the card)."""
    import json
    if raw is None:
        return []
    try:
        value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, list) and all(isinstance(r, str) for r in value) else None


def _malformed(target: Any, col: str) -> Optional[str]:
    raw = _stored(target, col)
    if _string_list(raw) is not None:
        return None
    return (f"its stored {col} {str(raw)[:80]!r} is not a JSON list of strings, so the Board's pool selection "
            f"answers task_metadata_invalid; fix the card's {col} or move it to one machine:<X>")


def malformed_requires(target: Any) -> Optional[str]:
    """Why the Board cannot read ``target``'s stored ``requires`` (:func:`_string_list`), else None.

    :func:`_requires` maps such a row to ``[]`` for the need, so this check is what keeps a pool move from looking
    eligible."""
    return _malformed(target, "requires")


def malformed_skills(target: Any) -> Optional[str]:
    """Why the Board cannot read ``target``'s stored ``skills``, else None: the Board's ``requiredLabels`` reads
    ``skills`` through the same ``stringList`` (t_01a1d2f66de283de)."""
    return _malformed(target, "skills")


def _need(target: Any) -> set:
    from hermes_cli import kanban_repin_notice
    from hermes_cli.kanban_placement_coordination import requirement_label
    # A NULL or empty assignee runs as ``default``, as in the Board's ``requiredLabels`` (``assignee || "default"``).
    return {requirement_label(r) for r in _requires(target)} | {kanban_repin_notice.profile_label(target["assignee"])}


def _pool_need(target: Any) -> set:
    """:func:`_need` plus the ``skill:<s>`` labels the Board's ``requiredLabels`` adds for a pool worker, built
    verbatim as the Board builds them. A machine:<X> move keeps :func:`_need`."""
    return _need(target) | {f"skill:{s}" for s in (_string_list(_stored(target, "skills")) or [])}


def eligible_now(name: str, own: str, machines: Optional[Mapping[str, Mapping[str, Any]]], need: set,
                 now_ms: int) -> Optional[str]:
    """None when ``name`` may take a card off ``own`` now (``kanban_pickup_stall`` placement rules), else why not."""
    from hermes_cli import kanban_pickup_stall as kps
    if machines is None:
        return "the machine registry is unreadable"
    entry = machines.get(name) or {}
    if not kps._takes_work(name, entry, own, need):
        return (f"{name} cannot take it (it is {own}, not registered, retired, held, takes no pickup, or lacks "
                f"{', '.join(sorted(need))})")
    if not kps._ready_now(entry, now_ms):
        return f"{name} is not eligible now (draining, not seen recently, or no free card slot)"
    return None


def scope_refusal(conn: sqlite3.Connection, alarm: Any, target: Any, runs_on: Any, *,
                  machines: Optional[Mapping[str, Mapping[str, Any]]], now_ms: int,
                  host_reason: Optional[str] = None) -> Optional[str]:
    """None when ``alarm``'s run may move ``target`` (rows, ``conn`` holds the alarm's comments) to ``runs_on``."""
    from hermes_cli import kanban_worker_node as wn
    own = alarm_own(alarm)
    if own is None:
        return "not an open pickup-stall alarm filed by the detector"
    if target is None or target["id"] not in listed_targets(conn, alarm, own):
        return f"the target is not a card this alarm about {own} lists"
    cols = _cols(target)
    if target["status"] != "ready" or any(target[c] for c in ("claim_lock", "lease_holder", "current_run_id")
                                          if c in cols):
        return f"{target['id']} is {target['status']} or claimed; only a ready, unclaimed card is re-placed"
    pins = wn._parse(target["runs_on"])
    if f"machine:{own}" not in (pins or []):
        # A held-default alarm lists default-placement cards created on its machine (kanban_pickup_stall_held).
        if not (is_held_default(alarm, own) and pins is None and _stored(target, "created_on") == own):
            return f"{target['id']} is no longer pinned to machine:{own}"
    try:
        labels = list(wn.normalize_runs_on(runs_on) or [])
    except ValueError as exc:
        return str(exc)
    if labels == [pool_label()]:
        # A worker may attest ssh access, but the Board's pool selection cannot place it, nor a requires or skills it
        # cannot read (not a JSON list of strings): the card would sit in task_requirement_unsupported or
        # task_metadata_invalid instead of leaving the stalled machine. A pool worker must also carry skill:<s>.
        from hermes_cli.kanban_placement_coordination import board_refusal
        if why := malformed_requires(target) or malformed_skills(target) or board_refusal(_requires(target)):
            return f"{target['id']} cannot go to {pool_label()}: {why}"
        return pool_refusal(own, machines, _pool_need(target), now_ms)
    if len(labels) != 1 or not labels[0].startswith("machine:"):
        return f"runs_on must be exactly [\"{pool_label()}\"] or exactly one machine:<X> label"
    name = labels[0].split(":", 1)[1]
    if name == own:
        return f"the card must leave {own}"
    from hermes_cli import kanban_move_pool as kmp
    reason = (host_reason or "").strip()
    if not reason:
        pooled = bool(kmp.pool_pins(labels, machines or {}))
        return f"{kmp.host_reason_message(name, pooled=pooled)} For a stall on {own} that is the pool queue."
    if kmp.capacity_only(reason):
        return f"host_reason {reason[:200]!r} cites capacity, not state on {name}; use runs_on=[\"{pool_label()}\"]"
    return eligible_now(name, own, machines, _need(target), now_ms)


def pool_label() -> str:
    from hermes_cli.kanban_placement import POOL_LABEL
    return POOL_LABEL


def pool_refusal(own: str, machines: Optional[Mapping[str, Mapping[str, Any]]], need: set,
                 now_ms: int) -> Optional[str]:
    """None when a pool machine other than ``own`` can take the card (active, unheld, copies cards, carries
    ``need``), else why not. A free slot now is not required: the card waits in the single queue until any worker
    has one (t_01a1d95c41620e6c). Requiring one sent the alarm to ``machine:<X>`` pins that stalled again on X.
    ``now_ms`` is kept for callers; eligibility here is structural."""
    from hermes_cli import kanban_pickup_stall as kps
    label = pool_label()
    if machines is None:
        return "the machine registry is unreadable"
    pooled = sorted(n for n, e in machines.items() if n != own and label in ((e or {}).get("labels") or []))
    if any(kps._takes_work(name, machines.get(name) or {}, own, need) for name in pooled):
        return None
    return (f"no {label} machine other than {own} can take it"
            + (f" ({', '.join(pooled)}: retired, held, takes no pickup, or lacks {', '.join(sorted(need))})"
               if pooled else " (none carries the label)")
            + f"; the card stays on {own} until a pool worker carries those labels")


def _registry_machines() -> Optional[Mapping[str, Mapping[str, Any]]]:
    from hermes_cli import kanban_worker_node as wn
    reg = wn.registry()
    return None if reg is None else (reg.get("machines") or {})


def refusal(conn: sqlite3.Connection, task_id: str, env_task: str, runs_on: Any, *,
            machines: Any = False, now_ms: Optional[int] = None, host_reason: Optional[str] = None) -> Optional[str]:
    """None when ``env_task`` (the caller's alarm) may move local card ``task_id`` to ``runs_on``; else why not."""
    from hermes_cli import kanban_pickup_stall as kps
    alarm = conn.execute("SELECT * FROM tasks WHERE id = ?", (env_task,)).fetchone()
    if alarm_own(alarm) is None:
        return "not an open pickup-stall alarm filed by the detector"
    target = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    from hermes_cli import kanban_capability_routing as kcr
    if target is None or kcr.card_home(conn, task_id) is not None:  # absent, or only a pickup copy here
        return (f"{task_id} is not on this gateway's board; a card another gateway created is moved with "
                "kanban_supervisor_request(ops=[{op: 'operator', action: 'move', task_id, runs_on, reason}])")
    return scope_refusal(conn, alarm, target, runs_on,
                         machines=_registry_machines() if machines is False else machines,
                         now_ms=kps._now_ms() if now_ms is None else now_ms, host_reason=host_reason)


def move(conn: sqlite3.Connection, task_id: str, env_task: str, *, runs_on: Any, reason: str,
         machines: Any = False, hub: Any = None, host_reason: Optional[str] = None) -> dict[str, Any]:
    """The granted move (``move_queued``'s result), re-checking the scope first."""
    from hermes_cli import kanban_move as km
    from hermes_cli import kanban_worker_node as wn
    pool = _registry_machines() if machines is False else machines
    why = refusal(conn, task_id, env_task, runs_on, machines=pool, host_reason=host_reason)
    if why is not None:
        return {"ok": False, "reason_code": "pickup_stall_scope", "error": why}
    alarm = conn.execute("SELECT * FROM tasks WHERE id = ?", (env_task,)).fetchone()
    return km.move_queued(conn, task_id, runs_on=list(wn.normalize_runs_on(runs_on) or []), reason=reason,
                          machines=pool, hub=hub, host_reason=host_reason,
                          event_extra={"pickup_stall_alarm": env_task, "stalled_machine": alarm_own(alarm)})


def operator_refusal(conn: sqlite3.Connection, rep: Optional[sqlite3.Connection], requester: str,
                     op: Mapping[str, Any], *, machines: Any = False, now_ms: Optional[int] = None) -> Optional[str]:
    """Request side of an operator-call move: None for a caller that is no alarm (no change), else the alarm
    scope's refusal for the foreign target as the replica shows it."""
    from hermes_cli import kanban_pickup_stall as kps
    if op.get("action") != "move":
        return None
    alarm = conn.execute("SELECT * FROM tasks WHERE id = ?", (requester,)).fetchone()
    if alarm_own(alarm) is None:
        return None
    target = None
    if rep is not None:
        try:
            target = rep.execute("SELECT * FROM tasks WHERE id = ?", (op.get("task_id"),)).fetchone()
        except sqlite3.OperationalError:
            target = None
    return scope_refusal(conn, alarm, target, op.get("runs_on"),
                         machines=_registry_machines() if machines is False else machines,
                         now_ms=kps._now_ms() if now_ms is None else now_ms, host_reason=op.get("host_reason"))
