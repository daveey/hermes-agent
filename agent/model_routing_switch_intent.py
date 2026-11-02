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

Reading the intent, allocating and compare-deleting it span two stores, and builds of one session can
overlap (a cold ``/review`` builds a same-session agent while the durable queue drains the next turn; the
adapter's idle guard reserves nothing). The allocator therefore keys the replacement to the intent itself:
``fence_token`` is passed as ``replace`` and recorded beside the route in the allocation transaction, so an
intent replaces a lineage's class allocation at most once. An overlapping build, or a retry after a crash
between allocation and consume, re-reads the honoured route; a rolled-back allocation leaves no fence.
Consume still happens only after the allocation commits, never before it.

The fence is also an order. Builds that read an intent are not serialized against later switches: a build
can read intent A, a newer switch B can commit and be honoured by another build, and only then the A build
allocates. Keyed to equality alone, A (a different token) replaced B's allocation, its compare-delete then
missed, and the next build on B's exact tuple hit "Exact pinned route cannot fail over". And an identical
intent recorded again (a profile selection A -> B -> A within one runtime generation writes the same raw
row) was mistaken for the already-honoured one. So every recorded intent carries an ``event``: a store
nonce plus a sequence number from one counter in the same session store, taken inside the recording
transaction, so it is unique and follows commit order. The allocator replaces only when the presented
event is newer than the one the lineage class last honoured; an equal one is a retry or an overlapping
build (sticky), an older one is a superseded build (sticky: an exact pin then refuses it). Intents without
an event, or from another store, keep the equality fence.
"""
import hashlib
import json
import uuid

PREFIX = "gateway.route_switch.v1."
# One counter per session store: {"store": <nonce>, "seq": <last issued>}. Not a session key.
ORDER_KEY = "gateway.route_switch_order.v1"
_TOKEN = "switch-intent:"
_ORDERED = _TOKEN + "v2:"


def fence_token(raw):
    """The allocator's key for exactly this committed intent event, ordered when the intent carries one."""
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    event = _event(raw)
    if event is None:
        return _TOKEN + digest
    store, seq = event
    return f"{_ORDERED}{store}:{seq:020d}:{digest}"


def _event(raw):
    try:
        event = json.loads(raw).get("event")
        store, seq = event["store"], event["seq"]
    except (TypeError, ValueError, AttributeError, KeyError):
        return None
    if not isinstance(store, str) or not store or ":" in store or type(seq) is not int or seq < 1:
        return None
    return store, seq


def _order(token):
    if not isinstance(token, str) or not token.startswith(_ORDERED):
        return None
    parts = token[len(_ORDERED):].split(":", 2)
    if len(parts) != 3 or not parts[1].isdigit():
        return None
    return parts[0], int(parts[1])


def supersedes(token, honoured):
    """Whether intent ``token`` may replace an allocation that last honoured ``honoured`` (or None).

    Same store: strictly newer events only; an equal token is the same event (retry, overlapping build).
    Unordered or cross-store tokens keep the equality fence.
    """
    if honoured is None:
        return True
    if token == honoured:
        return False
    mine, theirs = _order(token), _order(honoured)
    if mine is not None and theirs is not None and mine[0] == theirs[0]:
        return mine[1] > theirs[1]
    return True


def _next_event(conn):
    row = conn.execute("SELECT value FROM state_meta WHERE key=?", (ORDER_KEY,)).fetchone()
    try:
        order = json.loads(row[0]) if row is not None else {}
        store, seq = order.get("store"), order.get("seq")
    except (TypeError, ValueError, AttributeError):
        store = seq = None
    if not isinstance(store, str) or not store or ":" in store or type(seq) is not int or seq < 0:
        store, seq = uuid.uuid4().hex, 0  # first intent in this store (or an unreadable counter)
    seq += 1
    conn.execute("INSERT INTO state_meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (ORDER_KEY, json.dumps({"store": store, "seq": seq}, sort_keys=True)))
    return {"store": store, "seq": seq}


def record_in_transaction(conn, session_id, model, provider, generation):
    """Write the intent inside the caller's receipt transaction (never on its own).

    Each call is a distinct, ordered event (module docstring), even for an identical tuple and generation.
    """
    value = json.dumps({"session_id": session_id, "model": model, "provider": provider or "",
                        "generation": generation, "event": _next_event(conn)}, sort_keys=True)
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
