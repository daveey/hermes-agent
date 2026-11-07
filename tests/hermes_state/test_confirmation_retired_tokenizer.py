"""Fresh corruption confirmation stays fail-closed without the retired native tokenizer."""

import sqlite3

from hermes_state import SessionDB
from hermes_state_corruption_confirm import confirm_corruption


def test_unknown_tokenizer_fails_whole_check_but_scoped_shadow_check_works(tmp_path):
    path = tmp_path / "state.db"
    SessionDB(db_path=path).close()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE VIRTUAL TABLE custom_notes USING fts5(body, tokenize='unicode61')")
        conn.execute("INSERT INTO custom_notes(body) VALUES ('서울 notes')")
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute(
            "UPDATE sqlite_master SET sql = replace(sql, 'unicode61', 'retired_custom_tokenizer') "
            "WHERE name = 'custom_notes'"
        )
        version = conn.execute("PRAGMA schema_version").fetchone()[0]
        conn.execute(f"PRAGMA schema_version={version + 1}")
        conn.execute("PRAGMA writable_schema=OFF")

    whole = confirm_corruption(path, None, 10.0)
    assert whole.outcome == "error" and not whole.clean, whole
    scoped = confirm_corruption(path, frozenset({"custom_notes"}), 10.0)
    assert scoped.clean and "custom_notes_data" in scoped.scope, scoped
