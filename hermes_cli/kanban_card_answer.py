"""Exact-ask owner transition for a verified remote Desktop decision. [daveey]

The courier authenticates the sender and reads the operation from its source
store. An envelope is routing input, never proof that the user answered. This
module deliberately has no tool or generic CLI entry point.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Callable

from hermes_cli import kanban_db as kb

FIELD = "card_answer"
LIFECYCLE = ("created", "blocked", "block_loop_detected", "unblocked", "archived", "completed")


class AnswerRefused(ValueError):
    """A stale, conflicting or unverified answer must leave the card untouched."""


class _AlreadyRecorded(Exception):
    """Internal transaction exit carrying the winner's independently read receipt."""

    def __init__(self, receipt):
        self.receipt = receipt


def encoded(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def snapshot(conn, task: str) -> dict | None:
    """Portable frozen ask; local integer event ids must never cross gateways.

    Synced events have UUID identities. Unsynced fixtures retain local ids, but
    cannot be delivered remotely without the adapter's independent owner check.
    """
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
    if row is None or row["status"] not in ("blocked", "triage"):
        return None
    try:
        ask = json.loads(row["human_request"] or "null")
    except (ValueError, TypeError):
        return None
    if not isinstance(ask, dict) or not ask.get("action"):
        return None
    marks = ",".join("?" for _ in LIFECYCLE)
    events = conn.execute(
        f"SELECT * FROM task_events WHERE task_id=? AND kind IN ({marks}) ORDER BY id",
        (task, *LIFECYCLE),
    ).fetchall()
    # Include the complete lifecycle identity set, not importer-local ordering.
    # A repeated identical block therefore has a distinct generation on replicas.
    identities = sorted(str(e["uid"]) if "uid" in e.keys() and e["uid"] else f"local:{e['id']}"
                        for e in events)
    return {"task": task, "status": row["status"], "ask": ask,
            "generation": digest({"lifecycle": identities, "ask": ask, "status": row["status"]})}


def validate_provenance(value: dict, task: str) -> None:
    """Shape fence for the private comment hook; the adapter establishes truth."""
    fields = {"ownerGateway", "ownerProfile", "board", "task", "generation", "choice", "operationId", "originGateway"}
    if isinstance(value, dict) and value.get('mode') == 'comment':
        fields.add('mode')
    if (not isinstance(value, dict) or set(value) != fields or value.get("task") != task
            or any(not isinstance(value[k], str) or not value[k] for k in fields)
            or len(value["generation"]) != 64):
        raise AnswerRefused("invalid_decision_provenance")


def _payload(raw):
    try:
        value = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _valid_receipt(conn, task, request, fingerprint, kind, receipt) -> bool:
    """Every receipt field and its same-transaction decision comment, exactly."""
    fields = set(request) | {"originGateway", "fingerprint", "destination", "executionStarted", "decisionProvenance"}
    origin = receipt.get("originGateway")
    if (set(receipt) != fields or any(receipt.get(k) != v for k, v in request.items())
            or not isinstance(origin, str) or not origin or receipt["fingerprint"] != fingerprint
            or receipt["destination"] != "kanban_ask" or receipt["executionStarted"] is not False
            or receipt["decisionProvenance"] != "recorded_desktop_operation"
            or (request.get('mode') == 'comment') != (kind == 'card_answer_recorded')
            or (kind == "archived" and request["choice"] != "archive")):
        return False
    provenance = {**request, "originGateway": origin}
    found = []
    for row in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='commented' ORDER BY id", (task,),
    ):
        payload = _payload(row[0])
        decision = payload.get("recorded_decision") if payload else None
        if isinstance(decision, dict) and decision.get("operationId") == request["operationId"]:
            found.append(payload)
    if len(found) != 1:
        return False
    payload = found[0]
    if (payload["recorded_decision"] != provenance or payload.get("writer_kind") != "agent"
            or "relayed_human" in payload or payload.get("author") != "user-steps"):
        return False
    comment = conn.execute("SELECT author,body FROM task_comments WHERE id=? AND task_id=?",
                           (payload.get("comment_id"), task)).fetchone()
    prefix, suffix = "Choice recorded in Desktop (remote): ", "\nOperation: " + request["operationId"]
    if comment is None or comment[0] != "user-steps" or not (comment[1].startswith(prefix) and comment[1].endswith(suffix)):
        return False
    chosen = _payload(comment[1][len(prefix):-len(suffix)])
    return chosen is not None and chosen.get("value") == request["choice"]


