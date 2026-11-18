"""Shared admitted-agent containment entry point for CLI and tools.

Local boards use the transaction owner; enrolled boards require authenticated
canonical transport. Board unavailability never falls back to local authority.
"""
from __future__ import annotations

import sqlite3

from hermes_cli import kanban_containment_lifecycle as lifecycle
from hermes_cli.kanban_containment_actor import current_actor
from hermes_cli.kanban_containment_policy import identity


def _target(conn: sqlite3.Connection, task_id: str) -> dict:
    from hermes_cli.kanban_db_sync import board_slug_for_path
    lifecycle._local(conn)  # persisted sync membership, not ambient config
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    return identity({"gateway": "local", "board": board_slug_for_path(path), "task_id": task_id})


def contain(conn: sqlite3.Connection, *, authority: str, summary: str,
            task_id: str | None = None, producer: str | None = None) -> dict:
    actor = current_actor(conn)
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
        raise lifecycle.ContainmentRefused("containment requires a bounded handoff summary")
    if _synced(conn):
        from hermes_cli import kanban_containment_remote as remote
        return remote.contain(conn, actor=actor, task=actor.task if task_id is None else task_id,
                              authority=authority, producer=producer)
    target = _target(conn, actor.task if task_id is None else task_id)
    from hermes_cli.kanban_containment_connections import bootstrap
    bootstrap(conn)
    spec = {"version": 1, "authority": identity({**target, "task_id": authority})}
    if producer is not None:
        spec["producer"] = identity({**target, "task_id": producer})
    result = lifecycle.contain_local(conn, target=target, resume_policy=spec, actor_task=actor.task,
                                     actor_run_uid=actor.run_uid, summary=summary)
    return {**result, "terminal_for_run": target["task_id"] == actor.task, "dependency_satisfied": False}


def resume(conn: sqlite3.Connection, *, task_id: str, episode: str, kind: str = "authority") -> dict:
    actor = current_actor(conn)
    if _synced(conn):
        from hermes_cli import kanban_containment_remote as remote
        return remote.resume(conn, actor=actor, task=task_id, episode=episode, kind=kind)
    target = _target(conn, task_id)
    from hermes_cli.kanban_containment_connections import bootstrap
    bootstrap(conn)
    result = lifecycle.resume_local(conn, target=target, episode=episode, actor_task=actor.task,
                                    actor_run_uid=actor.run_uid, kind=kind)
    return {**result, "dependency_satisfied": False}


def _synced(conn: sqlite3.Connection) -> bool:
    from hermes_cli import kanban_db_sync as sync
    return sync.sync_state(conn) is not None
