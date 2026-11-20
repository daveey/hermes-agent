"""A running card moves itself to another machine: ``kanban_move`` / ``hermes kanban move``. [daveey]

A move is a ``kanban_continue`` whose re-queue also rewrites the card's ``runs_on``. In one write transaction it
saves the checkpoint, ends this run as ``continued``, writes the new labels and appends a ``moved`` event. The
card goes back to ``ready``; when the holder pushes it out of ``running`` the Board ends its lease (worker
``rows.ts``), and the Board's grant checks ``runs_on`` labels for every claimer, the creator included
(``checkPlacement``), so only a matching machine runs it next. Nothing runs twice: the run ends before the lease
does, and the lease CAS admits one claimer.

Guards:

* the target labels must be satisfiable now by an active, unheld registry machine; a running card must leave
  this machine, while a queued card may target it (``machine:<x>`` for an unregistered x never is);
* ``machine:<x>`` needs x to advertise pickup (``placement_unavailable`` otherwise: x would never copy the card,
  :func:`hermes_cli.kanban_worker_node.check_machine_pins`);
* ``kanban.move.max_moves`` (default 3) moves per card, except a queued card's move off a retired ``machine:<x>`` pin
  (:func:`dead_pin`: x absent from, or revoked in, a successful registry read); that move still counts, and its
  event carries ``dead_pin``;
* moving back to a machine the card already left is ping-pong: refused unless ``force``, and it still counts;
* ``dir`` workspaces exist only on their creator, so those cards never move; a ``worktree`` card moves only with
  portable workspace_source custody (published branch, :mod:`hermes_cli.kanban_source_move`);
* the card's ``requires`` must fit the assignee's profile on at least one matching machine whose profile
  tools are known or unknown; every match known to lack one refuses ``capability_mismatch``, naming the
  machine and the missing tool (:mod:`hermes_cli.kanban_runs_on_capabilities`);
* a queued card's move that drops a ``capability:`` label is refused ``capacity_wait`` unless ``force`` while matching
  machines claimed cards needing its current labels in the last 30 min (:mod:`hermes_cli.kanban_label_backlog`): the
  card is queued behind them, and a machine without the label cannot do the work (kanban-b962867f17).
* the board must sync to the Board service (``runs_on`` is a Board placement field).
* a target must attest ``capability:host_repo`` when required and ``skill:<pin>`` for every pinned skill,
  in addition to satisfying the assignee's existing profile-tool check.
* a reason claiming ship/landing custody needs ``host_reason`` (``ship_custody_portable``): ship lands from every
  machine (:mod:`hermes_cli.kanban_move_ship`).
* a card leaving this host (no match is this host) while a tree of it here is dirty or has commits on no remote is
  refused ``unpushed_state_here`` unless ``force`` (then the event carries ``left_unpushed_state``). Its trees are
  :func:`hermes_cli.kanban_move_pool.card_trees`: the workspace, owner-marked linked worktrees (``~/code/_wt/<slug>``)
  and linked worktrees named in the body, the newest comment, the newest checkpoint or the move's own checkpoint
  (a text-named tree whose owner marker names another card is that card's work, not this one's). On 11-11 a hub
  card whose ``~/code/_wt`` trees held unpushed commits was moved to worker-2, which could not reach them and moved
  it back. A worker-first card's Board repin to the worker queue takes the same guard, by the same leaving rule
  (:func:`leaves_here`, matches from the registry). Limitations: only the host
  holding the trees can see them, so a move issued from another host is blind to them; and a move whose matches
  still include this host (``pool:worker`` issued on a pool worker, ``linux``) is not "leaving" and is not
  guarded, though another matching host may claim the card (intentional: this host may still run it). The same
  holds for a worker-first repin to ``[]`` or ``[pool:worker]`` whose origin is an active, unheld registry machine
  carrying those labels (:func:`_repin_leaves`): it skips the unpushed guard, as :func:`check` would.

Every move is a ``moved`` event ``{from_machine, runs_on, previous_runs_on, reason, move_no, forced}``: the audit log.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
from typing import Any, Mapping, Optional

DEFAULT_MAX_MOVES = 3
MOVED_EVENT = "moved"
UNPUSHED_HERE = "unpushed_state_here"


class MoveRefused(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _SelectionChanged(Exception):
    """Raised inside the move's ending transaction to roll it back (the portable receipt went stale)."""


