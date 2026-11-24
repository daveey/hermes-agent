"""Quick-snapshot pruning after a size-capped or failed capture (hermes_cli.backup_prune)."""
import sqlite3

import pytest

from tests.hermes_cli.test_backup import _advance_backup_clock


class TestPruneAfterSnapshot:
    @pytest.fixture
    def hermes_home(self, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text("model:\n  provider: openrouter\n")
        conn = sqlite3.connect(str(home / "state.db"))
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, data TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('s1', ?)", ("x" * 4096,))
        conn.commit()
        conn.close()
        return home

    def test_oversized_db_still_prunes_snapshots_that_never_held_it(self, hermes_home):
        """A size-capped caller (pre-update: keep=1, 1 GiB cap) must not leak a
        snapshot per run. Older snapshots taken under the same cap never held
        the skipped DB, so they are no recovery source and are pruned; an older
        snapshot that does hold it survives however many runs follow.
        """
        from hermes_cli.backup import create_quick_snapshot, list_quick_snapshots

        complete_id = create_quick_snapshot(label="complete", hermes_home=hermes_home)
        assert (hermes_home / "state-snapshots" / complete_id / "state.db").exists()

        capped_ids = []
        for n in range(4):
            _advance_backup_clock()
            capped_ids.append(create_quick_snapshot(
                label=f"capped{n}", hermes_home=hermes_home, max_file_size=1024, keep=1
            ))
        assert all(capped_ids)

        snap_ids = {s["id"] for s in list_quick_snapshots(limit=100, hermes_home=hermes_home)}
        assert snap_ids == {complete_id, capped_ids[-1]}, snap_ids

    def test_failed_db_capture_still_suppresses_all_pruning(self, hermes_home, monkeypatch):
        """A DB that failed to capture keeps every older snapshot, as before."""
        from hermes_cli import backup

        first_id = backup.create_quick_snapshot(label="first", hermes_home=hermes_home)
        _advance_backup_clock()
        second_id = backup.create_quick_snapshot(label="second", hermes_home=hermes_home)
        _advance_backup_clock()

        monkeypatch.setattr(backup, "_safe_copy_db", lambda *a, **k: False)
        third_id = backup.create_quick_snapshot(label="failed", hermes_home=hermes_home, keep=1)

        snap_ids = {s["id"] for s in backup.list_quick_snapshots(limit=100, hermes_home=hermes_home)}
        assert {first_id, second_id} <= snap_ids
        assert third_id is None or third_id in snap_ids
