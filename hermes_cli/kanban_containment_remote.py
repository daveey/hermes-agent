"""Public canonical Board containment and exact-request recovery.

Preparation is metadata-only and grants no permission. The authenticated Board
rechecks the actor lease, ancestry, nominees, frozen baseline and release scope.
"""

from __future__ import annotations

import json
import sqlite3

from hermes_cli import kanban_containment_canonical as canonical
from hermes_cli.kanban_containment_actor import Actor
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
from hermes_cli.kanban_containment_policy import identity, policy
from hermes_cli.kanban_containment_state import _payload, _stop, _uid, fingerprint


def enrolled(conn: sqlite3.Connection):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client

    state = sync.sync_state(conn)
    if not state or state["mode"] != "primary":
        raise ContainmentRefused(
            "canonical containment requires an attached primary board"
        )
    hub = client.Hub(client.load_identity())
    if state["gateway_id"] != hub.ident.gateway:
        hub.close()
        raise ContainmentRefused("enrolled gateway differs from the admitted board")
    try:
        require_capability(hub, state["board_id"])
    except (ContainmentRefused, client.HubError, OSError):
        hub.close()
        raise
    return hub, state["board_id"]


def require_capability(hub, board: str) -> None:
    data = hub.containment_capability(board)
    if (
        not isinstance(data, dict)
        or type(data.get("containment_protocol")) is not int
        or data.get("containment_protocol") != 1
        or data.get("receipt_envelopes") != [1]
        or any(type(v) is not int for v in data["receipt_envelopes"])
        or data.get("task_marker") != "containment_episode"
        or data.get("resume_predicates") != ["authority", "producer"]
    ):
        raise ContainmentRefused(
            "Board protected containment protocol is unavailable or incompatible"
        )


def context(hub, board: str, task: str, producer: str | None = None) -> dict:
    data = hub.containment_context(board, task, producer)
    if (
        not isinstance(data, dict)
        or type(data.get("version")) is not int
        or data["version"] != 1
        or data.get("board") != board
    ):
        raise ContainmentRefused("Board containment context capability is absent")
    target = identity(data.get("target"))
    if (
        target["board"] != board
        or target["task_id"] != task
        or type(data.get("held")) is not bool
    ):
        raise ContainmentRefused("Board containment context target changed")
    return data


def _replay(
    conn: sqlite3.Connection, board: str, actor: Actor, task: str, action: str
) -> str | None:
    canonical._schema(conn)
    matches = []
    for request, raw in conn.execute(
        "SELECT request_uid,op_json FROM containment_delivery WHERE board=? AND confirmed=0",
        (board,),
    ):
        op = json.loads(raw)
        if (
            op.get("actor_task") == actor.task
            and op.get("actor_run_uid") == actor.run_uid
            and op.get("task") == task
            and op.get("op") == action
        ):
            matches.append(request)
    if len(matches) > 1:
        raise ContainmentRefused(
            "ambiguous containment request history; no replacement may be submitted"
        )
    return matches[0] if matches else None


def contain(
    conn: sqlite3.Connection,
    *,
    actor: Actor,
    task: str,
    authority: str,
    producer: str | None,
) -> dict:
    hub, board = enrolled(conn)
    try:
        request = _replay(conn, board, actor, task, "contain")
        if request is not None:
            saved = json.loads(
                conn.execute(
                    "SELECT op_json FROM containment_delivery WHERE request_uid=?",
                    (request,),
                ).fetchone()[0]
            )["payload"]["policy"]
            if (
                saved["authority"]["task_id"] != authority
                or saved.get("producer", {}).get("task_id") != producer
            ):
                raise ContainmentRefused(
                    "recovery cannot replace the frozen containment predicate"
                )
        if request is None:
            data = context(hub, board, task, producer)
            target = data["target"]
            from hermes_cli.kanban_containment_authority import require_open
            authority_context = context(hub, board, authority)
            require_open(authority_context.get("status"))
            spec = {"version": 1, "authority": authority_context["target"]}
            if producer is not None:
                spec["producer"] = identity(data.get("producer"))
                if spec["producer"]["task_id"] != producer:
                    raise ContainmentRefused("Board producer coordinates changed")
            payload = {
                "version": 1,
                "target": target,
                "policy": policy(spec, target=target),
                "producer_baseline": data.get("producer_baseline"),
            }
            request = _stage(conn, hub, board, actor, task, "contain", payload)
        result = _deliver(conn, hub, request)
        projected = settle_local(conn, hub, board, result, actor)
        return {
            **result,
            "status": "triage",
            "episode": result["event_uid"],
            "local_projected": projected,
            "terminal_for_run": actor.task == task,
            "dependency_satisfied": False,
        }
    finally:
        hub.close()


