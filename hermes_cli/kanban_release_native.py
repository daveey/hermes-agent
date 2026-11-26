"""Native parent's exact owner-release producer; child fields contain no identity.

Authorization is not HOME application. Pending is reported as pending, and every
uncertain send retains its original journal identity. No local legacy release is
used as fallback when the canonical contract is absent or refuses the request.
"""
from __future__ import annotations

from contextlib import ExitStack

from hermes_cli import kanban_release_intent as intent

_FIELDS = {"board", "task_id", "episode", "capability_episodes", "runs", "note"}


def _proof(hub, body):
    proof = hub.owner_release_evidence(body["board"], body["intent_uid"])
    if (not isinstance(proof, dict) or set(proof) != {"version", "intent", "digest", "state"}
            or type(proof["version"]) is not int or proof["version"] != 1
            or not isinstance(proof["state"], str)
            or proof["state"] not in {"authorized", "consumed", "applied", "expired", "refused"}
            or intent.validate(proof["intent"]) != body or intent.encode(proof["intent"]) != intent.encode(body)
            or proof["digest"] != intent.digest(body)):
        raise ValueError("canonical owner-release evidence unavailable or mismatched")
    return proof


def _send(conn, hub, body, *, seen=None):
    from hermes_cli import kanban_release_delivery as delivery, kanban_release_result as result
    if delivery.existing(conn, body) is not None:
        return delivery.send(conn, hub, body)  # Completed result precedes publication inside send.
    completed = result.reconcile(conn, hub, body)
    if completed is not None:
        return completed
    stage = intent.stage(conn, body)  # Fresh custody, not authority from a prior read.
    if stage in {"expired", "refused", "applied"}:
        return {"ok": stage == "applied", "task_id": body["task_id"], "intent_uid": body["intent_uid"],
                "release_stage": "consumed_or_applied" if stage == "applied" else stage}
    if stage == "intent_journaled_unsent":
        if not intent.advance(conn, body, expected=stage, stage="send_pending"):
            raise ValueError("release journal changed before send")
        stage = "send_pending"
    if stage == "send_pending":
        # Commit is complete before networking; this exact op can be reconciled
        # after reply loss. The Hub deliberately does not automatically replay POSTs.
        intent.stage(conn, body)  # Reauthenticate immediately before actual send.
        hub.ops(body["board"], [{"client_op_id": body["intent_uid"], "op": "authorize_owner_release",
                                  "task": body["task_id"], "intent": body, "digest": intent.digest(body)}])
    proof = _proof(hub, body)  # A successful POST alone is not authorization evidence.
    if proof["state"] in {"expired", "refused"}:
        if not intent.advance(conn, body, expected=stage, stage=proof["state"]):
            if intent.stage(conn, body) != proof["state"]:
                raise ValueError("release journal changed during terminal reconciliation")
        return {"ok": False, "task_id": body["task_id"], "intent_uid": body["intent_uid"],
                "release_stage": proof["state"]}
    if stage == "send_pending":
        if not intent.advance(conn, body, expected=stage, stage="authorized_pending_home"):
            raise ValueError("release journal changed after authorization")
    if proof["state"] == "authorized":
        from hermes_cli.kanban_release_read import prepare
        result = prepare(conn, hub, body, seen=seen)
        if result.get("release_stage") == "acknowledged_pending_home":
            delivery.prepare(conn, body, nonce=seen)
            return delivery.send(conn, hub, body)
        return result
    # Consumption/application are not inferred from a writable event. HOME's
    # exact result is reconciled separately; authorization only promises pending.
    return {"ok": True, "task_id": body["task_id"], "intent_uid": body["intent_uid"],
            "release_stage": "authorized_pending_home"}


def dispatch(args: dict, *, gate) -> dict:
    """Called only by captured parent policy; still independently checks custody."""
    from agent.origin_turn_custody import CapturedOrigin, origin
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_sync_client as client
    from hermes_cli.kanban_origin_native import _primary_scope

    if not isinstance(args, dict) or set(args) - {"fresh_after_expiry", "seen_release"} != _FIELDS:
        raise ValueError("native release accepts only exact action fields, never caller or HOME overrides")
    expired_intent = args.get("fresh_after_expiry")
    seen = args.get("seen_release")
    args = {k: v for k, v in args.items() if k not in {"fresh_after_expiry", "seen_release"}}
    from hermes_cli.kanban_release import default_runs
    args = {**args, "board": kb._normalize_board_slug(args["board"]) or kb.get_current_board(),
            "runs": default_runs() if args["runs"] is None else args["runs"]}
    # Validate the bounded child action before using coordinates in any network path.
    intent._action({**args, "home": "pending-canonical-home"})
    qualified = origin()
    if qualified is None:
        raise ValueError("release requires live native owning-chat custody")
    captured = CapturedOrigin(qualified)
    with kb.scoped_current_board(args["board"]), ExitStack() as scope:
        conn = scope.enter_context(kbc.connect_closing())
        if _primary_scope(conn) != (qualified["gateway"], qualified["profile"], args["board"]):
            raise ValueError("native release requires its exact local primary board")
        hub = client.Hub(client.load_identity())
        scope.callback(hub.close)
        if hub.ident.gateway != qualified["gateway"]:
            raise ValueError("native release gateway differs from authenticated transport")
        recorded = intent.existing(conn, args)
        if recorded is not None and (expired_intent is None or intent.acknowledged_successor(conn, recorded, expired_intent)):
            # Reconcile the immutable request before capability/current HOME reads.
            # prepare still reauthenticates and admits this exact actor/action.
            body = intent.prepare(conn, {**args, "home": recorded["home"]}, gate=gate, expired_intent=expired_intent)
            return _send(conn, hub, body, seen=seen)
        capability = hub.decision_retain_capability(args["board"])
        if not isinstance(capability, dict) or type(capability.get("owner_release")) is not int or capability["owner_release"] != 1:
            return {"ok": False, "reason": "board_unsupported", "error": "Board owner-release v1 is not available; nothing sent"}
        proof = hub.row_evidence(args["board"], "tasks", args["task_id"], max_response_bytes=131072)
        row = proof.get("row") if isinstance(proof, dict) and proof.get("state") == "live" else None
        home = row.get("created_on") if isinstance(row, dict) and row.get("id") == args["task_id"] else None
        if not isinstance(home, str) or not home or home == qualified["gateway"]:
            raise ValueError("native foreign release needs exact canonical foreign HOME")
        if captured.validated() is None:
            raise ValueError("native release custody expired before preparation")
        body = intent.prepare(conn, {**args, "home": home}, gate=gate, expired_intent=expired_intent)
        return _send(conn, hub, body, seen=seen)
