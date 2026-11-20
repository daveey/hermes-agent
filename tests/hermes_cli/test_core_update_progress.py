"""Desktop's version pill reads the host's background core update through one route. [daveey]

hermes-hub 2026-11-19: hermes-sync drained for 12 min before applying and Desktop showed nothing but
"Waiting for a Hermes update" on the held message. ``GET /api/hermes/update/progress`` now reports the
published window (``cache/core-update/window.json``), the drainer's cap (``drain.json``) and what the
latest deferral waited on (``governor-other-defers.json``); anything unusable is ``null``, never an error.
"""
import asyncio
import json
import socket

import pytest

from hermes_cli import core_update_progress as cup

NOW = 1_795_131_600


@pytest.fixture
def cu(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "cache" / "core-update").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home / "cache" / "core-update"


def window(cu, **changes):
    data = dict(version=1, host=socket.gethostname().split(".")[0], observed_at=NOW - 5,
                window_at=NOW - 701, state="draining", state_since=NOW - 701, drain_bound_s=7200,
                last_update_at=NOW - 5000, writer="drainer")
    data.update(changes)
    (cu / "window.json").write_text(json.dumps(data))


def test_draining_reports_elapsed_cap_and_what_it_waits_on(cu):
    window(cu, state_since=NOW - 45)  # due 701 s ago; this drain pass began 45 s ago
    (cu / "drain.json").write_text(json.dumps({"pid": 1, "state": "draining", "total_cap_s": 60.0}))
    reasons = [f"default:2026111{i}_000000_aaaaa{i}" for i in range(10)]
    (cu / "governor-other-defers.json").write_text(json.dumps({"ts": NOW - 30, "reasons": reasons}))
    assert cup.read_progress(NOW) == {
        "state": "draining", "due_at": NOW - 701, "state_since": NOW - 45, "elapsed_s": 701.0,
        "state_elapsed_s": 45.0, "cap_s": 60.0, "waiting_on": reasons[:cup.MAX_WAITING_ON]}


def test_applying_has_no_waiting_list_and_batching_is_due_in_the_future(cu):
    window(cu, state="applying")
    (cu / "drain.json").write_text(json.dumps({"pid": 1, "state": "applying", "total_cap_s": 60.0}))
    (cu / "governor-other-defers.json").write_text(json.dumps({"ts": NOW, "reasons": ["default:x"]}))
    got = cup.read_progress(NOW)
    assert (got["state"], got["waiting_on"], got["cap_s"]) == ("applying", [], 60.0)

    window(cu, state="batching", window_at=NOW + 1860)
    (cu / "drain.json").write_text(json.dumps({"pid": 1, "state": "idle", "total_cap_s": 60.0}))
    got = cup.read_progress(NOW)
    assert (got["state"], got["elapsed_s"], got["cap_s"]) == ("batching", -1860.0, None)


@pytest.mark.parametrize("change", [
    {"state": "idle"},                       # nothing owed
    {"host": "some-other-host"},             # another host's record
    {"observed_at": NOW - 3600},             # stale publisher
    {"version": 2},                          # unknown schema
    {"window_at": "soon"},                   # malformed
])
def test_unusable_window_is_no_progress(cu, change):
    window(cu, **change)
    assert cup.read_progress(NOW) is None


def test_missing_files_and_stale_or_junk_side_records_degrade(cu):
    assert cup.read_progress(NOW) is None
    window(cu)
    (cu / "drain.json").write_text("not json")
    (cu / "governor-other-defers.json").write_text(json.dumps({"ts": NOW - 3600, "reasons": ["old"]}))
    got = cup.read_progress(NOW)
    assert (got["state"], got["cap_s"], got["waiting_on"]) == ("draining", None, [])


def test_route_serves_the_reader(cu, monkeypatch):
    from hermes_cli.web_routers import actions

    monkeypatch.setattr("time.time", lambda: NOW)
    assert asyncio.run(actions.hermes_update_progress()) == {"progress": None}
    window(cu)
    assert asyncio.run(actions.hermes_update_progress())["progress"]["state"] == "draining"
