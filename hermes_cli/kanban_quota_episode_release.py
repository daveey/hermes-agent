"""``hermes kanban quota-release``: an operator ends a card's quota episode. [daveey]

2026-11-12 (t_01a1cfac4046a296): during a revoked-token outage every usable Claude account failed with
401, so worker startup raised ``SubscriptionQuotaUnavailable ... resets at <the earliest reset of the
accounts genuinely at 100%>``. That hint became a hinted episode, and ``decision`` trusted it until the
deadline: 10+ ready cards were held 1-6 days while anthropic had room again.

The automatic path is ``kanban_quota_episode._disproved`` (PR #1409): a hinted wall the live OAuth pool
contradicts keeps only its doubling backoff. This module is the operator escape for what that check cannot
see (an unreadable or empty pool, a hint the operator knows is wrong): ``quota-release <id> [--reason]``
appends a ``quota_episode_released`` event. ``episode()`` treats it as a boundary, so the released runs
never form an episode again, and the queue note names the release until the card's next run.

The release also clears the provider_walls.json entry the reaper copied from the released hint
(``record_wall`` only extends one, so it outlived the episode and kept other cards off the provider); a
wall ending later than the episode came from other evidence and stays. [daveey]
"""
from __future__ import annotations

import time

KIND = "quota_episode_released"


class ReleaseRefused(ValueError):
    pass


def _latest(conn, task_id):
    from hermes_cli import kanban_db as kb
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? "
                       "ORDER BY id DESC LIMIT 1", (task_id, KIND)).fetchone()
    return kb._json_dict(row[0]) if row else None


def released_run(conn, task_id):
    """Run id the latest release closed (runs up to it form no episode), or None."""
    run = (_latest(conn, task_id) or {}).get("run_id")
    return run if type(run) is int else None


def covers_latest(conn, task_id) -> dict | None:
    """The latest release's payload while no run has ended after it, else None (queue notes, clocks)."""
    payload = _latest(conn, task_id)
    run = (payload or {}).get("run_id")
    if type(run) is not int:
        return None
    later = conn.execute("SELECT 1 FROM task_runs WHERE task_id=? AND id>? LIMIT 1", (task_id, run)).fetchone()
    return None if later else payload


def record(conn, task_id, state, *, reason, via, actor):
    """Append the release event (boundary for ``episode()``) inside the caller's transaction."""
    from hermes_cli import kanban_db as kb
    payload = {"reason": reason, "via": via, "actor": actor, "run_id": state["run_id"],
               "episode_id": state["episode_id"], "until": state["until"], "hinted": bool(state.get("hinted")),
               "providers": list(state.get("providers") or [])}
    kb._append_event(conn, task_id, KIND, payload)
    return payload


def _drop_walls(walls):
    """Clear provider_walls.json entries copied from the released hints (``record_wall`` never shrinks one), so
    retiering other cards stops treating the provider as walled. A later wall stays."""
    from hermes_cli import kanban_provider_walls as pw
    from hermes_cli.kanban_quota_episode import _providers
    deadline = {}
    for wall in walls:
        for name in _providers(wall):
            deadline[name] = max(deadline.get(name, 0), wall["until"])
    for name, until in deadline.items():
        pw.drop_wall(name, not_after=until)


def release(conn, task_id: str, *, reason: str, actor: str) -> dict:
    """Operator release of the card's quota episode; raises ReleaseRefused."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import write_txn
    from hermes_cli.kanban_db_dispatch_quota import clear_quota_error, is_quota_error
    from hermes_cli.kanban_operator_resume import worker_lineage
    from hermes_cli.kanban_quota_episode import _outstanding, episode
    from hermes_cli.kanban_wake_check import foreign_copy
    marker = worker_lineage()
    if marker:
        raise ReleaseRefused(f"operator-only: refused under worker lineage ({marker})")
    reason = (reason or "").strip() or "operator release"
    with write_txn(conn):
        task = kb.get_task(conn, task_id)
        if task is None:
            raise ReleaseRefused(f"unknown task {task_id}")
        if foreign_copy(conn, task_id):
            raise ReleaseRefused("quota-release must run on the card's home gateway")
        state = episode(conn, task_id)
        if not state:
            raise ReleaseRefused("no quota episode holds this card")
        held = _outstanding(conn, task_id, state, time.time())
        payload = record(conn, task_id, state, reason=reason[:500], via="operator", actor=actor)
        stamp = kb._lossy_text(task.last_failure_error)
        if stamp and is_quota_error(stamp):
            clear_quota_error(conn, task_id, stamp)
        kb.add_comment(conn, task_id, actor, f"QUOTA RELEASED: {reason}")
    _drop_walls(held)
    return payload


def cmd(args) -> int:
    """``hermes kanban quota-release <id> [--reason ...]``."""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban import _profile_author
    from hermes_cli.kanban_output import _err
    try:
        with kbc.connect_closing() as conn:
            payload = release(conn, args.task_id, reason=args.reason, actor=_profile_author())
    except ReleaseRefused as exc:
        return _err(f"kanban quota-release: {exc}", 2)
    until = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(payload["until"]))
    print(f"Released {args.task_id}: quota episode {payload['episode_id']} (held until {until}) no longer "
          f"holds admission ({KIND})")
    return 0
