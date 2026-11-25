"""Warm work is synchronously owned, with bounded admission and one shared cap."""
import threading
import time

from hermes_cli import always_on_rollover as ro
from tests.hermes_cli import test_always_on_sessions_promotion as native

home = native.home


def test_warm_stops_submitting_at_deadline_and_joins_only_six_inflight(home):
    roller = ro.Roller(token="fixture", checkout=None, marker=home / "marker", warm_cap_s=.12)
    lock = threading.Lock()
    started, finished, inflight, maximum = [], [], [0], [0]

    def get(port, path, timeout):
        with lock:
            started.append((path, timeout))
            inflight[0] += 1
            maximum[0] = max(maximum[0], inflight[0])
        threading.Event().wait(timeout)  # health: allow HX013 -- model an admitted transport exhausting its own deadline
        with lock:
            inflight[0] -= 1
            finished.append(path)
        return 200, timeout, {}

    roller.get = get
    began = time.monotonic()
    roller.warm(9148, [f"/api/fixture/{n}" for n in range(42)])
    assert time.monotonic() - began < .35
    assert 1 <= len(started) <= 6
    assert maximum[0] <= 6
    assert len(finished) == len(started) and inflight[0] == 0
    assert all(0 < timeout <= .12 for _, timeout in started)


def test_board_discovery_and_both_rounds_share_postpromotion_deadline(home):
    roller, _host = native.native_roller(home)
    roller.warm_cap_s = .12
    original = roller.get
    broad = []

    def get(port, path, timeout):
        if path == "/api/plugins/kanban/boards":
            broad.append((path, timeout))
            assert ro.read_active()["port"] == port
            roller.sleep(timeout)
            return 200, timeout, {"boards": [{"slug": "fixture"}]}
        if path not in (native.LIST, native.SIDEBAR, "/api/health", "/api/host/identity", "/api/ssh/ownership"):
            broad.append((path, timeout))
        return original(port, path, timeout)

    roller.get = get
    assert roller.roll("fixture") is True
    assert len(broad) == 1  # discovery used the entire cap: no queued warm rounds
    assert broad[0][0] == "/api/plugins/kanban/boards"
    assert 0 < broad[0][1] <= .12
