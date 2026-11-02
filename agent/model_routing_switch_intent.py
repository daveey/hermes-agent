"""A committed explicit model switch, carried to the next agent build as a one-shot allocator replace. [daveey]

The canonical ``model`` mutation (``hermes_state_mutation_prepared.model_in_transaction``) moves the
session row and its launch policy to the picked tuple, but the durable allocation store
(``model-routing.sqlite3``) still holds the previous route, possibly as an exact pin. Rebuilding the
agent on the picked tuple then collided with that pin ("Exact pinned route cannot fail over"), and
readers that trust the recorded route (``restore_route``) resumed the OLD model.

The receipt transaction therefore writes an intent row in ``state_meta`` beside the row update: it
exists only if the switch committed (a refused, guarded or deferred preparation never reaches that
transaction). The next build of exactly that session on exactly that tuple allocates with
``replace=True`` and then deletes the intent by compare-and-delete. Without a matching intent an
exact pin still refuses to move, so a stale row or a reader's guess cannot override it. A failed
allocation (credential prepare raised) rolls back in the allocation store and leaves the intent for
the next attempt.
"""
import json

PREFIX = "gateway.route_switch.v1."


def record_in_transaction(conn, session_id, model, provider, generation):
    """Write the intent inside the caller's receipt transaction (never on its own)."""
    value = json.dumps({"session_id": session_id, "model": model, "provider": provider or "",
                        "generation": generation}, sort_keys=True)
    conn.execute("INSERT INTO state_meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (PREFIX + session_id, value))


def pending(session_db, session_id, model, provider):
    """The raw intent row when it names exactly this session and tuple, else ``None``."""
    if session_db is None or not session_id or not model:
        return None
    # Both the owner's SessionDB and a managed worker's RuntimeSessionStore expose this one narrow read,
    # bound to the session it names (the worker's is owner-checked against its own assignment). A store
    # without it (a test double, a foreign wrapper) has no committed switch to honour; a store that has
    # it and fails raises, so a broken owner link is never mistaken for "no switch".
    read = getattr(session_db, "route_switch_intent", None)
    if not callable(read):
        return None
    raw = read(session_id)
    if not raw:
        return None
    try:
        intent = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(intent, dict) or intent.get("session_id") != session_id or intent.get("model") != model:
        return None
    wanted = intent.get("provider") or ""
    if wanted and provider not in (None, "", "auto", wanted):
        return None  # the build asks for another provider: not this switch
    return raw


def consume(session_db, session_id, raw):
    """Delete the intent only if it is still the one this build honoured (a newer switch survives)."""
    session_db.consume_route_switch_intent(session_id, raw)


def read_on_conn(conn, session_id):
    row = conn.execute("SELECT value FROM state_meta WHERE key=?", (PREFIX + session_id,)).fetchone()
    return row[0] if row is not None else None


def consume_on_conn(conn, session_id, raw):
    """Compare-and-delete; true only when this exact intent was the one removed."""
    return conn.execute("DELETE FROM state_meta WHERE key=? AND value=?",
                        (PREFIX + session_id, raw)).rowcount == 1


def worker_pending(db, conn, session_id, payload):
    """``worker.persist`` ``route_switch.pending``: the intent of the worker's own assigned session only."""
    from hermes_state_runtime import RuntimeStoreError
    if payload:
        raise RuntimeStoreError('invalid_params')
    return {'value': read_on_conn(conn, session_id)}


def worker_consume(db, conn, session_id, payload):
    """``worker.persist`` ``route_switch.consume``: CAS-delete the worker's own session intent."""
    from hermes_state_runtime import RuntimeStoreError
    if set(payload) != {'raw'} or not isinstance(payload['raw'], str):
        raise RuntimeStoreError('invalid_params')
    return {'value': consume_on_conn(conn, session_id, payload['raw'])}
