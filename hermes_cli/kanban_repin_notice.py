"""Owner notice for a worker-first card no pool worker can run (``no_eligible_worker``). [daveey]

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


def maybe_notify(conn, task_id, gateway, value):
    """Post the notice for a normalized ``queued``/``no_eligible_worker`` observation, once per card revision.

    Runs on every reserve reply, not only on a changed observation, so a card already queued when this code
    deploys gets its notice on the next pass. Same ownership fence as the observation record: only the origin
    gateway, only an unclaimed ready policy card. Returns True when it posted.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli.kanban_worker_placement import POLICY

    if not isinstance(value, dict) or value.get("state") != "queued" or value.get("code") != CODE:
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
    """True while the card's current queue observation is the Board's ``no_eligible_worker`` queue."""
    from hermes_cli import kanban_queue_reason as queue

    value = queue.latest(conn, task_id) or {}
    return bool(value.get("current") and value.get("state") == "queued" and value.get("code") == CODE)
