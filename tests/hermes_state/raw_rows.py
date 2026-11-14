"""Raw-SQL row writer for tests that deliberately corrupt an update checkpoint's proof.

The public appenders refuse while a started turn is parked (t_01a1d6f94ac774a2), so a test that needs the
"somebody wrote after the tool suffix" corruption must bypass them. This is the corruption, not a writer.
"""
import time


def raw_append_message(db, session_id, role, content, **_ignored):
    """Insert one active row after the current tail, skipping every guard the public appenders run."""
    return db._execute_write(lambda c: c.execute(
        'INSERT INTO messages(session_id, role, content, timestamp) VALUES(?,?,?,?)',
        (session_id, role, content, time.time())).lastrowid)
