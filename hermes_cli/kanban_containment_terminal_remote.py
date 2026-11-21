"""Canonical terminal archive: ordinary op, durable request, authenticated readback.

No local SQL lifecycle write precedes Board admission. A lost reply reuses the
same operation; closure event identities are server-generated, never guessed.
"""
from __future__ import annotations

import json

from hermes_cli import kanban_containment_canonical as canonical
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
from hermes_cli.kanban_containment_state import _payload


PROTECTED = ("contained", "containment_released", "containment_closed")
MAX_EVENTS = 1000
META_MAX_BYTES, VIA_MAX, DEPTH_MAX = 16384, 2000, 32


def _evidence(hub, board, task, uid, kinds):
    """One exact authenticated event row; identity, task and kind are checked, never trusted from a page."""
    proof = hub.row_evidence(board, "task_events", uid)
    row = proof.get("row") if isinstance(proof, dict) else None
    if (not isinstance(row, dict) or proof.get("board") != board or proof.get("table") != "task_events"
            or proof.get("uid") != uid or proof.get("state") != "live" or row.get("uid") != uid
            or row.get("task_id") != task or row.get("kind") not in kinds):
        raise ContainmentRefused("canonical archive event evidence is invalid")
    return proof


def _inventory(hub, board, task):
    """Targeted metadata context, then exact evidence per UID: no board-wide snapshot scan.

    Returns ``(protected_proofs, archive_proof_or_None)``."""
    from hermes_cli import kanban_containment_remote as remote
    ctx = remote.context(hub, board, task)
    uids, latest = ctx.get("containment_events"), ctx.get("archive_event")
    if (not isinstance(uids, list) or len(uids) > MAX_EVENTS or len(set(uids)) != len(uids)
            or any(not isinstance(u, str) or not u for u in uids)
            or not (latest is None or (isinstance(latest, str) and latest))):
        raise ContainmentRefused("canonical archive context is incomplete")
    protected = {u: _evidence(hub, board, task, u, PROTECTED) for u in uids}
    archived = _evidence(hub, board, task, latest, ("archived",)) if latest else None
    return protected, archived


