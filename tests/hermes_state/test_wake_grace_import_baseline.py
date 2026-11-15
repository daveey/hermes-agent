"""The first lease check after suspend must use the sample taken at import."""

import importlib.util
import time

import pytest

import hermes_state_wake_grace as wake_grace
from hermes_platform import quiet_clock


@pytest.mark.parametrize("suspend_seconds", [0, 400])
def test_first_lapsed_row_check_uses_import_baseline(monkeypatch, suspend_seconds):
    suspended_ns = 1000 * 10**9
    monkeypatch.setattr(
        quiet_clock, "suspend_inclusive_ns",
        lambda: time.monotonic_ns() + suspended_ns,
    )
    # Execute a fresh module without resetting or sampling its tracker ourselves.
    # Keep it separate from the singleton used by SessionDB and neighbouring tests.
    spec = importlib.util.spec_from_file_location("_wake_grace_import_probe", wake_grace.__file__)
    assert spec is not None and spec.loader is not None
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    awake = 50_000.0
    monkeypatch.setattr(fresh, "_awake_now", lambda: awake)

    # The process stays alive across sleep, but makes no lease call before waking.
    suspended_ns += suspend_seconds * 10**9
    holder = "pid=123:turn=live"
    assert fresh.lapsed_row_reclaimable(holder, expires_at=300, now=500) is (suspend_seconds == 0)
    awake += fresh.WAKE_GRACE_SECONDS - 1
    assert fresh.lapsed_row_reclaimable(holder, expires_at=300, now=589) is (suspend_seconds == 0)
    awake += 2
    assert fresh.lapsed_row_reclaimable(holder, expires_at=300, now=591) is True
