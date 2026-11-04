"""Owner notice for a worker-first card no pool worker can run (``no_eligible_worker``, or with overflow off the
Board's ``capability_mismatch``/``mixed`` rollup over the whole pool; see ``no_pool_worker``). [daveey]

Under the approved overflow plan (ruling 9) the Board no longer returns such a card to its origin: it stays
queued and fenced until its creator repins it. Without this module the only trace was a queue reason in
``kanban show``. Here the origin posts ONE comment per card revision with the exact repin command, appends a
``worker_repin_notice`` event (the gateway/TUI notifiers deliver it to the card's origin chat, when it has one),
and the dispatcher's stuck summary lists such cards as waiting for a manual repin instead of a lease stall.

Never repins: the notice only tells the creator what to run.

A "revision" is the card's required labels plus its run count, not the Board ``sync_version``: every Board
reserve of a queued card bumps that version, so keying on it would repeat the notice on every pass.
"""
from __future__ import annotations

import json

NOTICE_KIND = "worker_repin_notice"
MANUAL_REPIN = "waiting_for_manual_repin"
AUTHOR = "worker-placement"
CODE = "no_eligible_worker"


def _json_list(text):
    try:
        value = json.loads(text) if text else []
    except (TypeError, ValueError):
        return []
    return [v for v in value if isinstance(v, str) and v] if isinstance(value, list) else []


def required_labels(row):
    """The labels a pool worker must attest to run this card (mirrors the Board's ``requiredLabels``)."""
    row = dict(row)
    return ([f"profile:{row.get('assignee') or 'default'}"]
            + [f"capability:{r}" for r in _json_list(row.get("requires"))]
            + [f"skill:{s}" for s in _json_list(row.get("skills"))])


def observed_labels(value):
    """Missing labels the Board observation names across its candidates, sorted; [] when it names none."""
    labels = set()
    for candidate in (value or {}).get("candidates") or []:
        labels.update(candidate.get("missing_labels") or [])
    return sorted(labels)


# Board exclusion reasons that come from ``supports()`` (task metadata vs operator attestation): a worker with one
# of them cannot run the card until its labels change or the card is repinned; capacity and liveness reasons clear
# by themselves.
STRUCTURAL = frozenset({"missing_labels", "task_workspace_unsupported", "task_branch_unsupported",
                        "task_metadata_invalid", "task_requirement_unsupported"})


def no_pool_worker(value, pool=None):
    """True when a normalized queue observation says no configured pool worker can run the card.

    The Board computes ``no_eligible_worker`` only on its overflow path; with overflow off (the default) the same
    card queues with the per-candidate rollup ``capability_mismatch`` or ``mixed``. Those count only when every
    candidate carries a structural reason and, when ``pool`` is given, the candidates cover the whole pool.
    """
    if not isinstance(value, dict) or value.get("state") != "queued":
        return False
    if value.get("code") == CODE:
        return True
    if value.get("code") not in ("capability_mismatch", "mixed"):
        return False
    candidates = value.get("candidates") or []
    if not candidates or not all(STRUCTURAL & set(c.get("reasons") or []) for c in candidates):
        return False
    return pool is None or set(pool) <= {c.get("machine") for c in candidates}


def command(task_id, origin):
    return f'hermes kanban move {task_id} --runs-on machine:{origin} --reason "no pool worker can run it"'


def notice_text(payload):
    """Comment and chat line (gateway and TUI notifiers share it)."""
    payload = payload if isinstance(payload, dict) else {}
    task_id = str(payload.get("task_id") or "<id>")[:64]
    origin = str(payload.get("origin") or "<origin>")[:120]
    labels = ", ".join(str(v) for v in (payload.get("labels") or [])[:64]) or "unknown labels"
    return (f"No pool worker can run this card (needs: {labels}). It stays queued until its creator repins it: "
            f"`{command(task_id, origin)}` on {origin}, or until a pool worker is given those labels.")


def _revision(conn, task_id, row):
    runs = conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", (task_id,)).fetchone()[0]
    return {"required": required_labels(row), "run_count": runs}


def maybe_notify(conn, task_id, gateway, value, pool=None):
    """Post the notice for a normalized queued observation no pool worker can serve (``no_pool_worker``), once per
    card revision.

    Runs on every reserve reply, not only on a changed observation, so a card already queued when this code
    deploys gets its notice on the next pass. Same ownership fence as the observation record: only the origin
    gateway, only an unclaimed ready policy card. Returns True when it posted.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_worker_placement import POLICY

    if not no_pool_worker(value, pool):
        return False
    with kb.write_txn(conn):
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if (row is None or row["created_on"] != gateway or row["placement_policy"] != POLICY
                or row["status"] != "ready" or row["claim_lock"] or row["lease_holder"]
                or sync.board_mode(conn) != "primary"
                or (sync.sync_state(conn) or {}).get("gateway_id") != gateway):
            return False
        revision = _revision(conn, task_id, row)
        for (payload,) in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                                       (task_id, NOTICE_KIND)):
            if kb._json_dict(payload).get("revision") == revision:
                return False
        payload = {"task_id": task_id, "origin": gateway, "revision": revision,
                   "labels": observed_labels(value) or revision["required"]}
        kb._append_event(conn, task_id, NOTICE_KIND, payload)
        kb.add_comment(conn, task_id, AUTHOR, notice_text(payload), origin_gateway=gateway)
        return True


def awaiting(conn, task_id):
    """True while the card's current queue observation says no pool worker can run it and its owner was told.

    The notice is the pool-coverage proof: ``maybe_notify`` posted it only when the candidates covered the pool.
    """
    from hermes_cli import kanban_queue_reason as queue

    value = queue.latest(conn, task_id) or {}
    if not (value.get("current") and no_pool_worker(value)):
        return False
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return False
    revision = _revision(conn, task_id, row)
    from hermes_cli import kanban_db as kb
    return any(kb._json_dict(p).get("revision") == revision for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (task_id, NOTICE_KIND)))
