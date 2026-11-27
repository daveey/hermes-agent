"""Atomic owner-side creation intents; publication/receipt transport runs later.

The request ID, card and immutable publication marker commit in the same board
transaction. No network I/O is allowed here. Ordinary (non-origin) creation keeps
its legacy idempotency semantics; receipt workflows use this stricter ledger.
"""
from __future__ import annotations

import json
import re
import secrets

from hermes_cli.kanban_origin import OriginError


class OriginCreateRefused(OriginError):
    """A deterministic refusal proven before this origin request can insert a task."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _subscribe_origin(conn, task_id: str, origin: dict) -> None:
    """Subscribe the bound origin chat to its card (platform 'hub', ``<gw>/<profile>/<session>``) in the
    caller's transaction. [daveey] The courier path stored the binding but no subscription, so the origin
    chat on another gateway was never linked to the card and never got its done notice. Only the legacy
    ``kanban_origin.bind`` path subscribed. The chat comes from the binding, never from a process env var.
    Same row shape as ``kanban_db_notify.add_notify_sub``, which opens its own non-nested txn."""
    import time

    from hermes_cli.kanban_placement import is_placeholder_session
    if is_placeholder_session(origin['session']):
        return
    chat_id = f"{origin['gateway']}/{origin['profile']}/{origin['session']}"
    conn.execute(
        "INSERT OR IGNORE INTO kanban_notify_subs (task_id, platform, chat_id, thread_id, chat_type, "
        "notifier_profile, delivery_mode, created_at, last_event_id) VALUES (?, 'hub', ?, '', 'dm', ?, 'notify', ?, "
        "COALESCE((SELECT MAX(id) FROM task_events WHERE task_id = ?), 0))",
        (task_id, chat_id, origin["profile"], int(time.time()), task_id))


def _link_origin(conn, task_id: str, origin: dict, owner_gateway: str) -> None:
    """Write the origin chat's ``created`` chat link on the owner, in the caller's transaction. [daveey]

    01:09 PT 2026-11-13: four cards placed from a MacBook chat on workers had no ``task_chat_links`` row on the
    Board. This path wrote only the hub subscription, so the link came only from the origin's ledger reconcile,
    which travels through the ORIGIN's Board sync; that sync had given up its role (SchemaAhead), so the chat never
    saw the cards it placed. The owner's capture triggers push this row through the owner's sync. The uid is the one
    the origin's ledger reconcile writes (``kanban_chat_links.link_uid``), so when that sync recovers it upserts the
    same Board row rather than adding one; the owner's reconcile manages only its own chats' rows and never releases
    it, and the origin releases it when the card closes (``_owner_written_releases``). A chat on the owner's
    gateway gets none, whatever its profile: there the owner's reconcile is the chat's gateway, and it releases on
    its next pass any ``created`` row of that gateway it does not derive itself (a courier card has no
    ``creator_chats_resolved`` event, so ``_local_links`` does not keep it), so a row for another profile's chat
    would only be written to be released. Such a chat is reached by its ``hub`` subscription (``_subscribe_origin``);
    its task-list link is left to its own ledger reconcile, as before this link existed.

    A receipt-shaped origin (``local-<sha256>``, ``is_headless_session``) gets none either, unless an ancestor
    already proves it, as in ``kanban_creator_chats.initialize_ownership``: an unreleased ``created`` link proves a
    foreign chat (``proven_foreign_chats``), only the chat's gateway can tell a Desktop chat from a worker's own
    hash, and ``hermes-hub create-task`` defaults the origin to a worker's ``$HERMES_SESSION_ID``. That link still
    comes from the origin's ledger reconcile.

    Relation to ``kanban_chat_links._owner_hub_links``: that reconcile pass already publishes a row with the same uid
    (``created`` for a courier-bound origin) from the origin's ``hub`` subscription on this owner's next links pass,
    and skips the owner's own gateway the same way. This write only closes the window until that pass runs, so the
    chat's link reaches the Board with the card's own capture rather than one reconcile later."""
    import time

    from hermes_cli.kanban_chat_links import link_uid
    from hermes_cli.kanban_placement import is_placeholder_session
    from hermes_cli.kanban_session_provenance import is_headless_session, proven_foreign_chats
    if origin["gateway"] == owner_gateway or is_placeholder_session(origin["session"]):
        return
    chat_id = f"{origin['gateway']}/{origin['profile']}/{origin['session']}"
    if is_headless_session(chat_id) and chat_id not in proven_foreign_chats(conn, (task_id,)):
        return
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
        return
    conn.execute(
        "INSERT OR IGNORE INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
        "released, created_at) VALUES (?, ?, ?, ?, ?, 'created', 0, ?)",
        (link_uid(task_id, origin["gateway"], origin["profile"], origin["session"], "created"), task_id,
         origin["gateway"], origin["profile"], origin["session"], int(time.time())))


OWNER_WRITTEN = "owner_written"


def origin_link_fields(conn, task_id: str, origin: dict) -> dict:
    """``{"origin_chat_link": "owner_written"}`` for the courier result when this owner holds the origin chat's
    unreleased ``created`` link and its Board sync is enabled and not recorded stopped, so the link reaches the
    Board without the origin; ``{}`` otherwise. [daveey] The origin reads this answer instead of guessing from the
    chat id's shape: an owner on older core (no ``_link_origin``) or with its sync off or stopped writes nothing that
    reaches the Board, and a ``local-<sha256>`` chat an ancestor already proves does get a link.

    "Not recorded stopped" is ``kanban_sync_runstate.down()`` being None. There is no positive running marker (a
    completed round deletes the record), so an enabled sync that has not started yet also answers; its link row
    waits in this owner's own outbox and goes out with the first round, while a refusal to start or a crash records
    a stop and withholds the answer. The origin reads the field from its replica's ``done`` row, which the owner's
    hermes-hub builds as ``{**creation, ...}`` over this result, so the field survives the binding accept. Never
    raises."""
    try:
        from hermes_cli import kanban_db_sync as sync
        from hermes_cli import kanban_sync_runstate as runstate
        from hermes_cli.kanban_chat_links import link_uid
        if not sync.sync_enabled() or runstate.down():
            return {}
        uid = link_uid(task_id, origin["gateway"], origin["profile"], origin["session"], "created")
        row = conn.execute("SELECT 1 FROM task_chat_links WHERE uid = ? AND released = 0", (uid,)).fetchone()
    except Exception:  # health: allow BLE001 -- an unreadable answer is "not written", never a failed create
        return {}
    return {"origin_chat_link": OWNER_WRITTEN} if row else {}


def _insert_intent(conn, row: tuple) -> None:
    """Store the intent row and its synced copy in one transaction. The row is machine-local and its binding id is a
    random secret, so only the copy lets an owner restored from the Board rebuild it (``kanban_origin_intent_carry``)."""
    from hermes_cli.kanban_origin_intent_carry import record
    conn.execute("INSERT INTO origin_creation_intents VALUES (?,?,?,?,?)", row)
    request_id, intent_json, task_id, binding_json, child_request_id = row
    record(conn, task_id, request_id, intent_json, binding_json, child_request_id)


def create_with_origin(conn, intent: dict, options: dict) -> str:
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_origin_ledger import validate_binding

    try:
        if (not isinstance(intent, dict) or
                set(intent) not in ({"request_id", "owner", "origin"}, {"request_id", "owner", "origin", "creator"})):
            raise ValueError
        if not isinstance(intent["request_id"], str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", intent["request_id"]):
            raise ValueError
        if not isinstance(intent["owner"], dict) or set(intent["owner"]) != {"gateway", "profile", "board"}:
            raise ValueError
        owner = intent["owner"]
        if owner["board"] != (options["board"] or kb.get_current_board()):
            raise ValueError
        validate_binding({"binding_id": "validation", "version": 1, "publication_version": 1,
                          "owner": {**owner, "task_id": "validation"}, "origin": intent["origin"],
                          "task_record_key": f"{owner['profile']}/{owner['board']}/validation"})
    except (KeyError, TypeError, ValueError):
        raise OriginError("origin_creation_invalid", "invalid owner/origin creation intent") from None

    options = dict(options)
    options.pop("insert_guard", None)  # a courier create was overlap-checked on the gateway that asked for it
    # The sender gated its own declaration; kept out of the stored envelope so an intent recorded
    # before the option existed still matches on retry. [daveey]
    options.pop("require_declaration", None)
    options.pop("placement_checked", None)  # internal decision, not part of the immutable envelope
    # Courier ingress always enforces scope authority itself (below, after the origin is subscribed/linked);
    # the flag is never an envelope field and a caller cannot waive it. create_task must not check it early:
    # a courier card anchors its authority only after create, inside this txn. [daveey]
    options.pop("require_scope_authority", None)
    # Options added after intents were first stored: omit them while at their default (None) so an
    # intent stored before the option existed still matches on retry; a real value stays in the
    # envelope and a differing one remains a conflict.
    for _late in ("chat_origin", "placement_policy", "admission_frees", "self_work"):
        if options.get(_late) is None:
            options.pop(_late, None)
    if options.get("admission_class") == "normal":
        options.pop("admission_class")
    options["parents"] = list(options["parents"])
    if options["skills"] is not None:
        options["skills"] = list(options["skills"])
    immutable = _json({"intent": intent, "options": options})
    with kb.write_txn(conn, allow_nested=True):
        conn.execute("""CREATE TABLE IF NOT EXISTS origin_creation_intents (
            request_id TEXT PRIMARY KEY NOT NULL,
            intent_json TEXT NOT NULL,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            binding_json TEXT NOT NULL,
            child_request_id TEXT NOT NULL UNIQUE)""")
        from hermes_cli.kanban_origin_intent_carry import ensure_task_index
        ensure_task_index(conn)
        existing = conn.execute(
            "SELECT intent_json,task_id FROM origin_creation_intents WHERE request_id=?",
            (intent["request_id"],),
        ).fetchone()
        if existing:
            if existing["intent_json"] != immutable:
                raise OriginError("origin_creation_conflict", "request ID reused with a different creation intent")
            return existing["task_id"]
        # The receipt request ID, not legacy best-effort idempotency, owns this creation: never adopt an
        # unrelated existing task by the creator's key. The key is still stored on the new card, so a guard or
        # probe that looks the card up by it matches (it used to be dropped to NULL). [daveey] A live card that
        # already holds it is a typed refusal naming that card, never a duplicate and never an adoption; an
        # exact retry is answered by the request ID above. Checked inside this write txn, so no race.
        key = options.get("idempotency_key")
        if key:
            held = conn.execute(
                "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
                "ORDER BY created_at DESC LIMIT 1", (key,)).fetchone()
            if held:
                raise OriginError("origin_idempotency_conflict",
                                  f"idempotency_key {key!r} already names card {held['id']} on this board")
        if "creator" in intent:
            from hermes_cli.kanban_origin_native import _primary_scope, _receipt
            creator = intent["creator"]
            expected_creator = {**intent["owner"], "task_id": options.get("creator_task_id")}
            primary = tuple(intent["owner"][k] for k in ("gateway", "profile", "board"))
            binding = _receipt(conn, options.get("creator_task_id"))
            if (creator != expected_creator or _primary_scope(conn) != primary or binding is None
                    or binding["owner"] != creator or binding["origin"] != intent["origin"]):
                raise OriginError("origin_creator_unproven", "creator lacks an owner-local native attestation")
        # Exact historical replay above wins even if a parent has since vanished.
        # Validate under the same write lock, before calling code that can insert
        # a task. Generic exceptions (including postcommit ValueError) prove no
        # such absence and must retain the owner's pending semantics.
        # Same normalisation as create_task (falsy entries are never linked); the
        # stored immutable envelope keeps its original parents unchanged.
        missing = kb._missing_task_ids(conn, [p for p in options["parents"] if p])
        if missing:
            from hermes_cli.kanban_dependency_validation import missing_dependency_error
            raise OriginCreateRefused("origin_parent_unknown", str(missing_dependency_error(missing)))
        task_id = kb.create_task(conn, **options, placement_checked=True,  # courier retains owner placement
                                 settle_owner_chat=False)  # its origin chat is linked below, then required
        marker = {"binding_id": "binding-" + secrets.token_hex(16), "version": 1,
                  "owner": {**intent["owner"], "task_id": task_id}, "origin": intent["origin"],
                  "task_record_key": f"{intent['owner']['profile']}/{intent['owner']['board']}/{task_id}"}
        _insert_intent(conn, (intent["request_id"], immutable, task_id, _json(marker),
                              "origin-bind-" + secrets.token_hex(16)))
        _subscribe_origin(conn, task_id, intent["origin"])
        _link_origin(conn, task_id, intent["origin"], intent["owner"]["gateway"])
        # Requirement D, courier ingress: only after the valid origin is subscribed/linked may renewable scope be
        # anchored. Refusal raises inside write_txn, rolling back card, intent and subscription.
        from hermes_cli.kanban_create_authority import require
        require(conn, task_id, options.get("creator_task_id"))
        return task_id


def adopt_with_origin(conn, intent: dict, *, board: str) -> str:
    """Attach a receipt intent to an exact existing card, never mutate its state.

    The trusted owner adapter checks the effective gateway/profile/database.
    This transaction verifies board/task identity and freezes the operation for
    retries. Publication and origin acknowledgement remain separate gates.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_origin_ledger import validate_binding

    try:
        if not isinstance(intent, dict) or set(intent) != {"request_id", "owner", "origin"}:
            raise ValueError
        if not isinstance(intent["request_id"], str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", intent["request_id"]):
            raise ValueError
        owner = intent["owner"]
        marker = {"binding_id": "validation", "version": 1,
                  "owner": owner, "origin": intent["origin"],
                  "task_record_key": f"{owner['profile']}/{owner['board']}/{owner['task_id']}"}
        validate_binding({**marker, "publication_version": 1})
        if owner["board"] != board:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise OriginError("origin_adoption_invalid", "invalid owner/origin adoption intent") from None

    immutable = _json({"operation": "adopt", "intent": intent})
    task_id = owner["task_id"]
    with kb.write_txn(conn, allow_nested=True):
        conn.execute("""CREATE TABLE IF NOT EXISTS origin_creation_intents (
            request_id TEXT PRIMARY KEY NOT NULL,
            intent_json TEXT NOT NULL,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            binding_json TEXT NOT NULL,
            child_request_id TEXT NOT NULL UNIQUE)""")
        from hermes_cli.kanban_origin_intent_carry import ensure_task_index
        ensure_task_index(conn)
        existing = conn.execute(
            "SELECT intent_json,task_id FROM origin_creation_intents WHERE request_id=?",
            (intent["request_id"],),
        ).fetchone()
        if existing:
            if existing["intent_json"] != immutable:
                raise OriginError("origin_creation_conflict", "request ID reused with a different origin intent")
            return existing["task_id"]
        if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone():
            raise OriginError("origin_task_missing", "exact owner task does not exist")
        for row in conn.execute("SELECT request_id,binding_json FROM origin_creation_intents WHERE task_id=?", (task_id,)):
            try:
                previous = json.loads(row["binding_json"])
            except (TypeError, ValueError):
                raise OriginError("origin_adoption_conflict", "existing task binding is unreadable") from None
            if not isinstance(previous, dict):
                raise OriginError("origin_adoption_conflict", "existing task binding is unreadable")
            if previous.get("owner") == owner and previous.get("origin") == intent["origin"]:
                raise OriginError("origin_already_bound", "retry existing origin request " + row["request_id"])
        marker["binding_id"] = "binding-" + secrets.token_hex(16)
        _insert_intent(conn, (intent["request_id"], immutable, task_id, _json(marker),
                              "origin-bind-" + secrets.token_hex(16)))
        _subscribe_origin(conn, task_id, intent["origin"])
        _link_origin(conn, task_id, intent["origin"], intent["owner"]["gateway"])
        return task_id
