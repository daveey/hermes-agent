"""Deterministic selection for separate regular and marked-only hub pools."""
MARKED = ("capacity", "urgent")


def select_pool(admission_class, *, regular, priority, regular_cap, priority_cap):
    """Marked work prefers the extra pool; idle extra slots never admit normal work."""
    if admission_class in ("capacity", "urgent") and priority < priority_cap:
        return "priority"
    if regular < regular_cap:
        return "regular"
    return None


def board_pools(conn, *, own=None, excluded=(), origins=None):
    """Count local running rows by claim-time receipt; legacy NULL is regular."""
    from hermes_cli.machine_metrics import table_columns
    cols = table_columns(conn, "task_runs")  # one row step, not one per column (GIL convoy under the lock)
    receipt = "r.admission_pool" if "admission_pool" in cols else "NULL"
    where, params = "t.status='running'", ()
    if own is not None:
        where += " AND (t.claim_lock IS NOT NULL OR t.lease_holder IS NULL OR t.lease_holder=?)"
        params = (own,)
    klass = "r.admission_class" if "admission_class" in cols else "NULL"
    origin = "r.admission_origin" if "admission_origin" in cols else "NULL"
    return count_pools(conn.execute(
        f"SELECT t.current_run_id, {receipt}, {klass}, {origin} FROM tasks t LEFT JOIN task_runs r "
        f"ON r.id=t.current_run_id AND r.task_id=t.id WHERE {where}", params,
    ), excluded=excluded, origins=origins)


def count_pools(receipts, *, excluded=(), origins=None):
    """Pool counts for ``(run_id, pool, admission_class, origin)`` receipts of counted running rows (see
    :func:`board_pools`); ``origins`` collects capacity receipts per origin."""
    counts = {"regular": 0, "priority": 0}
    for run_id, pool, admission_class, origin_key in receipts:
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


def admission_policy() -> dict:
    """Root ``kanban.admission`` mapping (``{}`` when absent); malformed nesting raises ValueError."""
    from pathlib import Path
    from hermes_constants import get_default_hermes_root
    from hermes_cli.config_backend import read_config_doc_readonly

    try:
        doc = read_config_doc_readonly(Path(get_default_hermes_root()) / "config.yaml")
    except FileNotFoundError:
        return {}
    policy = {} if doc is None else doc
    for key in ("kanban", "admission"):
        if not isinstance(policy, dict):
            raise ValueError("admission configuration must be mappings")
        policy = policy.get(key, {})
    if not isinstance(policy, dict):
        raise ValueError("admission configuration must be mappings")
    return policy


def root_settings():
    """Opt-in root policy; absent priority_slots leaves the existing scalar gate intact."""
    policy = admission_policy()
    slots = policy.get("priority_slots", 0)
    ceiling = policy.get("priority_cpu_busy_below_pct", 85)
    if type(slots) is not int or slots < 0:
        raise ValueError("priority_slots must be a nonnegative integer")
    if type(ceiling) not in (int, float) or not 0 < ceiling <= 100:
        raise ValueError("priority CPU ceiling must be in (0,100]")
    return slots, ceiling


def score_enabled() -> bool:
    """Whether hub-specific pool diagnostics are enabled; scoring is shared by all executors."""
    from hermes_cli import machine_cpu_metrics as cpu

    return cpu.root_limit()[0] == cpu.ON and root_settings()[0] > 0


def lane_rows(conn, status):
    """Shared score order; marked-only capacity is enforced separately by PoolBudget."""
    from hermes_cli.kanban_db_dispatch_queries import lane_rows as query

    return query(conn, status)