def resume(
    conn: sqlite3.Connection, *, actor: Actor, task: str, episode: str, kind: str
) -> dict:
    if not _uid(episode) or kind not in ("authority", "producer"):
        raise ContainmentRefused(
            "resume requires exact episode and authority or producer predicate"
        )
    hub, board = enrolled(conn)
    try:
        _recover_stop(conn, hub, board, actor, task, episode)
        request = _replay(conn, board, actor, task, "resume_contained")
        if request is not None:
            saved = json.loads(
                conn.execute(
                    "SELECT op_json FROM containment_delivery WHERE request_uid=?",
                    (request,),
                ).fetchone()[0]
            )["payload"]
            if saved["episode"] != episode or saved["evidence"]["kind"] != kind:
                raise ContainmentRefused(
                    "recovery cannot replace the exact release episode or predicate"
                )
        if request is None:
            stop_proof = hub.row_evidence(board, "task_events", episode)
            stop = canonical.certified_stop(
                stop_proof, board=board, task=task, uid=episode
            )
            frozen = _stop(stop)
            if kind == "authority":
                who = context(hub, board, actor.task)["target"]
                evidence = {"kind": kind, kind: who, "run_uid": actor.run_uid}
            else:
                producer = frozen["policy"].get("producer")
                if producer is None:
                    raise ContainmentRefused("episode has no producer predicate")
                data = context(hub, board, producer["task_id"], producer["task_id"])
                latest = data.get("producer_baseline")
                if (
                    not isinstance(latest, dict)
                    or not _uid(latest.get("event_uid"))
                    or not _uid(latest.get("run_uid"))
                ):
                    raise ContainmentRefused(
                        "producer has no portable completion evidence"
                    )
                evidence = {"kind": kind, kind: data["target"], **latest}
            payload = {
                "version": 1,
                "episode": episode,
                "containment_sha256": fingerprint(frozen),
                "resume_status": "ready",
                "evidence": evidence,
            }
            request = _stage(conn, hub, board, actor, task, "resume_contained", payload)
        result = _deliver(conn, hub, request)
        projected = settle_local(conn, hub, board, result, actor)
        data = context(hub, board, task)
        return {
            **result,
            "episode": episode,
            "status": data["status"],
            "local_projected": projected,
            "dependency_satisfied": False,
        }
    finally:
        hub.close()


def _recover_stop(conn, hub, board, actor, task, episode):
    """Live nominated authority settles only the original committed stop bytes.

    It cannot borrow the new authority's lease to authorize an expired original
    operation; Board request replay and original-actor admission still apply.
    """
    canonical._schema(conn)
    for request, raw in conn.execute(
        "SELECT request_uid,op_json FROM containment_delivery WHERE board=? AND confirmed>=0",
        (board,),
    ):
        op = json.loads(raw)
        if (
            op.get("op") != "contain"
            or op.get("task") != task
            or op.get("event_uid") != episode
        ):
            continue
        if op["payload"]["policy"]["authority"]["task_id"] != actor.task:
            return
        result = _deliver(conn, hub, request)
        settle_local(
            conn, hub, board, result, Actor(op["actor_task"], op["actor_run_uid"])
        )
        return


def _deliver(conn, hub, request):
    from hermes_cli.kanban_sync_client import HubError

    try:
        return canonical.deliver(conn, hub, request=request)
    except (canonical.DeliveryUnknown, OSError, HubError) as exc:
        op = json.loads(
            conn.execute(
                "SELECT op_json FROM containment_delivery WHERE request_uid=?",
                (request,),
            ).fetchone()[0]
        )
        episode = op["event_uid"] if op["op"] == "contain" else op["payload"]["episode"]
        raise canonical.DeliveryUnknown(
            "Canonical delivery is uncertain; recover the same request, never replace it",
            request_uid=request,
            episode=episode,
        ) from exc


def _stage(conn, hub, board, actor, task, action, payload):
    from hermes_cli.kanban_db_sync import uuid7
    from hermes_cli import kanban_containment_guard as guard
    from hermes_cli.kanban_containment_connections import verifier

    guard.install(conn, verify_release=lambda r, s: verifier(conn, r, s))
    op = {
        "client_op_id": uuid7(),
        "event_uid": uuid7(),
        "op": action,
        "task": task,
        "actor_task": actor.task,
        "actor_run_uid": actor.run_uid,
        "payload": payload,
    }
    return canonical.stage(conn, board=board, gateway=hub.ident.gateway, op=op)


def settle_local(conn, hub, board, result, actor) -> bool:
    """Read back exact canonical rows and project a retained local target.

    The feed cursor does not advance; later normal feed replay is harmless.
    A foreign TODO relay need not be picked up or dispatched just to contain it.
    """
    task = result["task_id"]
    if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (task,)).fetchone():
        return False
    from hermes_cli import kanban_sync_client as client
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_containment_replica as replica

    event = hub.row_evidence(board, "task_events", result["event_uid"])
    changes = [client._row_change("task_events", event["row"])]
    if event["row"]["kind"] == "containment_released":
        episode = _payload(event["row"])["episode"]
        changes.insert(
            0,
            client._row_change(
                "task_events", hub.row_evidence(board, "task_events", episode)["row"]
            ),
        )
    for table, uid in [
        ("tasks", task),
        *(([("task_runs", actor.run_uid)]) if actor.task == task else []),
    ]:
        proof = hub.row_evidence(board, table, uid)
        row = proof.get("row")
        if (
            proof.get("board") != board
            or proof.get("table") != table
            or proof.get("uid") != uid
            or proof.get("state") != "live"
            or not isinstance(row, dict)
            or row.get("id" if table == "tasks" else "task_id") != task
        ):
            raise ContainmentRefused("canonical lifecycle projection changed identity")
        changes.append(client._row_change(table, {**row, "uid": uid}))
    replica.certify_page(conn, hub, board, changes)
    cursor = sync.sync_state(conn)["cursor"]
    sync.apply_remote_changes(conn, changes, new_cursor=cursor, board=board)
    return True
