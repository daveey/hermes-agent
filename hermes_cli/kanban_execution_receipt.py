"""Done-work observations from the landed completion-receipt seam; no store, writer or precedence.

A card is observed ``done`` only when ALL hold: the owner's completion verdict is verified (not the
legacy release basis), the admitted ``completion_receipt`` (``kanban_completion_receipt_projection``)
has every item ``delivered``, and receipt, verified completion event and closing run are one exact,
qualified execution. Anything else yields no record here: the caller's existing path keeps it
``unknown/unverified_completion``. Archive and cancellation are never success. The receipt is
re-derived from append-only owner rows; when freshness lapses ``project`` keeps it as history.
"""
from __future__ import annotations

from hermes_cli.execution_observations import epoch

DONE_TTL_SECONDS = 180


def _delivered(receipt):
    items = receipt.get("items") if isinstance(receipt, dict) else None
    return bool(items) and all(isinstance(i, dict) and i.get("disposition") == "delivered" for i in items)


def done_raw(tid, fact, bundle, *, board, gateway, profile, observed_at):
    """Raw v1 done observation for one card, or None when delivery is not established. Pure."""
    if fact is None or fact.status != "done" or not fact.verified or fact.legacy or not bundle:
        return None
    if not (gateway and profile and board and epoch(observed_at)):
        return None
    receipt, source = bundle["completion_receipt"], bundle["completion_receipt_source"]
    if not _delivered(receipt) or source.get("ref") != f"@task:{board}@{gateway}/{tid}":
        return None
    # One execution: the receipt's event and run are the verified completion's, which closed the latest run.
    if not (type(fact.event_id) is int and fact.event_id == source["event_id"]
            and fact.event_run_id is not None and fact.event_run_id == source["run_id"] == fact.run_id):
        return None
    if fact.run_uid:
        if source.get("run_uid") != fact.run_uid:
            return None
        run_id = fact.run_uid
    elif source.get("run_uid") is None:
        run_id = fact.run_id  # unsynced board: the local run id is the portable-enough attempt
    else:
        return None
    identity = {"gateway": gateway, "profile": profile, "board": board, "kind": "card", "work_id": tid,
                "run_id": run_id}
    ref = f"{source['ref']}#event:{source.get('event_uid') or source['event_id']}"
    owner = {"gateway": gateway, "profile": profile}
    if fact.assignee:
        owner["assignee"] = fact.assignee
    if fact.session_id:
        owner["session"] = fact.session_id
    return {"version": 1, "identity": identity, "owner": owner, "state": "done", "source": "kanban.card",
            "source_revision": ref, "evidence_refs": [ref], "observed_at": observed_at,
            "valid_until": observed_at + DONE_TTL_SECONDS, "last_activity": source["completed_at"],
            "last_activity_kind": "completed", "last_progress": None, "last_progress_kind": None,
            "last_progress_ref": None, "wait": None,
            "delivery_receipt": {"verified": True, "disposition": "delivered", "evidence_ref": ref,
                                 "identity": dict(identity)}}


def assemble_done(conn, ids, *, board, gateway, profile, observed_at):
    """``{task_id: raw}`` for the done cards among ``ids`` whose delivery is established.

    A fixed number of SELECTs per 500 cards inside the caller's read transaction, and only for ids
    the caller already knows are done. Must run on an authoritative LOCAL board, like
    ``kanban_execution_source.assemble``: a pulled mirror cannot attest its owner.
    """
    inputs = materialize_done(conn, ids, board=board, gateway=gateway, profile=profile)
    return derive_done(inputs, board=board, gateway=gateway, profile=profile, observed_at=observed_at)