def _receipt(conn, request, fingerprint):
    task, operation_id = request["task"], request["operationId"]
    for row in conn.execute(
        "SELECT kind,payload FROM task_events WHERE task_id=? AND kind IN "
        "('unblocked','archived','card_answer_recorded','card_answer_released','card_answer_declined') "
        "ORDER BY id DESC",
        (task,),
    ):
        payload = _payload(row[1])
        receipt = payload.get(FIELD) if payload else None
        if not isinstance(receipt, dict) or receipt.get("operationId") != operation_id:
            continue
        if receipt.get("fingerprint") != fingerprint:
            raise AnswerRefused("operation_conflict")
        if not _valid_receipt(conn, task, request, fingerprint, row[0], receipt):
            raise AnswerRefused("operation_receipt_invalid")
        return receipt
    return None


RELEASED = "card_answer_released"  # receipt of a default Unblock that released a spend/scope stop
DECLINED = "card_answer_declined"  # receipt of Don't on a human-only release offer: nothing changed


def _record_release(conn, request, fingerprint, receipt, note, provenance):
    """The receipt and its decision comment in one transaction, after the release landed."""
    with kb.write_txn(conn):
        old = _receipt(conn, request, fingerprint)
        if old is not None:
            return old
        kb.add_comment(conn, request["task"], "user-steps", note, writer_kind="agent", recorded_decision=provenance)
        kb._append_event(conn, request["task"], RELEASED, {FIELD: receipt})
    return _receipt(conn, request, fingerprint)


def _release(conn, request, fingerprint, receipt, note, provenance, fence, ask):
    """A default Unblock on an ask that offered a release is the user's authenticated Desktop operation: release
    the stopped card as that human act (``kanban_release``).

    The offer is the ``release_target`` stamp the stranded-park brief writes on its own ask and no supplied
    request can carry (``kanban_release.offered``): it must name the card still held and its still-pending stop,
    and on a decision card the stop that card was filed for. Anything else returns None and the caller applies
    the plain unblock: an agent-written ask, an ask filed before the stamp existed, a stale decision card, a
    card no stop holds, and a decision card whose stopped card lives on another board (it is not held here).
    A refused release (provider safety, containment) leaves the ask in place and records the reason once per
    operation. ``fence`` is asked here and again inside the transaction that records the release decision."""
    from hermes_cli import kanban_release
    task, operation = request["task"], request["operationId"]
    if kanban_release.offered(conn, task, ask) is None:
        return None
    error = fence(conn)
    if error:
        raise AnswerRefused(error)
    out = kanban_release.release(conn, task, writer=kanban_release.human_writer("desktop-operation:" + operation),
                                 author="user-steps", note=kanban_release.desktop_note(operation), precondition=fence)
    if out["outcome"] == "refused" and out["refusal"] == "ask_changed":
        raise AnswerRefused("stale_ask")
    if out["outcome"] == "refused":
        reason = (f"Desktop Unblock did not release {out['target']} ({out['refusal']}): {out['message']}"
                  f"\nOperation: {operation}")
        if not conn.execute("SELECT 1 FROM task_comments WHERE task_id=? AND author='user-steps' AND body=?",
                            (task, reason)).fetchone():
            kb.add_comment(conn, task, "user-steps", reason, system=True)
        raise AnswerRefused(f"release_refused: {out['refusal']}")
    return _record_release(conn, request, fingerprint, receipt, note, provenance)


def _offers(conn, request, fingerprint, receipt, note, origin, fence, ask, plain):
    """``(exception, receipt)`` for an answer that may act on a home-filed offer: the default Unblock
    (:func:`_default_unblock`) or Release / Don't on a human-only offer (:func:`_human_offer`). ``plain``: an
    archive or an informational comment, which never does."""
    if plain:
        return None, None
    if not ask.get("choices") and request["choice"] == "unblock":
        return _default_unblock(conn, request, fingerprint, receipt, note, origin, fence, ask)
    return None, _human_offer(conn, request, fingerprint, receipt, note, origin, fence, ask)