def _metadata(via, event_extra):
    """The exact bounded Board ``archive_metadata`` carriage, or None when nothing is carried."""
    if not via and not event_extra:
        return None
    if via is not None and not isinstance(via, str):
        raise ContainmentRefused("archive via must be a string")
    if event_extra is not None and not isinstance(event_extra, dict):
        raise ContainmentRefused("archive event_extra must be an object")

    def safe(v, depth):
        if depth > DEPTH_MAX:
            return False
        if v is None or isinstance(v, (str, bool)):
            return True
        if isinstance(v, int):
            return abs(v) <= 2 ** 53
        if isinstance(v, float):
            return v == v and v not in (float("inf"), float("-inf"))
        if isinstance(v, (list, tuple)):
            return all(safe(x, depth + 1) for x in v)
        return type(v) is dict and all(isinstance(k, str) and safe(x, depth + 1) for k, x in v.items())

    meta = {"via": via or None, "event_extra": dict(event_extra) if event_extra else None}
    if (meta["via"] is not None and len(meta["via"].encode("utf-16-le")) // 2 > VIA_MAX) or not safe(meta, 0):
        raise ContainmentRefused("archive metadata is not bounded JSON")
    if len(json.dumps(meta, separators=(",", ":"), ensure_ascii=False).encode()) > META_MAX_BYTES:
        raise ContainmentRefused("archive metadata exceeds the Board bound")
    return json.loads(json.dumps(meta))


def _expected_event(meta):
    """What the Board writes for this metadata: extras, then via, then source_status last."""
    extra = dict((meta or {}).get("event_extra") or {})
    extra.pop("source_status", None)
    via = (meta or {}).get("via")
    if isinstance(via, str):
        extra.pop("via", None)
        extra["via"] = via
    extra["source_status"] = "triage"
    return extra


def _same_metadata(op, meta):
    return (op.get("archive_metadata") or None) == (meta or None)



def _find(conn, board, gateway, task, meta):
    """The single durable archive request for this task (metadata must be exactly what was staged), or None."""
    canonical._schema(conn)
    found = []
    for uid, raw, confirmed in conn.execute(
        "SELECT request_uid,op_json,confirmed FROM containment_delivery WHERE board=? AND gateway=? AND confirmed>=0",
        (board, gateway),
    ):
        op = json.loads(raw)
        if op.get("op") == "archive" and op.get("task") == task:
            found.append((uid, op, confirmed))
    if len(found) > 1:
        raise ContainmentRefused("ambiguous canonical archive request custody")
    if found and not _same_metadata(found[0][1], meta):
        raise ContainmentRefused("canonical archive retry carries different metadata than its durable request")
    return found[0] if found else None


def _request(conn, board, gateway, task, meta=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_sync import uuid7
    found = _find(conn, board, gateway, task, meta)
    if found:
        return found
    uid = uuid7()
    op = {"op": "archive", "task": task, "client_op_id": uid, **({"archive_metadata": meta} if meta else {})}
    with kb.write_txn(conn):
        conn.execute("INSERT INTO containment_delivery(request_uid,board,gateway,op_json,confirmed) VALUES (?,?,?,?,0)",
                     (uid, board, gateway, json.dumps(op, sort_keys=True, separators=(",", ":"))))
    return uid, op, 0


def _scope_refusal(conn, task, precondition):
    """Only the creator-archive scope has an exact canonical form; any other local predicate cannot be enforced atomically.

    The Board archive itself atomically requires an unclaimed triage task with no run/lease/human request, which is
    everything ``archive_scope_for`` additionally checks for a creator-owned card. What remains is the creator
    identity, immutable from the card's created event, so it is checked once here, before any network operation."""
    from hermes_cli.kanban_creator_archive import CreatorScope
    if precondition is None:
        return
    if type(precondition) is not CreatorScope:
        raise ContainmentRefused("canonical terminal archive cannot carry a local precondition")
    from hermes_cli.kanban_archive_scope import _creator_task_id
    who = precondition.requester
    if not who or who == task or precondition.target != task or _creator_task_id(conn, task) != who:
        raise ContainmentRefused("creator_archive requires a different card created by the requesting task")


def archive(conn, task, *, comment=None, precondition=None, via=None, event_extra=None):
    from hermes_cli import kanban_containment_remote as remote
    from hermes_cli import kanban_containment_replica as replica
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client
    from hermes_cli.kanban_containment_terminal import terminal_closed
    if conn.in_transaction:
        raise ContainmentRefused("canonical terminal archive cannot run inside a writer transaction")
    # Sync can project a closure before a lost-reply request is settled. Finish
    # that request once; only then suppress replay of its reason comment.
    from hermes_cli.kanban_containment_terminal import closed
    canonical._schema(conn)
    state = sync.sync_state(conn)
    unsettled = conn.execute(
        "SELECT 1 FROM containment_delivery WHERE board=? AND gateway=? AND confirmed=0 "
        "AND json_extract(op_json,'$.op')='archive' AND json_extract(op_json,'$.task')=?",
        (state["board_id"], state["gateway_id"], task),
    ).fetchone() if state else None
    current = kb.get_task(conn, task)
    if current is not None and current.status == "archived" and closed(conn, task) and not unsettled:
        return False
    _scope_refusal(conn, task, precondition)  # before any network operation
    meta = _metadata(via, event_extra)
    if state:  # a changed retry is refused from local custody alone, before any Board contact
        _find(conn, state["board_id"], state["gateway_id"], task, meta)
    hub, board = remote.enrolled(conn)
    try:
        capability = hub.containment_capability(board)
        if not isinstance(capability, dict) or type(capability.get("terminal_archive")) is not int or capability["terminal_archive"] != 1:
            raise ContainmentRefused("Board terminal archive capability is unavailable")
        request, op, confirmed = _request(conn, board, hub.ident.gateway, task, meta)
        if not confirmed:
            response = hub.ops(board, [op])
            results = response.get("results") if isinstance(response, dict) else None
            if (not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict)
                    or results[0].get("client_op_id") != request):
                raise canonical.DeliveryUnknown("canonical archive disposition unknown; recover same request", request_uid=request)
            verdict = results[0]
            if verdict.get("status") == "rejected":
                with kb.write_txn(conn):
                    conn.execute("UPDATE containment_delivery SET confirmed=-1 WHERE request_uid=?", (request,))
                raise ContainmentRefused("Board refused terminal archive: " + str(verdict.get("reason")))
            if verdict.get("status") not in ("applied", "merged"):
                raise canonical.DeliveryUnknown("canonical archive outcome unknown; recover same request", request_uid=request)
        proofs, archived = _inventory(hub, board, task)
        rows = [p["row"] for p in proofs.values()]
        def verify(receipt, stop):
            certify = canonical.certified_closure if receipt["kind"] == "containment_closed" else canonical.certified_release
            certify(proofs[stop["uid"]], proofs[receipt["uid"]], board=board, task=task,
                    episode=stop["uid"], receipt=receipt["uid"])
            return True
        if not terminal_closed(rows, verify=verify):
            raise ContainmentRefused("canonical terminal archive lacks authenticated closures for all active stops")
        closures = [r for r in rows if r["kind"] == "containment_closed"]
        if not closures or not any(proofs[r["uid"]]["containment_v1"]["request"] == request for r in closures):
            raise canonical.DeliveryUnknown("canonical closure belongs to another archive request", request_uid=request)
        # The exact canonical audit event: never synthesized locally, and it must be what this request staged.
        try:
            audit = json.loads(archived["row"]["payload"]) if archived else None
        except (TypeError, ValueError):
            audit = None
        if audit is None or audit != _expected_event(meta) or list(audit)[-1] != "source_status":
            raise ContainmentRefused("canonical archived audit event differs from the staged archive metadata")
        proof = hub.row_evidence(board, "tasks", task)
        row = proof.get("row") if isinstance(proof, dict) else None
        if (not isinstance(proof, dict) or proof.get("board") != board or proof.get("table") != "tasks"
                or proof.get("uid") != task or proof.get("state") != "live" or not isinstance(row, dict)
                or row.get("id") != task or row.get("status") != "archived" or row.get("containment_episode") is None
                or row.get("current_run_uid") is not None or row.get("human_request") is not None
                or row.get("delete_pending")):
            raise ContainmentRefused("canonical terminal task readback changed custody")
        ordered = sorted([*rows, archived["row"]], key=lambda r: (r.get("ord", 0), r["uid"]))
        changes = [client._row_change("task_events", r) for r in ordered]
        changes.append(client._row_change("tasks", {**row, "uid": task}))
        replica.certify_page(conn, hub, board, changes)
        sync.apply_remote_changes(conn, changes, new_cursor=sync.sync_state(conn)["cursor"], board=board)
        from hermes_cli.kanban_containment_terminal import closed
        if (not closed(conn, task) or kb.get_task(conn, task).status != "archived"
                or conn.execute("SELECT 1 FROM task_events WHERE uid=? AND task_id=? AND kind='archived'",
                                (archived["row"]["uid"], task)).fetchone() is None):
            raise ContainmentRefused("canonical terminal archive was not projected locally")
        with kb.write_txn(conn):
            conn.execute("UPDATE containment_delivery SET confirmed=1 WHERE request_uid=?", (request,))
            if comment:
                kb.add_comment(conn, task, author=comment[0], body=comment[1])
        return True
    finally:
        hub.close()