def materialize_done(conn, ids, *, board, gateway, profile):
    """Materialize completion facts and admitted receipts in the caller's snapshot."""
    from hermes_cli.kanban_completion_inputs import read_completions
    from hermes_cli.kanban_completion_receipt_projection import read_completed_many

    ids = list(dict.fromkeys(ids))
    if not ids or not (gateway and profile):
        return {}, {}
    facts = read_completions(conn, ids)
    candidates = [tid for tid, f in facts.items() if f.status == "done" and f.verified and not f.legacy]
    bundles = read_completed_many(conn, candidates, board=board, gateway=gateway)
    return {tid: facts[tid] for tid in candidates}, bundles


def derive_done(inputs, *, board, gateway, profile, observed_at):
    """Pure observation derivation; does not own a source connection."""
    facts, bundles = inputs
    out = {}
    for tid in facts:
        raw = done_raw(tid, facts[tid], bundles.get(tid), board=board, gateway=gateway, profile=profile,
                       observed_at=observed_at)
        if raw is not None:
            out[tid] = raw
    return out


def counts_done(conn, *, board, gateway, profile, observed_at, counted_sub_clause, authority, window):
    """Sidebar/collector entry: ``(pairs, {task_id: raw})`` for done cards of linked sessions in ``window``.

    Reads inside the board's existing count transaction: two SELECTs for the (session, card) pairs and the
    constant batch of :func:`assemble_done`. Optional evidence: any adapter failure, or a non-authoritative
    mirror (``authority`` raises), yields nothing, so done is simply not observed (never inferred).
    """
    inputs = materialize_counts(conn, board=board, gateway=gateway, profile=profile, observed_at=observed_at,
                                counted_sub_clause=counted_sub_clause, authority=authority, window=window)
    return derive_counts(inputs, board=board, gateway=gateway, profile=profile, observed_at=observed_at)


def materialize_counts(conn, *, board, gateway, profile, observed_at, counted_sub_clause, authority, window):
    """Read the sidebar done bucket without deriving execution observations."""
    from hermes_cli.kanban_execution_observations import ADAPTER_ERRORS
    from hermes_cli.kanban_session_activity_recent_done import done_pairs_on

    try:
        authority(conn)
        pairs = done_pairs_on(conn, observed_at - window, counted_sub_clause)
        inputs = materialize_done(conn, {tid for _, tid in pairs}, board=board, gateway=gateway, profile=profile)
    except ADAPTER_ERRORS:
        import logging
        logging.getLogger(__name__).debug("done evidence unavailable for board %s", board, exc_info=True)
        return [], ({}, {})
    return pairs, inputs


def derive_counts(materialized, *, board, gateway, profile, observed_at):
    """Pure sidebar bucket derivation, retaining the optional-evidence fallback."""
    from hermes_cli.kanban_execution_observations import ADAPTER_ERRORS

    pairs, inputs = materialized
    try:
        raws = derive_done(inputs, board=board, gateway=gateway, profile=profile, observed_at=observed_at)
    except ADAPTER_ERRORS:
        import logging
        logging.getLogger(__name__).debug("done evidence unavailable for board %s", board, exc_info=True)
        return [], {}
    return [(sid, tid) for sid, tid in pairs if tid in raws], raws


def attach_done(conn, found, *, board, gateway, profile, observed_at):
    """Task-list entry: set ``_execution_done`` on listed done cards whose delivery is established.

    A failed read marks only those done cards ``_execution_error`` (explicit unknown); open cards and asks
    are untouched. A receipt is independent of the open-card read, so it is still read for rows that
    read failed; only a failed receipt read leaves them unknown.
    """
    from hermes_cli.kanban_execution_observations import ADAPTER_ERRORS

    ids = [tid for tid, row in found.items() if row.get("status") == "done"]
    if not ids:
        return
    try:
        raws = assemble_done(conn, ids, board=board, gateway=gateway, profile=profile, observed_at=observed_at)
    except ADAPTER_ERRORS:
        import logging
        logging.getLogger(__name__).debug("done evidence unavailable for board %s", board, exc_info=True)
        for tid in ids:
            found[tid]["_execution_error"] = True
        return
    for tid, raw in raws.items():
        found[tid]["_execution_done"] = raw
