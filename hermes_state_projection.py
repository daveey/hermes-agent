"""Write-side revision for the session fields consumed by Desktop projections."""

# Heartbeat fields intentionally excluded: liveness uses session.active_list.
SESSION_SIGNATURE_FIELDS = (
    "id", "source", "session_key", "display_name", "model", "parent_session_id",
    "started_at", "ended_at", "end_reason", "message_count", "tool_call_count",
    "cwd", "git_branch", "git_repo_root", "title", "title_source", "profile_name",
    "archived", "pinned", "hidden", "last_read_at", "handoff_state",
)

# A content generation, not a count: in-place SQLite restores can replace a
# database with divergent content after exactly the same number of writes.
_CHANGE_SQL = """INSERT INTO state_meta(key, value) VALUES ('sessions_change_seq', lower(hex(randomblob(16))))
    ON CONFLICT(key) DO UPDATE SET value = excluded.value;"""
_UPDATE_FIELDS = ", ".join(SESSION_SIGNATURE_FIELDS)
_UPDATE_WHEN = " OR ".join(f"OLD.{field} IS NOT NEW.{field}" for field in SESSION_SIGNATURE_FIELDS)
SESSION_PROJECTION_TRIGGERS = {
    f"sessions_projection_{operation}":
    f"CREATE TRIGGER sessions_projection_{operation}\n{clause} ON sessions\n{predicate}BEGIN\n    {_CHANGE_SQL}\nEND"
    for operation, clause, predicate in (
        ("insert", "AFTER INSERT", ""),
        ("delete", "AFTER DELETE", ""),
        ("update", f"AFTER UPDATE OF {_UPDATE_FIELDS}", f"WHEN {_UPDATE_WHEN}\n"),
    )
}
SESSION_PROJECTION_SQL = "\n".join(
    f"DROP TRIGGER IF EXISTS {name};\n{sql.replace('CREATE TRIGGER ', 'CREATE TRIGGER IF NOT EXISTS ', 1)};"
    for name, sql in SESSION_PROJECTION_TRIGGERS.items()
)


# Installation and replacement of the triggers seed a fresh generation in the
# same write transaction, so stores upgraded without later session writes never
# share a value. Settled opens skip installation and therefore write nothing.
SEED_SESSION_PROJECTION_SQL = _CHANGE_SQL


def session_projection_installed(conn) -> bool:
    """True when all three projection triggers exist with the current bodies."""
    names = tuple(SESSION_PROJECTION_TRIGGERS)
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name IN (?, ?, ?)", names,
    ).fetchall()
    return dict(rows) == SESSION_PROJECTION_TRIGGERS


def session_projection_revision(conn):
    """Return the generation only when all current triggers and a generation exist.

    A missing generation is unavailable capability (None), never a shared zero:
    two stores without one cannot be told apart. The caller holds a read
    transaction across capability and generation probes.
    """
    if not session_projection_installed(conn):
        return None
    row = conn.execute("SELECT value FROM state_meta WHERE key='sessions_change_seq'").fetchone()
    return row[0] if row else None