def _human_offer(conn, request, fingerprint, receipt, note, origin, fence, ask):
    """Release / Don't on this home's own human-only offer (``kanban_release_offer``), else None.

    Release is :func:`_release`: the recorded human answer releases the stopped card as that person's act, exactly
    as the default Unblock on a stamped ask does. Don't records the answer and leaves the card and its ask as they
    are. Both need the live, home-filed stamp (``kanban_release.offered``, whose digest covers the choices): an ask
    whose stamp went stale after a new stop, or text the home did not file, falls through to the plain transition."""
    from hermes_cli import kanban_release
    from hermes_cli import kanban_release_offer as offers
    if request["choice"] not in (offers.RELEASE, offers.UNBLOCK, offers.DONT) or not offers.is_offer(ask):
        return None
    if kanban_release.offered(conn, request["task"], ask) is None:
        return None
    provenance = {**request, "originGateway": origin}
    if request["choice"] != offers.DONT:  # Release, or the Desktop's default Unblock on the offer
        return _release(conn, request, fingerprint, receipt, note, provenance, fence, ask)
    with kb.write_txn(conn):
        old = _receipt(conn, request, fingerprint)
        if old is not None:
            return old
        error = fence(conn)
        if error:
            raise AnswerRefused(error)
        kb.add_comment(conn, request["task"], "user-steps", note, writer_kind="agent", recorded_decision=provenance)
        kb._append_event(conn, request["task"], DECLINED, {FIELD: receipt})
    return _receipt(conn, request, fingerprint)


def _default_unblock(conn, request, fingerprint, receipt, note, origin, fence, ask):
    """``(exception, released)`` for a default Unblock. On the home's own admission-exception offer
    (``kanban_selfwork_exception``) the first is its stamp: the caller's plain unblock then records the grant on
    its event, releases nothing and renews no spend. Otherwise the second is :func:`_release`'s receipt or None."""
    from hermes_cli import kanban_selfwork_exception as sx
    exception = sx.granting(conn, request["task"], ask)
    if exception is not None:
        return exception, None
    return None, _release(conn, request, fingerprint, receipt, note, {**request, "originGateway": origin}, fence, ask)


def _owner(conn, task, gateway, *, require_primary=False):
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
    if row is None or ('created_on' in row.keys() and row['created_on'] not in (None, '', gateway)):
        raise AnswerRefused('owner_card_missing_or_foreign')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_sync_state'").fetchone():
        state = conn.execute('SELECT gateway_id,mode FROM kanban_sync_state LIMIT 1').fetchone()
        if state is None or state['gateway_id'] != gateway or state['mode'] != 'primary':
            raise AnswerRefused('owner_board_not_primary')
    elif require_primary:
        raise AnswerRefused('owner_board_not_primary')


