"""Deterministic selection for separate regular and marked-only hub pools."""


def select_pool(admission_class, *, regular, priority, regular_cap, priority_cap):
    """Marked work prefers the extra pool; idle extra slots never admit normal work."""
    if admission_class in ("capacity", "urgent") and priority < priority_cap:
        return "priority"
    if regular < regular_cap:
        return "regular"
    return None


def board_pools(conn, *, own=None, excluded=()):
    """Count local running rows by claim-time receipt; legacy NULL is regular."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(task_runs)")}
    receipt = "r.admission_pool" if "admission_pool" in cols else "NULL"
    where, params = "t.status='running'", ()
    if own is not None:
        where += " AND (t.claim_lock IS NOT NULL OR t.lease_holder IS NULL OR t.lease_holder=?)"
        params = (own,)
    counts = {"regular": 0, "priority": 0}
    for run_id, pool in conn.execute(
        f"SELECT t.current_run_id, {receipt} FROM tasks t LEFT JOIN task_runs r "
        f"ON r.id=t.current_run_id AND r.task_id=t.id WHERE {where}", params,
    ):
        if run_id in excluded:
            continue
        pool = "regular" if pool is None else pool
        if pool not in counts:
            raise ValueError("unknown admission pool receipt")
        counts[pool] += 1
    return counts


def root_settings():
    """Opt-in root policy; absent priority_slots leaves the existing scalar gate intact."""
    from pathlib import Path
    from hermes_constants import get_default_hermes_root
    from hermes_cli.config_backend import read_config_doc_readonly

    try:
        doc = read_config_doc_readonly(Path(get_default_hermes_root()) / "config.yaml") or {}
    except FileNotFoundError:
        return 0, 85
    policy = (doc.get("kanban") or {}).get("admission") or {}
    slots = policy.get("priority_slots", 0)
    ceiling = policy.get("priority_cpu_busy_below_pct", 85)
    if type(slots) is not int or slots < 0:
        raise ValueError("priority_slots must be a nonnegative integer")
    if type(ceiling) not in (int, float) or not 0 < ceiling <= 100:
        raise ValueError("priority CPU ceiling must be in (0,100]")
    return slots, ceiling


def advisory_limit(conn, task_id, regular_limit):
    """Advisory may admit a marked candidate to final arbitration, never grant a slot."""
    slots, ceiling = root_settings()
    if not slots:
        return regular_limit
    row = conn.execute("SELECT admission_class FROM tasks WHERE id=?", (task_id,)).fetchone()
    return ceiling if row and row[0] in ("capacity", "urgent") else regular_limit


def candidate_pool(conn, task_id, regular_cap, *, echo=None):
    """Read current class and a whole-host receipt census; None means no admissible pool."""
    from hermes_cli import machine_metrics

    slots, ceiling = root_settings()
    if not slots:
        return None, None, None
    row = conn.execute("SELECT admission_class FROM tasks WHERE id=?", (task_id,)).fetchone()
    detail = {"pools": {"regular": 0, "priority": 0}}
    active = machine_metrics.active_card_runs(exclude=echo, detail=detail)
    if row is None or active is None:
        return None, ceiling, detail
    detail.update(active_before=active + detail.get("excluded", 0), active_after=active)
    if echo is not None and detail.get("excluded"):
        detail["excluded_run_uid"] = echo.run_uid
    counts = detail["pools"]
    pool = select_pool(row[0], **counts, regular_cap=regular_cap, priority_cap=slots)
    return pool, ceiling, detail
