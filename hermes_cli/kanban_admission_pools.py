"""Deterministic selection for separate regular and marked-only hub pools."""


def select_pool(admission_class, *, regular, priority, regular_cap, priority_cap):
    """Marked work prefers the extra pool; idle extra slots never admit normal work."""
    if admission_class in ("capacity", "urgent") and priority < priority_cap:
        return "priority"
    if regular < regular_cap:
        return "regular"
    return None


def board_pools(conn, *, own=None, excluded=(), origins=None):
    """Count local running rows by claim-time receipt; legacy NULL is regular."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(task_runs)")}
    receipt = "r.admission_pool" if "admission_pool" in cols else "NULL"
    where, params = "t.status='running'", ()
    if own is not None:
        where += " AND (t.claim_lock IS NOT NULL OR t.lease_holder IS NULL OR t.lease_holder=?)"
        params = (own,)
    counts = {"regular": 0, "priority": 0}
    klass = "r.admission_class" if "admission_class" in cols else "NULL"
    origin = "r.admission_origin" if "admission_origin" in cols else "NULL"
    for run_id, pool, admission_class, origin_key in conn.execute(
        f"SELECT t.current_run_id, {receipt}, {klass}, {origin} FROM tasks t LEFT JOIN task_runs r "
        f"ON r.id=t.current_run_id AND r.task_id=t.id WHERE {where}", params,
    ):
        if run_id in excluded:
            continue
        pool = "regular" if pool is None else pool
        if pool not in counts:
            raise ValueError("unknown admission pool receipt")
        counts[pool] += 1
        if origins is not None and admission_class == "capacity":
            if not origin_key:
                raise ValueError("capacity receipt has no origin")
            origins[origin_key] = origins.get(origin_key, 0) + 1
    return counts


def root_settings():
    """Opt-in root policy; absent priority_slots leaves the existing scalar gate intact."""
    from pathlib import Path
    from hermes_constants import get_default_hermes_root
    from hermes_cli.config_backend import read_config_doc_readonly

    try:
        doc = read_config_doc_readonly(Path(get_default_hermes_root()) / "config.yaml")
    except FileNotFoundError:
        return 0, 85
    policy = {} if doc is None else doc
    for key in ("kanban", "admission"):
        if not isinstance(policy, dict):
            raise ValueError("admission configuration must be mappings")
        policy = policy.get(key, {})
    if not isinstance(policy, dict):
        raise ValueError("admission configuration must be mappings")
    slots = policy.get("priority_slots", 0)
    ceiling = policy.get("priority_cpu_busy_below_pct", 85)
    if type(slots) is not int or slots < 0:
        raise ValueError("priority_slots must be a nonnegative integer")
    if type(ceiling) not in (int, float) or not 0 < ceiling <= 100:
        raise ValueError("priority CPU ceiling must be in (0,100]")
    return slots, ceiling


def lane_rows(conn, status):
    """Offer marked candidates before normal rows only on an opted-in hub."""
    from hermes_cli import machine_cpu_metrics as cpu
    from hermes_cli.kanban_db_dispatch_queries import lane_rows as query

    marked_first = cpu.root_limit()[0] == cpu.ON and root_settings()[0] > 0
    return query(conn, status, marked_first=marked_first)


def dispatch_remaining(regular_cap):
    """Upper bound for candidate attempts; final locked claims enforce each pool."""
    from hermes_cli import machine_cpu_metrics as cpu, machine_metrics

    if cpu.root_limit()[0] != cpu.ON:
        return None
    slots, _ceiling = root_settings()
    if not slots:
        return None
    detail = {"pools": {"regular": 0, "priority": 0}}
    active = machine_metrics.active_card_runs(detail=detail)
    if active is None:
        return 0
    counts = detail["pools"]
    return max(0, regular_cap - counts["regular"]) + max(0, slots - counts["priority"])


def host_remaining(regular_cap, running_count, board):
    """Keep the legacy scalar budget when the extra hub pool is not enabled."""
    from hermes_cli.kanban_db_dispatch import count_running_tasks_other_boards
    try:
        remaining = dispatch_remaining(regular_cap)
    except (OSError, ValueError, TypeError):
        return 0
    if remaining is None:
        return regular_cap - running_count - count_running_tasks_other_boards(board)
    return remaining


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
    detail = {"pools": {"regular": 0, "priority": 0}, "capacity_origins": {}}
    active = machine_metrics.active_card_runs(exclude=echo, detail=detail)
    if row is None or active is None:
        return None, ceiling, detail
    detail.update(active_before=active + detail.get("excluded", 0), active_after=active)
    if echo is not None and detail.get("excluded"):
        detail["excluded_run_uid"] = echo.run_uid
    from hermes_cli.kanban_admission_origin import origin_key
    origins = detail.pop("capacity_origins")
    if row[0] == "capacity" and origins.get(origin_key(conn, task_id), 0) >= 2:
        return None, ceiling, detail
    counts = detail["pools"]
    pool = select_pool(row[0], **counts, regular_cap=regular_cap, priority_cap=slots)
    return pool, ceiling, detail
