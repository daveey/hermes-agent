"""A call site that keeps opening writable SessionDB() handles is named in the log (hot-path writable-open guard)."""
import logging

import pytest

import hermes_state


@pytest.fixture(autouse=True)
def _fresh_counters(monkeypatch):
    monkeypatch.setattr(hermes_state, "_writable_opens", {})
    monkeypatch.setattr(hermes_state, "_writable_open_warned", {})


def test_warns_once_per_interval_when_a_site_reaches_the_threshold(caplog):
    caplog.set_level(logging.WARNING, logger="hermes_state")
    n = hermes_state._WRITABLE_OPEN_WARN_COUNT
    warned = [hermes_state._note_writable_open("plugin.poll:12", now=100.0 + i * 0.1) for i in range(n + 5)]
    assert warned.index(True) == n - 1 and warned.count(True) == 1
    assert "plugin.poll:12" in caplog.text and "read_only=True" in caplog.text
    # Still hot after the quiet interval: named again.
    later = 100.0 + hermes_state._WRITABLE_OPEN_WARN_INTERVAL_S + 1
    assert [hermes_state._note_writable_open("plugin.poll:12", now=later + i * 0.1) for i in range(n)][-1]


def test_spread_out_or_distinct_sites_stay_quiet():
    n = hermes_state._WRITABLE_OPEN_WARN_COUNT
    window = hermes_state._WRITABLE_OPEN_WINDOW_S
    assert not any(hermes_state._note_writable_open("cli.oneshot:1", now=i * (window / (n - 1) + 1)) for i in range(n * 2))
    assert not any(hermes_state._note_writable_open(f"site:{i}", now=1.0) for i in range(n * 2))


def test_writable_sessiondb_counts_its_caller_and_read_only_does_not(tmp_path):
    db_path = tmp_path / "state.db"
    hermes_state.SessionDB(db_path=db_path).close()
    sites = list(hermes_state._writable_opens)
    assert len(sites) == 1 and sites[0].endswith(
        "test_writable_sessiondb_counts_its_caller_and_read_only_does_not:" + sites[0].rsplit(":", 1)[1]
    )
    hermes_state.SessionDB(db_path=db_path, read_only=True).close()
    assert list(hermes_state._writable_opens) == sites and len(hermes_state._writable_opens[sites[0]]) == 1
