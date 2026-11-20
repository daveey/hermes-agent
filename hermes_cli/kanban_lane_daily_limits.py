"""Daily lane admission/spend caps and local-midnight release. [daveey]"""
import json
import time


def cards_today(conn, lane, *, now=None):
    """Count actual admissions, once per card/lane/day; re-laning is not admission."""
    from hermes_cli import kanban_lanes as lanes

    return conn.execute(
        "SELECT COUNT(DISTINCT task_id) FROM task_events WHERE created_at >= ? "
        "AND json_extract(payload, '$.lane') = ? AND (kind = 'lane_admitted' OR "
        "(kind = 'lane_assigned' AND (json_extract(payload, '$.admitted') = 1 OR "
        "(json_type(payload, '$.admitted') IS NULL AND json_type(payload, '$.from') IS NULL "
        "AND COALESCE(json_extract(payload, '$.would_gate'), 0) = 0 AND EXISTS "
        "(SELECT 1 FROM task_events c WHERE c.task_id=task_events.task_id AND c.kind='created' "
        "AND json_extract(c.payload, '$.status') IN ('ready', 'todo'))))))",
        (lanes.day_start(now), lane)).fetchone()[0]


def exempt(conn, lane, opts):
    from hermes_cli import kanban_lanes as lanes

    if opts.get("_stored_card") and not opts.get("creator_task_id"):
        return False  # Stored provenance never comes from the evaluating worker's environment.
    if any(p.search(opts.get("title") or "") for p in lane.gate_exempt):
        row = lanes._creator_row(conn, opts)
        return row is not None and not (row["status"] == "triage" and row["block_kind"] == lanes.GATE_KIND)
    return False


def reason(conn, pol, lane_name, opts, *, now=None):
    from hermes_cli import kanban_lanes as lanes
    from hermes_cli.kanban_admission_score import _alarm_key

    lane = pol.lanes[pol.lane_of(lane_name)]
    if (opts.get("created_by") in lanes.RECOVERY_CREATORS or opts.get("admission_class") == "urgent"
            or _alarm_key(opts.get("idempotency_key")) or exempt(conn, lane, opts)):
        return None
    now = time.time() if now is None else now
    if lane.usd_per_day is not None and lanes.spent_today(conn, pol, lane.name, now=now) >= lane.usd_per_day:
        return "limit:usd_per_day"
    if lane.cards_per_day is not None and cards_today(conn, lane.name, now=now) >= lane.cards_per_day:
        return "limit:cards_per_day"
    return None


def row_opts(conn, row):
    """A stored card as creation options, including the creator fields the exemptions read."""
    opts = dict(row)
    opts["_stored_card"] = True
    created = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='created' ORDER BY id LIMIT 1", (row["id"],)).fetchone()
    if created:
        opts["creator_task_id"] = json.loads(created[0] or "{}").get("creator_task_id")
    return opts


def release_due(conn, *, foreign=()):
    """Inside readiness's write txn: release only daily-limit parks, oldest first.

    Ordinary gated lanes still require admission. Foreign replicas and containment never release here.
    Each release writes an admission before testing the next card, reserving the remaining daily count.
    """
    from hermes_cli import kanban_lanes as lanes
    from hermes_cli import kanban_containment_connections as containment

    pol = lanes.enforced()
    if pol is None:
        return 0
    count = 0
    for row in conn.execute(f"SELECT * FROM tasks WHERE {lanes.HELD} ORDER BY created_at, id").fetchall():
        tid = row["id"]
        if tid in foreign or containment.held(conn, tid):
            continue
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
            (tid, lanes.GATED)).fetchone()
        if event is None or json.loads(event[0] or "{}").get("reason") not in (
                "limit:cards_per_day", "limit:usd_per_day"):
            continue
        opts = row_opts(conn, row)
        if lanes._gated(conn, pol, row["tenant"], opts) or reason(conn, pol, row["tenant"], opts):
            continue
        count += lanes.admit(conn, tid, actor="lane-limit-reset", reason="daily lane capacity available")
    return count