def apply(conn, request: dict, *, owner_gateway: str, owner_profile: str, board: str,
          verify_source: Callable[[dict], dict], require_primary: bool = False) -> dict:
    """Apply only after independent source-operation and owner verification.

    ``verify_source`` is a trusted adapter callback, NOT an envelope field. It
    reads an immutable recorded Desktop operation from the authenticated sending
    gateway and returns its exact target/decision fields. Network evidence is
    acquired before taking the board lock; the exact ask is rechecked under it.
    The transition API owns its transaction and post-commit archive cleanup.
    """
    fields = {"ownerGateway", "ownerProfile", "board", "task", "generation", "choice", "operationId"}
    if isinstance(request, dict) and request.get('mode') == 'comment':
        fields.add('mode')
    if not isinstance(request, dict) or set(request) != fields:
        raise AnswerRefused("invalid_answer")
    if any(not isinstance(request[k], str) or not request[k] for k in fields):
        raise AnswerRefused("invalid_answer")
    if (request["ownerGateway"] != owner_gateway or request["ownerProfile"] != owner_profile
            or request["board"] != board or not re.fullmatch(r'[A-Za-z0-9._:-]{1,200}', request["operationId"])
            or len(request["choice"]) > 2000 or len(request["generation"]) != 64):
        raise AnswerRefused("invalid_answer")
    _owner(conn, request['task'], owner_gateway, require_primary=require_primary)
    fingerprint = digest(request)
    old = _receipt(conn, request, fingerprint)
    if old:
        return old
    # Replays of committed decisions do not depend on a now-offline origin.
    # Fresh applications never accept caller-supplied proof or a bare boolean.
    proof = verify_source(dict(request))
    if not isinstance(proof, dict) or proof.get("answer") != request or proof.get("recorded") is not True:
        raise AnswerRefused("unverified_user_decision")
    origin = proof.get("gateway")
    if not isinstance(origin, str) or not origin:
        raise AnswerRefused("unverified_user_decision")
    current = snapshot(conn, request["task"])
    if current is None or current["generation"] != request["generation"]:
        # Another delivery may commit while source verification is in flight.
        # Resolve its fully validated receipt under the same owner write lock
        # before interpreting the resulting lifecycle change as a stale ask.
        with kb.write_txn(conn):
            _owner(conn, request['task'], owner_gateway, require_primary=require_primary)
            old = _receipt(conn, request, fingerprint)
            if old is not None:
                return old
        # A release by this very operation that died before its receipt: the card left its ask because the
        # click worked. Record the receipt now instead of refusing the retry.
        from hermes_cli import kanban_release
        label = {"unblock": "Unblock", "release": "Release"}.get(request["choice"])
        if request.get("mode") != "comment" and label and kanban_release.released_by(
                conn, request["task"], kanban_release.desktop_note(request["operationId"])):
            return _record_release(
                conn, request, fingerprint,
                {**request, "originGateway": origin, "fingerprint": fingerprint, "destination": "kanban_ask",
                 "executionStarted": False, "decisionProvenance": "recorded_desktop_operation"},
                "Choice recorded in Desktop (remote): " + encoded({"label": label, "value": request["choice"]})
                + "\nOperation: " + request["operationId"], {**request, "originGateway": origin})
        raise AnswerRefused("stale_ask")
    options = current["ask"].get("choices") or [
        {"label": "Unblock", "value": "unblock"}, {"label": "Archive", "value": "archive"},
    ]
    from hermes_cli import kanban_release_offer as offers
    if request["choice"] == offers.UNBLOCK and offers.is_offer(current["ask"]):
        options = [*options, {"label": "Unblock", "value": offers.UNBLOCK}]  # Desktop's Unblock = Release here
    choices = [ch for ch in options if isinstance(ch, dict) and ch.get("value") == request["choice"]]
    informational = request.get('mode') == 'comment'
    if informational:
        choices = [dict(label='Other (user-relayed comment; does not resume)', value=request['choice'])]
    elif len(choices) != 1:
        raise AnswerRefused("unknown_choice")
    archive = not informational and not current["ask"].get("choices") and request["choice"] == "archive"
    receipt = {**request, "originGateway": origin, "fingerprint": fingerprint,
               "destination": "kanban_ask", "executionStarted": False,
               "decisionProvenance": "recorded_desktop_operation"}
    note = "Choice recorded in Desktop (remote): " + encoded(choices[0]) + "\nOperation: " + request["operationId"]

    def fence(c, releasing=False):
        _owner(c, request['task'], owner_gateway, require_primary=require_primary)
        from hermes_cli.kanban_spend_decision import pending
        from hermes_cli.kanban_containment_guard import pending as containment_pending
        if containment_pending(c, request['task']) and not releasing:  # release refuses it with the reason
            return "containment_hold"
        if not archive and not informational and not releasing and pending(c, request["task"]):
            return "spend_decision_pending"
        if _receipt(c, request, fingerprint):
            return "already_recorded"
        fresh = snapshot(c, request["task"])
        return None if sx.same_offer(c, fresh, current, exception) else "stale_ask"

    from hermes_cli import kanban_selfwork_exception as sx
    exception = None  # read by fence() while _offers runs
    exception, released = _offers(conn, request, fingerprint, receipt, note, origin,
                                  lambda c: fence(c, releasing=True), current["ask"], archive or informational)
    if released is not None:
        return released

    transition = kb.archive_task if archive else kb.unblock_task
    from hermes_cli.kanban_reason_comment import RecordedDecisionComment
    provenance = {**request, "originGateway": origin}

    def before_insert(c):
        old = _receipt(c, request, fingerprint)
        if old is not None:
            raise _AlreadyRecorded(old)

    comment = RecordedDecisionComment("user-steps", note, provenance, before_insert)
    if informational:
        with kb.write_txn(conn):
            old = _receipt(conn, request, fingerprint)
            if old is not None:
                return old
            error = fence(conn)
            if error:
                raise AnswerRefused(error)
            kb.add_comment(conn, request['task'], comment.author, comment.body,
                           writer_kind='agent', recorded_decision=provenance)
            kb._append_event(conn, request['task'], 'card_answer_recorded', {FIELD: receipt})
        return _receipt(conn, request, fingerprint)
    extra = {FIELD: receipt, **sx.grant_fields(exception, request, fingerprint, origin)}
    try:
        transition(conn, request["task"], precondition=fence, comment=comment, event_extra=extra)
    except _AlreadyRecorded as done:
        return done.receipt
    recorded = _receipt(conn, request, fingerprint)
    if not recorded:
        raise AnswerRefused("ask_changed_or_held")
    return recorded
