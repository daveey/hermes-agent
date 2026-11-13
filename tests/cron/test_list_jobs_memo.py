"""list_jobs memoizes the parsed jobs.json on its (mtime_ns, size, ino) and picks a write up on the next call.

Card t_01a1d59ed9eb75fe (design t_01a1d563324d7e8e §6, serve residue): the Desktop's cron poll re-parsed every
profile's jobs.json (and re-ran ensure_dirs) on every read. [daveey]
"""
import json

import pytest

import cron.jobs as cj


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    parses = []
    real = cj._parse_jobs_file
    monkeypatch.setattr(cj, "_parse_jobs_file", lambda path: parses.append(path) or real(path))
    return parses


def _names():
    return [j["name"] for j in cj.list_jobs(include_disabled=True)]


def test_an_unchanged_jobs_file_is_not_parsed_again(store):
    cj.create_job(prompt="first", schedule="every 1h", name="one")
    assert _names() == ["one"]
    parsed = len(store)
    assert _names() == ["one"]
    assert _names() == ["one"]
    assert len(store) == parsed


def test_a_write_is_picked_up_on_the_next_list(store):
    job = cj.create_job(prompt="first", schedule="every 1h", name="one")
    assert _names() == ["one"]
    cj.update_job(job["id"], {"name": "renamed"})
    assert _names() == ["renamed"]
    cj.create_job(prompt="second", schedule="every 2h", name="two")
    assert sorted(_names()) == ["renamed", "two"]
    cj.remove_job(job["id"])
    assert _names() == ["two"]


def test_a_hand_edit_with_a_new_inode_is_picked_up(store):
    cj.create_job(prompt="first", schedule="every 1h", name="one")
    assert _names() == ["one"]
    path = cj._current_cron_store().jobs_file
    data = json.loads(path.read_text())
    data["jobs"][0]["name"] = "two"  # same length: only the inode and mtime tell
    tmp = path.with_name("jobs.json.edit")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)
    assert _names() == ["two"]


def test_a_caller_mutating_the_list_does_not_change_the_next_answer(store):
    cj.create_job(prompt="first", schedule="every 1h", name="one")
    first = cj.list_jobs(include_disabled=True)
    first[0]["name"] = "mutated"
    first[0]["schedule"]["kind"] = "mutated"
    again = cj.list_jobs(include_disabled=True)
    assert again[0]["name"] == "one"
    assert again[0]["schedule"]["kind"] != "mutated"


def test_executions_stay_live_on_a_memo_hit(store, monkeypatch):
    """The memo covers jobs.json only: latest/live executions are read on every call."""
    job = cj.create_job(prompt="first", schedule="every 1h", name="one")
    cj.list_jobs(include_disabled=True)
    import cron.executions as ce
    monkeypatch.setattr(ce, "latest_executions", lambda ids: {job["id"]: {"id": "x1", "status": "completed"}})
    monkeypatch.setattr(ce, "live_executions", lambda ids: {})
    listed = cj.list_jobs(include_disabled=True)
    assert listed[0]["latest_execution"] == {"id": "x1", "status": "completed"}


def test_each_profile_store_has_its_own_memo(tmp_path):
    for name in ("a", "b"):
        with cj.use_cron_store(tmp_path / name):
            cj.create_job(prompt=f"job {name}", schedule="every 1h", name=f"job-{name}")
    for _ in range(2):
        for name in ("a", "b"):
            with cj.use_cron_store(tmp_path / name):
                assert _names() == [f"job-{name}"]


def test_memo_hits_are_p1_skips(store):
    from hermes_cli import loop_accounting as la
    cj.create_job(prompt="first", schedule="every 1h", name="one")
    before = la.counters().get("cron.list_jobs", {"runs": 0, "skips": 0})
    for _ in range(3):
        cj.list_jobs(include_disabled=True)
    after = la.counters()["cron.list_jobs"]
    assert after["runs"] - before["runs"] == 3
    assert after["skips"] - before["skips"] == 2