def max_moves(cfg: Optional[dict] = None) -> int:
    if cfg is None:
        try:
            from hermes_cli.config import load_config_readonly
            cfg = ((load_config_readonly() or {}).get("kanban") or {}).get("move") or {}
        except Exception:
            cfg = {}
    try:
        return max(1, int((cfg or {}).get("max_moves", DEFAULT_MAX_MOVES)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_MOVES


def history(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
                        (task_id, MOVED_EVENT)).fetchall()
    out = []
    for (payload,) in rows:
        try:
            out.append(json.loads(payload) if payload else {})
        except ValueError:
            out.append({})
    return out


def matching_machines(runs_on: list[str], machines: Mapping[str, Mapping[str, Any]], *, me: str,
                      exclude_me: bool = True) -> list[str]:
    """Active, unheld registry machines carrying every label; running moves exclude ``me``.

    A machine that has published ``metrics.claiming == 0`` is not taking portable cards. A pin to
    that machine (``machine:<name>`` in ``runs_on``) stays, because a batch or worktree may be there.
    """
    from hermes_cli import kanban_worker_node as wn
    out = []
    for name, entry in machines.items():
        if (exclude_me and name == me) or not entry or entry.get("state", "active") != "active" or entry.get("hold"):
            continue
        if _not_claiming(entry) and f"machine:{name}" not in runs_on:
            continue
        if set(runs_on) <= wn.effective_labels(entry, name):
            out.append(name)
    return sorted(out)


def _not_claiming(entry: Mapping[str, Any]) -> bool:
    metrics = entry.get("metrics")
    return isinstance(metrics, dict) and metrics.get("claiming") == 0


def _requirement_matches(matches, machines, requires, skills, *, me=None, assignee=None):
    """Move-only attestation gate, using the Board worker contract's capability/skill labels.

    Profile tools retain their existing known/unknown policy; host checkouts and pins instead need
    affirmative labels. Filter first so separate machines cannot satisfy separate halves of the check.
    """
    from hermes_cli import kanban_worker_node as wn
    required = ({"capability:host_repo"} if "host_repo" in requires else set())
    required.update(f"skill:{pin}" for pin in skills)
    lacking = {name: sorted(required - wn.effective_labels(machines[name], name)) for name in matches}
    if me in matches and required and assignee:
        # The origin need not advertise worker-pickup labels. Read its actual profile rather than
        # rejecting a return home because the worker registry omits local checkout/skill inventory.
        from hermes_cli import kanban_capabilities as caps
        available = set()
        try:
            available = {f"capability:{c}" for c in caps._profile_capabilities(assignee)}
            with caps._profile_policy(assignee):
                available.update(f"skill:{pin}" for pin in skills if caps._skill_frontmatter(pin) is not None)
        except (OSError, ValueError):
            available = set()  # unknown local inventory cannot veto a capable remote candidate
        lacking[me] = sorted(required - available)
    capable = [name for name in matches if not lacking[name]]
    if not capable:
        detail = "; ".join(f"machine {name} lacks {', '.join(missing)}" for name, missing in lacking.items())
        raise MoveRefused("capability_mismatch", f"No move target satisfies required worker labels: {detail}")
    return capable


def check(conn: sqlite3.Connection, task_id: str, runs_on: Any, *, force: bool = False,
          machines: Optional[Mapping[str, Mapping[str, Any]]] = None, me: Optional[str] = None,
          cfg: Optional[dict] = None, host_reason: Optional[str] = None,
          reason: Optional[str] = None, checkpoint: Optional[str] = None,
          fresh: Optional[tuple] = None) -> dict[str, Any]:
    """Validate a move; returns ``{runs_on, previous, matches, move_no, forced, me, host}`` or raises
    :class:`MoveRefused`. ``host`` holds the event fields of a pin to one pool worker (:mod:`kanban_move_pool`).
    A ``reason`` claiming ship custody needs ``host_reason`` (:mod:`kanban_move_ship`). ``checkpoint`` is the
    move's own, not yet written checkpoint: scanned for tree paths with the card's stored text. ``fresh`` is a running
    card's just-published ``(manifest, custody)`` (workspace_source, v2 §7)."""
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_move_ship as kms
    from hermes_cli import kanban_source_move as ksm
    from hermes_cli import kanban_worker_node as wn
    try:
        labels = wn.normalize_runs_on(runs_on)
    except ValueError as exc:
        raise MoveRefused("invalid_runs_on", str(exc)) from exc
    if not labels:
        raise MoveRefused("invalid_runs_on", "runs_on must name at least one label, e.g. ['machine:worker-1']")
    ship_custody = kms.refusal(reason, host_reason)
    if ship_custody:
        raise MoveRefused(kms.SHIP_CUSTODY_REFUSED, ship_custody)
    row = conn.execute("SELECT status, workspace_kind, workspace_path, runs_on, assignee, requires, skills, "
                       "model_override, provider_override, model_pin_mandatory, workspace_source, workspace_custody "
                       "FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise MoveRefused("not_found", f"task {task_id} not found")
    if sync.board_mode(conn) != "primary":
        if row["workspace_kind"] in ("dir", "worktree"):
            raise MoveRefused("workspace_bound", f"a {row['workspace_kind']} card runs only where its workspace is")
        raise MoveRefused("not_synced", "runs_on needs a board that syncs to the Board service (kanban.sync)")
    me = me or str(sync._state(conn)["gateway_id"])
    if bound := ksm.parked_refusal(conn, task_id, me=me, fresh=fresh):
        raise MoveRefused(*bound)
    from hermes_cli.kanban_mandatory_pin import carry
    # A mandatory pin keeps its attestation label through every move (agent, operator, pickup-stall alarm):
    # [pool:worker] alone would let an old core lease it; machine:<x> needs x to attest it. [daveey]
    labels = carry(labels, dict(row), me=me)
    from hermes_cli import kanban_label_backlog as klb
    if machines is None:
        reg = wn.registry(require_fresh=True)
        if reg is None:
            raise MoveRefused("registry_unavailable", "the Board machine registry is unreachable; try again")
        machines = reg.get("machines") or {}
    pool: Mapping[str, Mapping[str, Any]] = machines or {}
    capacity_wait = klb.move_refusal(conn, task_id, row["status"], wn._parse(row["runs_on"]), labels, machines=pool)
    if capacity_wait and not force:
        raise MoveRefused(klb.MOVE_REFUSED, capacity_wait)
    exclude_me = row["status"] == "running"
    matches = matching_machines(labels, pool, me=me, exclude_me=exclude_me)
    if not matches:
        other = f" other than {me}" if exclude_me else ""
        raise MoveRefused("no_machine", f"no active, unheld machine{other} has every label in {labels}")
    try:
        wn.check_machine_pins(conn, labels, machines=pool, me=me)
    except wn.MachinePinRefused as exc:
        raise MoveRefused(exc.code, str(exc)) from exc
    from hermes_cli import kanban_move_pool as kmp
    host = kmp.check(labels, pool, me=me, running=exclude_me, previous=wn._parse(row["runs_on"]),
                     workspace_path=row["workspace_path"], host_reason=host_reason, force=force, refused=MoveRefused,
                     task_id=task_id)
    from hermes_cli import kanban_runs_on_capabilities as roc
    try:
        requires = json.loads(row["requires"]) if row["requires"] else []
    except ValueError:
        requires = []
    source_card = {**dict(row), **({"workspace_source": fresh[0], "workspace_custody": fresh[1]} if fresh else {})}
    requires = ksm.effective_requires(source_card, requires)  # a portable worktree needs no host checkout
    matches = _requirement_matches(matches, pool, requires, wn._parse(row["skills"]) or [],
                                   me=me, assignee=row["assignee"])
    try:
        roc.check(labels, assignee=row["assignee"], requires=requires, workspace_kind=ksm.capability_kind(source_card),
                  me=me, machines={m: pool[m] for m in matches}, exclude_me=exclude_me)
    except roc.PlacementCapabilityMismatch as exc:
        raise MoveRefused("capability_mismatch", str(exc)) from exc
    past, host = _spend_budget(conn, task_id, row, pool, me=me, cfg=cfg, host=host)
    left = {p.get("from_machine") for p in past}
    back = [m for m in matches if m in left]
    if back and len(back) == len(matches) and not force:
        raise MoveRefused("ping_pong", f"the card already left {', '.join(back)}; pass force to move it back")
    if leaves_here(me, matches):  # git reads: check() runs before every caller's write transaction
        if state := unpushed_here(conn, task_id, row["workspace_path"], me=me, force=force, checkpoint=checkpoint):
            host = {**host, "left_unpushed_state": state}
    if capacity_wait:  # forced past the capacity-wait guard: the audit keeps why it was a queue, not a fault
        host = {**host, "capacity_wait_overridden": capacity_wait[:500]}
    return {"runs_on": labels, "previous": wn._parse(row["runs_on"]), "matches": matches, "move_no": len(past) + 1,
            "forced": bool(back and force), "me": me, "host": host}


def _spend_budget(conn: sqlite3.Connection, task_id: str, row: Any, pool: Mapping[str, Mapping[str, Any]], *,
                  me: str, cfg: Optional[dict], host: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The card's move history and event fields, or ``move_limit``. A queued card pinned to a retired machine can
    never be leased again: moving it off is not a bounce, and the cap must not strand it (t_01a1a1dea9c64d6a on
    worker-6). That move still counts, so later moves stay capped."""
    from hermes_cli import kanban_worker_node as wn
    past = history(conn, task_id)
    limit = max_moves(cfg)
    if len(past) < limit:
        return past, host
    dead = None if row["status"] == "running" else dead_pin(wn._parse(row["runs_on"]), pool, me=me)
    if dead is None:
        raise MoveRefused("move_limit", f"card already moved {len(past)} time(s) (kanban.move.max_moves {limit}); "
                                        "finish it here or block it")
    return past, {**host, "dead_pin": dead}


def dead_pin(previous: Optional[list[str]], machines: Mapping[str, Mapping[str, Any]], *, me: str) -> Optional[str]:
    """The retired machine a card is pinned to, or None. Exactly one ``machine:<x>`` label, x not ``me``, and a
    non-empty registry read in which x is absent or ``revoked``. ``GET /v2/machines`` lists active gateways only, so
    absence from a successful read is the Board's own word that no key for x can lease the card. Held, stale or busy
    machines are listed and so never dead; an unreadable registry never gets here (``registry_unavailable``)."""
    names = [label.removeprefix("machine:") for label in previous or [] if label.startswith("machine:")]
    if not machines or len(names) != 1 or names[0] == me:
        return None
    entry = machines.get(names[0])
    return names[0] if not entry or entry.get("state") == "revoked" else None


def leaves_here(me: str, matches: list[str]) -> bool:
    """Whether a move leaves ``me`` (and so takes the unpushed-state guard): no machine that matches it is ``me``.
    A running move's matches never include ``me``. The one rule :func:`check` and the worker-first repin share."""
    return me not in matches


def _repin_leaves(labels: list[str], me: str, machines: Optional[Mapping[str, Mapping[str, Any]]]) -> bool:
    """:func:`leaves_here` for a worker-first Board repin, which skips :func:`check`: matches come from the
    registry with ``me`` included (the card is queued). A repin to exactly ``machine:<me>`` stays (the origin need
    not be registered); an unreadable registry counts as leaving, so the guard runs (fail safe)."""
    from hermes_cli import kanban_worker_node as wn
    if labels == [f"machine:{me}"]:
        return False
    pool: Mapping[str, Mapping[str, Any]] = machines or {}
    if machines is None:
        reg = wn.registry()
        if reg is None:
            return True
        pool = reg.get("machines") or {}
    return leaves_here(me, matching_machines(labels, pool, me=me, exclude_me=False))


def unpushed_here(conn: sqlite3.Connection, task_id: str, workspace_path: Optional[str], *, me: str, force: bool,
                  checkpoint: Optional[str] = None) -> Optional[str]:
    """The card's unpushed state on this host (to record as ``left_unpushed_state`` under ``force``) or None;
    without ``force`` a state raises ``unpushed_state_here``. Runs git: call it outside any write transaction."""
    from hermes_cli import kanban_move_pool as kmp
    _trees, state = kmp.card_trees_state(task_id, workspace_path, _card_text(conn, task_id, checkpoint),
                                         linked_text_only=True)
    if state and not force:
        raise MoveRefused(UNPUSHED_HERE, (
            f"{state} on {me}: another machine cannot resume it. Push the branch (or let the card's run publish "
            "it) and move again, or pass force to leave it"))
    return state


def _card_text(conn: sqlite3.Connection, task_id: str, checkpoint: Optional[str] = None) -> str:
    """Body, newest comment, newest checkpoint and the move's own ``checkpoint``: where a card names its
    ``~/code/_wt/<slug>`` trees."""
    parts = []
    for sql in ("SELECT body FROM tasks WHERE id = ?",
                "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id DESC LIMIT 1"):
        try:
            row = conn.execute(sql, (task_id,)).fetchone()
        except sqlite3.Error:
            row = None
        parts.append(str(row[0] or "") if row else "")
    try:
        row = conn.execute("SELECT metadata, summary FROM task_runs WHERE task_id = ? AND outcome = 'continued' "
                           "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    except sqlite3.Error:
        row = None
    if row:
        try:
            meta = json.loads(row[0]) if row[0] else {}
        except ValueError:
            meta = {}
        parts.append(str((meta.get("checkpoint") if isinstance(meta, dict) else None) or row[1] or ""))
    parts.append(checkpoint or "")
    return "\n".join(parts)


def move_task(conn: sqlite3.Connection, task_id: str, *, runs_on: Any, reason: str, checkpoint: str,
              expected_run_id: Optional[int] = None, force: bool = False,
              machines: Optional[Mapping[str, Mapping[str, Any]]] = None, me: Optional[str] = None,
              cfg: Optional[dict] = None, host_reason: Optional[str] = None) -> dict[str, Any]:
    """Move a RUNNING card: ``{"ok": True, "runs_on", "matches", "move_no", "run_id", "status"}`` or
    ``{"ok": False, "error", "reason_code"}``."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_worker_node as wn
    from hermes_cli.kanban_continuation import continue_task
    reason = (reason or "").strip()
    if not reason:
        return {"ok": False, "reason_code": "reason_required", "error": "reason is required (why this card must move)"}
    from hermes_cli.kanban_hub_admission_placement import gate_move
    if (refused := gate_move(conn, task_id, runs_on)) is not None:
        return refused  # a hub target must convince the admission judge first [daveey]
    from hermes_cli import kanban_source_move as ksm
    from hermes_cli import kanban_source_receipt as carrier
    receipt, fresh, code = ksm.publish_for_move(conn, task_id)  # workspace_source: publish, then check (v2 §7)
    if code:
        return {"ok": False, "reason_code": code, "error": f"the worktree could not be published ({code}); the run "
                                                          "stays here"}
    try:
        plan = check(conn, task_id, runs_on, force=force, machines=machines, me=me, cfg=cfg, host_reason=host_reason,
                     reason=reason, checkpoint=checkpoint, fresh=fresh)
    except MoveRefused as exc:
        return {"ok": False, "reason_code": exc.code, "error": str(exc)}

    # A worker-first card's runs_on is Board-owned: a local write is stripped from the push
    # (kanban_worker_placement.acknowledged_outbox) and the origin would just reserve it for the pool again. The
    # moved event is the request; the origin applies it with the Board's creator repin (worker_placement.reevaluate).
    policy = conn.execute("SELECT placement_policy FROM tasks WHERE id = ?", (task_id,)).fetchone()
    requested = bool(policy and policy[0])

    def _in_txn(c: sqlite3.Connection, run_id: int) -> None:
        # The ending's receipt fence may have downgraded the portable receipt that chose this placement.
        from hermes_cli.kanban_source_artifacts import receipt_selection_current
        if receipt and not receipt_selection_current(c, task_id, run_id, receipt):
            raise _SelectionChanged()
        if not requested:
            wn.set_runs_on(c, task_id, plan["runs_on"])
        kb._append_event(c, task_id, MOVED_EVENT, {
            "from_machine": plan["me"], "runs_on": plan["runs_on"], "previous_runs_on": plan["previous"],
            "reason": reason[:500], "move_no": plan["move_no"], "forced": plan["forced"],
            "matches": plan["matches"], **plan["host"], **({"repin_requested": True} if requested else {})},
            run_id=run_id)

    note = f"Moving to {plan['runs_on']} ({', '.join(plan['matches'])}): {reason}"
    run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
    with carrier.staged(task_id, run_id, receipt) if receipt else contextlib.nullcontext():
        try:
            res = continue_task(conn, task_id, checkpoint=f"{checkpoint.strip()}\n\n[{note}]",
                                expected_run_id=expected_run_id, in_txn=_in_txn)
        except _SelectionChanged:
            return {"ok": False, "reason_code": "artifact_selection_changed",
                    "error": "the artifact selection changed after publishing; the run stays here, retry the move"}
    if not res.get("ok"):
        return {"ok": False, "reason_code": res.get("reason_code") or "not_running", "error": res.get("error")}
    out = {"ok": True, "runs_on": plan["runs_on"], "matches": plan["matches"], "move_no": plan["move_no"],
           "run_id": res.get("run_id"), "status": res.get("status")}
    if requested:
        out["repin"] = "requested"
        out["note"] = ("worker-first card: its origin applies this move with the Board repin on its next placement "
                       "tick (the policy is then cleared and runs_on set)")
    return out


def move_queued(conn: sqlite3.Connection, task_id: str, *, runs_on: Any, reason: str, checkpoint: str = "", force: bool = False,
                machines: Optional[Mapping[str, Mapping[str, Any]]] = None, me: Optional[str] = None,
                cfg: Optional[dict] = None, hub: Any = None, host_reason: Optional[str] = None,
                event_extra: Optional[dict] = None) -> dict[str, Any]:
    """Move a card that is NOT running (ready/todo/scheduled/blocked/triage): same guards, no run to end.
    A worker-first policy card instead takes the explicit Board repin path (``hub`` is for tests).
    ``event_extra`` is merged into the ``moved`` event (audit provenance, e.g. an operator call's uid)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as wn
    reason = (reason or "").strip()
    if not reason:
        return {"ok": False, "reason_code": "reason_required", "error": "reason is required (why this card must move)"}
    from hermes_cli.kanban_hub_admission_placement import gate_move
    if (refused := gate_move(conn, task_id, runs_on)) is not None:
        return refused  # a hub target must convince the admission judge first [daveey]
    from hermes_cli import kanban_worker_repin as repin
    if repin.applies(conn, task_id):
        # A worker-first card's placement is Board-owned: a local relabel would be stripped, so only the Board's
        # explicit repin (to the worker queue, or to the origin) is supported (kanban_worker_repin).
        try:
            labels = wn.normalize_runs_on(runs_on)
        except ValueError as exc:
            return {"ok": False, "reason_code": "invalid_runs_on", "error": str(exc)}
        extra: dict[str, Any] = {}  # operator provenance rides a marker (kanban_operator_call), not this event
        origin = (sync.sync_state(conn) or {}).get("gateway_id")
        if origin and _repin_leaves(list(labels or []), str(origin), machines):  # the check() leaving rule
            row = conn.execute("SELECT workspace_path FROM tasks WHERE id = ?", (task_id,)).fetchone()
            try:
                if state := unpushed_here(conn, task_id, row["workspace_path"] if row else None, me=str(origin),
                                          force=force, checkpoint=checkpoint):
                    extra["event_extra"] = {"left_unpushed_state": state}
            except MoveRefused as exc:
                return {"ok": False, "reason_code": exc.code, "error": str(exc)}
        return repin.repin_to_origin(conn, task_id, list(labels or []), reason, hub=hub, **extra)
    try:
        plan = check(conn, task_id, runs_on, force=force, machines=machines, me=me, cfg=cfg, host_reason=host_reason,
                     reason=reason, checkpoint=checkpoint)
    except MoveRefused as exc:
        return {"ok": False, "reason_code": exc.code, "error": str(exc)}
    with kb.write_txn(conn):
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row["status"] in ("running", "done", "archived"):
            return {"ok": False, "reason_code": "bad_status",
                    "error": f"task {task_id} is {row['status']}" + (" (use --reclaim, or kanban_move from its run)"
                                                                   if row["status"] == "running" else "")}
        wn.set_runs_on(conn, task_id, plan["runs_on"])
        kb._append_event(conn, task_id, MOVED_EVENT, {
            "from_machine": plan["me"], "runs_on": plan["runs_on"], "previous_runs_on": plan["previous"],
            "reason": reason[:500], "move_no": plan["move_no"], "forced": plan["forced"],
            "matches": plan["matches"], "queued": True, "checkpoint": checkpoint.strip(), **plan["host"],
            **(event_extra or {})})
        if checkpoint.strip():
            kb.add_comment(conn, task_id, "kanban-operator", f"Move checkpoint:\n\n{checkpoint.strip()}")
    return {"ok": True, "runs_on": plan["runs_on"], "matches": plan["matches"], "move_no": plan["move_no"],
            "status": row["status"]}


def move_reclaim(conn: sqlite3.Connection, task_id: str, *, runs_on: Any, reason: str, force: bool = False,
                 machines: Optional[Mapping[str, Mapping[str, Any]]] = None, me: Optional[str] = None,
                 cfg: Optional[dict] = None, host_reason: Optional[str] = None) -> dict[str, Any]:
    """Operator move of a card running on THIS gateway (``hermes kanban move --reclaim``): write the new labels
    first (``runs_on`` is not lease-fenced), then reclaim the run (stops its worker) and release the Board lease.
    Labels first means a re-claim between the two steps already sees the new placement."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_worker_node as wn
    reason = (reason or "").strip()
    if not reason:
        return {"ok": False, "reason_code": "reason_required", "error": "reason is required (why this card must move)"}
    from hermes_cli.kanban_hub_admission_placement import gate_move
    if (refused := gate_move(conn, task_id, runs_on)) is not None:
        return refused  # a hub target must convince the admission judge first [daveey]
    from hermes_cli.kanban_source_move import reclaim_refusal
    if code := reclaim_refusal(conn, task_id):  # a reclaim is involuntary: it cannot publish (v2 §7)
        return {"ok": False, "reason_code": code,
                "error": "a worktree/source card cannot be reclaimed to move: move it from its run (kanban_move), "
                         "or let it park and move it then"}
    try:
        plan = check(conn, task_id, runs_on, force=force, machines=machines, me=me, cfg=cfg, host_reason=host_reason,
                     reason=reason)
    except MoveRefused as exc:
        return {"ok": False, "reason_code": exc.code, "error": str(exc)}
    row = conn.execute("SELECT t.status, t.claim_lock, t.placement_policy, r.uid AS run_uid FROM tasks t "
                       "LEFT JOIN task_runs r "
                       "ON r.id = t.current_run_id WHERE t.id = ?", (task_id,)).fetchone()
    if row["status"] != "running" or row["claim_lock"] is None:
        return {"ok": False, "reason_code": "not_running_here",
                "error": f"task {task_id} is not running under a claim on this gateway"}
    requested = bool(row["placement_policy"])  # Board-owned runs_on: the origin repins it (see move_task)
    with kb.write_txn(conn):
        if not requested:
            wn.set_runs_on(conn, task_id, plan["runs_on"])
        kb._append_event(conn, task_id, MOVED_EVENT, {
            "from_machine": plan["me"], "runs_on": plan["runs_on"], "previous_runs_on": plan["previous"],
            "reason": reason[:500], "move_no": plan["move_no"], "forced": plan["forced"],
            "matches": plan["matches"], "reclaimed": True, **plan["host"],
            **({"repin_requested": True} if requested else {})})
    if not kb.reclaim_task(conn, task_id, reason=f"moved to {plan['runs_on']}: {reason}"[:400]):
        return {"ok": False, "reason_code": "reclaim_failed",
                "error": f"labels set to {plan['runs_on']} but the run could not be reclaimed; it ends on its own"}
    sync.release_lease(conn, task_id, row["run_uid"])
    status = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()["status"]
    out = {"ok": True, "runs_on": plan["runs_on"], "matches": plan["matches"], "move_no": plan["move_no"],
           "status": status}
    if requested:
        out["repin"] = "requested"
    return out
