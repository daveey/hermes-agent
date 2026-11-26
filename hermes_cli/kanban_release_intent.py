"""Frozen native-owner release intents; no subprocess-supplied caller identity.

The local journal is not HOME release authority. Only the authenticated Board
operation may authorize these bytes; retaining them makes an uncertain send
recoverable without inventing a successor request or episode.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3

_COORDINATE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,200}\Z")
_ACTION = {"board", "task_id", "home", "episode", "capability_episodes", "runs", "note"}
_INTENT = _ACTION | {"version", "intent_uid", "actor"}
_ACTOR = {"gateway", "profile", "session"}
_TRANSITIONS = {
    "intent_journaled_unsent": {"send_pending"},
    "send_pending": {"authorized_pending_home", "expired", "refused"},
    "authorized_pending_home": {"read_first_pending", "consumed", "expired", "refused"},
    "read_first_pending": {"consumed", "expired", "refused"},
    "consumed": {"applied", "refused"},
    "applied": set(), "expired": set(), "refused": set(),
}


def encode(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: dict) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def _coordinate(value) -> bool:
    return isinstance(value, str) and _COORDINATE.fullmatch(value) is not None


def _action(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != _ACTION:
        raise ValueError("release action accepts only its exact bounded fields, never caller or journal identity")
    if not all(_coordinate(value[k]) for k in ("board", "task_id", "home", "episode")):
        raise ValueError("release action has an invalid coordinate")
    if type(value["runs"]) is not int or not 1 <= value["runs"] <= 9007199254740991:
        raise ValueError("release runs must be a positive wire-safe integer")
    if not isinstance(value["note"], str) or not value["note"].strip() or len(value["note"]) > 2000:
        raise ValueError("release intent needs a nonempty bounded note")
    selected = value["capability_episodes"]
    if (not isinstance(selected, list) or len(selected) > 32
            or not all(_coordinate(e) for e in selected)):
        raise ValueError("release historical capability selection must be a bounded coordinate list")
    if len(selected) != len(set(selected)) or value["episode"] in selected:
        raise ValueError("release historical selection must be unique and exclude the current episode")
    return {**value, "capability_episodes": sorted(selected)}


def validate(value: dict) -> dict:
    """Parse untrusted transport bytes; validation itself grants no authority."""
    if not isinstance(value, dict) or set(value) != _INTENT or type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("invalid release intent version or fields")
    actor = value["actor"]
    if (not _coordinate(value["intent_uid"]) or not isinstance(actor, dict) or set(actor) != _ACTOR
            or not all(_coordinate(actor[k]) for k in _ACTOR)):
        raise ValueError("invalid release intent identity")
    action = _action({k: value[k] for k in _ACTION})
    return {**action, "version": 1, "intent_uid": value["intent_uid"], "actor": dict(actor)}


def _saved(conn: sqlite3.Connection, key: str, *, by_uid=False):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='native_owner_release_intents'").fetchone():
        return None
    column = "intent_uid" if by_uid else "action_key"
    return conn.execute(f"SELECT intent_uid, body, digest, stage FROM native_owner_release_intents WHERE {column}=?", (key,)).fetchone()


def _resume(row, action, actor):
    if row[3] not in _TRANSITIONS:
        raise ValueError("release journal has an invalid stage")
    try:
        body = validate(json.loads(row[1]))
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("release journal is malformed; no new authority was issued") from exc
    if body["actor"] != actor:
        raise ValueError("release journal actor differs from the live native chat")
    if (row[0] != body["intent_uid"] or row[2] != digest(body)
            or {k: body[k] for k in _ACTION} != action or row[1] != encode(body)):
        raise ValueError("release journal identity or bytes changed; no new authority was issued")
    return body


def _current(conn, body):
    from agent.origin_turn_custody import CapturedOrigin, origin
    from hermes_cli.kanban_origin_native import _primary_scope

    qualified = origin()
    if qualified is None:
        raise ValueError("release requires live native owning-chat custody")
    captured = CapturedOrigin(qualified)
    body = validate(body)
    action = {k: body[k] for k in _ACTION}
    if _primary_scope(conn) != (qualified["gateway"], qualified["profile"], action["board"]):
        raise ValueError("native release journal requires its exact local primary board")
    row = _saved(conn, body["intent_uid"], by_uid=True)
    if row is None or _resume(row, action, qualified) != body:
        raise ValueError("release journal does not match this exact intent")
    if captured.validated() is None:
        raise ValueError("native release custody expired before journal access")
    return row, captured


def stage(conn: sqlite3.Connection, body: dict) -> str:
    """Read only a validated exact journal under freshly captured native custody."""
    return _current(conn, body)[0][3]


def advance(conn: sqlite3.Connection, body: dict, *, expected: str, stage: str) -> bool:
    """Durably CAS a forward transition; this is bookkeeping, NOT authority.

    Persist send_pending BEFORE the first network attempt. Uncertain attempts
    retain that identity; terminal records never re-open or mint a retry UID.
    An expired action remains one-shot unless a fresh native invocation explicitly
    acknowledges that exact expired UID. Its replacement is itself one-shot;
    replay of the acknowledgement returns that replacement, not a third intent.
    Never manufacture an expiry acknowledgement to recover network uncertainty.
    """
    from hermes_cli import kanban_db as kb

    if expected not in _TRANSITIONS or stage not in _TRANSITIONS[expected]:
        raise ValueError("invalid release journal transition")
    with kb.write_txn(conn):
        row, captured = _current(conn, body)
        if row[3] != expected:
            return False
        changed = conn.execute(
            "UPDATE native_owner_release_intents SET stage=? WHERE intent_uid=? AND stage=? AND digest=? AND body=?",
            (stage, row[0], expected, row[2], row[1]),
        ).rowcount
        if captured.validated() is None:
            raise ValueError("native release custody expired before journal commit")
    return changed == 1


def _replace_expired(conn, row, action, actor, expired_intent):
    if not _coordinate(expired_intent) or row is None:
        raise ValueError("an exact recorded expired intent must be acknowledged")
    old = _saved(conn, expired_intent, by_uid=True)
    if old is None or old[3] != "expired":
        raise ValueError("acknowledged intent is not definitively expired")
    _resume(old, action, actor)
    if row[0] == expired_intent:
        return True
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='native_owner_release_retries'").fetchone():
        raise ValueError("expired acknowledgement does not identify the current action")
    successor = conn.execute("SELECT successor_uid FROM native_owner_release_retries WHERE expired_uid=?", (expired_intent,)).fetchone()
    if successor is None or successor[0] != row[0]:
        raise ValueError("expired acknowledgement was already superseded")
    return False


def acknowledged_successor(conn: sqlite3.Connection, body: dict, expired_intent: str) -> bool:
    """An explicit ACK replay may resume only its already-recorded successor."""
    row, _ = _current(conn, body)
    return not _replace_expired(conn, row, {k: body[k] for k in _ACTION}, body["actor"], expired_intent)


def existing(conn: sqlite3.Connection, action: dict) -> dict | None:
    """Resolve this invocation's frozen journal before mutable canonical preflight.

    HOME is intentionally absent from the invocation: a rename or new task episode
    must not prevent reconciling the exact old request. Never select a child-named
    UID or use the current task row to decide historical identity.
    """
    fields = sorted(_ACTION - {"home"})
    action = _action({**action, "home": "pending-canonical-home"})
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='native_owner_release_intents'").fetchone():
        return None
    clauses = " AND ".join(f"json_extract(body,'$.{key}')=?" for key in fields)
    values = [encode(action[key]) if key == "capability_episodes" else action[key] for key in fields]
    try:
        rows = conn.execute("SELECT action_key,intent_uid,body,digest,stage FROM native_owner_release_intents "
                            f"WHERE length(action_key)=64 AND {clauses} LIMIT 2", values).fetchall()
    except sqlite3.OperationalError as exc:
        raise ValueError("native release invocation journal is unavailable or corrupt") from exc
    if len(rows) > 1:
        raise ValueError("ambiguous native release invocation journal")
    if not rows:
        return None
    row = rows[0]
    body = validate(json.loads(row[2]))
    full_action = {k: body[k] for k in _ACTION}
    if row[0] != digest(full_action):
        raise ValueError("release invocation journal key changed")
    _current(conn, body)  # exact live actor/primary board and immutable bytes
    return body


def prepare(conn: sqlite3.Connection, action: dict, *, gate, expired_intent=None) -> dict:
    """Journal or resume under fresh parent custody; admit before the first write.

    The parent resolves HOME and board before calling. The child cannot select
    an existing journal UID, actor or delivery watermark through this schema.
    No networking occurs here. A committed journal is inert without a fresh
    live capture of the exact same originating chat on every subsequent send.
    A revocation after the final validation can still leave an inert journal;
    this is not release authority and every later network send must revalidate.
    """
    from agent.origin_turn_custody import CapturedOrigin, origin
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_origin_native import _primary_scope
    from hermes_cli.kanban_db_sync import uuid7

    qualified = origin()
    if qualified is None:
        raise ValueError("release requires live native owning-chat custody")
    captured = CapturedOrigin(qualified)
    action = _action(action)
    key = digest(action)
    with kb.write_txn(conn):
        if _primary_scope(conn) != (qualified["gateway"], qualified["profile"], action["board"]):
            raise ValueError("native release journal requires its exact local primary board")
        row = _saved(conn, key)
        replace = expired_intent is not None and _replace_expired(conn, row, action, qualified, expired_intent)
        body = (_resume(row, action, qualified) if row is not None and not replace
                else validate({**action, "version": 1, "intent_uid": uuid7(), "actor": qualified}))
        if captured.validated() is None:
            raise ValueError("native release custody expired before journal admission")
        gate.admit(action["task_id"], intent_uid=body["intent_uid"])
        if replace:
            conn.execute("UPDATE native_owner_release_intents SET action_key=? WHERE action_key=? AND intent_uid=?",
                         (key + ":" + expired_intent, key, expired_intent))
            conn.execute("CREATE TABLE IF NOT EXISTS native_owner_release_retries (expired_uid TEXT PRIMARY KEY, successor_uid TEXT UNIQUE NOT NULL)")
            conn.execute("INSERT INTO native_owner_release_retries VALUES (?,?)", (expired_intent, body["intent_uid"]))
        if row is None or replace:
            conn.execute("""CREATE TABLE IF NOT EXISTS native_owner_release_intents (
                action_key TEXT PRIMARY KEY, intent_uid TEXT UNIQUE NOT NULL,
                body TEXT NOT NULL, digest TEXT NOT NULL, stage TEXT NOT NULL
            )""")
            conn.execute("INSERT INTO native_owner_release_intents VALUES (?,?,?,?,?)",
                         (key, body["intent_uid"], encode(body), digest(body), "intent_journaled_unsent"))
        if captured.validated() is None:
            raise ValueError("native release custody expired before journal commit")
    return body