class PoolBudget:
    """One tick's per-pool candidate budget on an opted-in hub. A normal row only ever fits a regular slot, so
    it is never attempted (no Board lease, no locked census) once the regular slots this tick saw are spent;
    a marked row is charged to a priority slot first. ``limit`` caps the total (max_spawn, memory, share).
    Advisory only: the locked claim recount stays the authority."""

    def __init__(self, regular, priority, limit=None):
        self.regular, self.priority, self.limit = max(0, regular), max(0, priority), limit

    @property
    def total(self):
        free = self.regular + self.priority
        return free if self.limit is None else min(free, self.limit)

    def admits(self, admission_class):
        if self.limit is not None and self.limit <= 0:
            return False
        return self.regular > 0 or (admission_class in MARKED and self.priority > 0)

    def take(self, admission_class):
        if admission_class in MARKED and self.priority > 0:
            self.priority -= 1
        else:
            self.regular -= 1
        if self.limit is not None:
            self.limit -= 1


def row_class(row):
    """A row's admission class; a schema without the column (older replica) reads as normal."""
    return (row["admission_class"] if "admission_class" in row.keys() else None) or "normal"


def pool_admits(result, row):
    budget = getattr(result, "admission_budget", None)
    return budget is None or budget.admits(row_class(row))


def pool_take(result, row):
    """Charge a spawned row to its pool; returns 1 so the caller's spawn count stays one expression."""
    budget = getattr(result, "admission_budget", None)
    if budget is not None:
        budget.take(row_class(row))
    return 1


def pickup_budget(result, spawn_budget):
    """Foreign pickup budget: the scalar on workers, a pool-aware budget capped to the tick's total on a hub."""
    budget = getattr(result, "admission_budget", None)
    if budget is None:
        return spawn_budget
    return PoolBudget(budget.regular, budget.priority, spawn_budget)


def dispatch_pools(regular_cap):
    """``(regular_free, priority_free, regular_used, priority_used)``; None when the extra pool is off."""
    from hermes_cli import machine_cpu_metrics as cpu, machine_metrics

    if cpu.root_limit()[0] != cpu.ON:
        return None
    slots, _ceiling = root_settings()
    if not slots:
        return None
    detail = {"pools": {"regular": 0, "priority": 0}}
    active = machine_metrics.active_card_runs(detail=detail)
    if active is None:
        return 0, 0, None, None
    counts = detail["pools"]
    return (max(0, regular_cap - counts["regular"]), max(0, slots - counts["priority"]),
            counts["regular"], counts["priority"])


def host_remaining(regular_cap, running_count, board, result=None):
    """Keep the legacy scalar budget when the extra hub pool is not enabled. When it is, record the per-pool
    budget on ``result`` and report a full regular pool as ``at_capacity`` even while marked-only priority
    slots stay free (an idle priority pool never admits normal work)."""
    from hermes_cli.kanban_db_dispatch import count_running_tasks_other_boards
    try:
        free = dispatch_pools(regular_cap)
    except (OSError, ValueError, TypeError):
        return 0
    if free is None:
        return regular_cap - running_count - count_running_tasks_other_boards(board)
    regular, priority, used, priority_used = free
    if result is not None:
        result.admission_budget = PoolBudget(regular, priority)
        if regular <= 0:
            slots = root_settings()[0]
            result.at_capacity = (f"regular pool max_in_progress={'?' if used is None else used}/{regular_cap} "
                                  f"(priority {'?' if priority_used is None else priority_used}/{slots} "
                                  "is marked-only)")
    return regular + priority


def queue_fields(regular_cap, admission_class, conn=None, task_id=None):
    """Advisory whole-host census; unknown counts are never rendered as free slots."""
    from hermes_cli import machine_cpu_metrics as cpu, machine_metrics

    if cpu.root_limit()[0] != cpu.ON:
        return {}
    slots, _ceiling = root_settings()
    if not slots:
        return {}
    detail = {"pools": {"regular": 0, "priority": 0}}
    active = machine_metrics.active_card_runs(detail=detail)
    counts = detail["pools"] if active is not None else {"regular": None, "priority": None}
    fields = {"admission_class": admission_class,
              "admission_pools": {**counts, "regular_cap": regular_cap, "priority_cap": slots}}
    if conn is not None and task_id is not None:
        from hermes_cli import kanban_admission_score as score

        rows = conn.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchall()
        fields["admission_score"] = score.rank(conn, rows)[0][1].as_dict() if rows else None
    return fields


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
