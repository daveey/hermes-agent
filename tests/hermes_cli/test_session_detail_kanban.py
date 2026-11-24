"""Older sidebar rows restored through detail need the same task evidence as list rows."""
import asyncio

import pytest

from hermes_cli.web_routers import sessions


@pytest.mark.parametrize("count", [0, 16])
def test_detail_annotates_compression_lineage(monkeypatch, count):
    class DB:
        def resolve_session_id(self, sid):
            return sid

        def get_session(self, sid):
            return {"id": sid}

        def get_compression_chain(self, sid):
            return ["root", "tip"]

        def close(self):
            pass

    monkeypatch.setattr(sessions, "_open_session_db_for_profile", lambda *a, **kw: DB())
    monkeypatch.setattr(sessions, "_serving_profile", lambda p: p)
    from hermes_cli.web_routers import cron
    monkeypatch.setattr(cron, "cron_run_scheduler_owned", lambda *a: None)

    def annotate(rows, *, display=False):
        assert display is True  # detail is a sidebar restore, not a strict lifecycle read
        assert rows[0]["_lineage_ids"] == ["root", "tip"]
        rows[0]["kanban_observed_at"] = 1234
        if count:
            rows[0]["kanban_active"] = count
        return rows

    monkeypatch.setattr(sessions, "annotate_kanban_activity", annotate)
    row = asyncio.run(sessions.get_session_detail("tip", "default"))
    assert row["kanban_observed_at"] == 1234
    assert row.get("kanban_active", 0) == count
    assert row["profile"] == "default"
