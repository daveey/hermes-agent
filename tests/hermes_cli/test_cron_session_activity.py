"""cron_active stamping on session-list rows (sidebar periodic state). [daveey]"""

import json
from pathlib import Path

import pytest

from hermes_cli import cron_session_activity as csa


def _write_jobs(path: Path, jobs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"jobs": jobs}))


def _job(owner, **kw):
    base = {"id": owner or "x", "enabled": True, "state": "scheduled", "next_run_at": "2026-01-01T00:00:00",
            "owner_session_id": owner}
    base.update(kw)
    return base


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    csa._reset_cache_for_tests()
    yield tmp_path
    csa._reset_cache_for_tests()


def test_only_live_owned_jobs_count_across_profiles(home):
    _write_jobs(home / "cron" / "jobs.json", [
        _job("s_live"),
        _job("s_paused", state="paused"),
        _job("s_disabled", enabled=False),
        _job("s_done", state="completed", next_run_at=None),
        _job(None),
        {"id": "legacy", "enabled": True, "next_run_at": "x"},  # predates owner_session_id
    ])
    _write_jobs(home / "profiles" / "work" / "cron" / "jobs.json", [_job("s_prof")])

    assert csa.cron_owner_sessions() == {"s_live", "s_prof"}


def test_annotates_rows_by_lineage_and_clears_stale_stamps(home):
    _write_jobs(home / "cron" / "jobs.json", [_job("s_root")])
    rows = [
        {"id": "s_tip", "_lineage_root_id": "s_root", "_lineage_ids": ["s_root", "s_tip"]},
        {"id": "s_old", "cron_active": True},  # job removed since: stale stamp is cleared
        {"id": "s_none"},
    ]

    csa.annotate_cron_activity(rows)

    assert rows[0]["cron_active"] is True
    assert "cron_active" not in rows[1]
    assert "cron_active" not in rows[2]


def test_missing_or_corrupt_jobs_file_never_raises(home):
    (home / "cron").mkdir()
    (home / "cron" / "jobs.json").write_text("{not json")
    rows = [{"id": "s1"}]
    assert csa.annotate_cron_activity(rows) == [{"id": "s1"}]
