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


def _saved(conn: sqlite3.Connection, key: str):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='native_owner_release_intents'").fetchone():
        return None
    return conn.execute("SELECT intent_uid, body, digest FROM native_owner_release_intents WHERE action_key=?", (key,)).fetchone()


def _resume(row, action, actor):
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


def prepare(conn: sqlite3.Connection, action: dict, *, gate) -> dict:
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
        body = (_resume(row, action, qualified) if row is not None
                else validate({**action, "version": 1, "intent_uid": uuid7(), "actor": qualified}))
        if captured.validated() is None:
            raise ValueError("native release custody expired before journal admission")
        gate.admit(action["task_id"], intent_uid=body["intent_uid"])
        if row is None:
            conn.execute("""CREATE TABLE IF NOT EXISTS native_owner_release_intents (
                action_key TEXT PRIMARY KEY, intent_uid TEXT UNIQUE NOT NULL,
                body TEXT NOT NULL, digest TEXT NOT NULL, stage TEXT NOT NULL
            )""")
            conn.execute("INSERT INTO native_owner_release_intents VALUES (?,?,?,?,?)",
                         (key, body["intent_uid"], encode(body), digest(body), "intent_journaled_unsent"))
        if captured.validated() is None:
            raise ValueError("native release custody expired before journal commit")
    return body
