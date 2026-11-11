"""Process observation contracts for the real-fetch custody assertions."""

from pathlib import Path

import pytest

from tests.hermes_cli import test_update_custody as custody


@pytest.mark.parametrize(("state", "exited"), [
    ("X", True), ("Z", True), ("R", False), ("S", False),
    ("D", False), ("T", False), ("t", False), ("", False),
])
def test_fetch_exit_observation(monkeypatch, state, exited):
    # No host PID lookup: retain a proc entry even for the rare Linux dead state.
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(Path, "read_text", lambda self, **kw: f"123 (git fetch) {state}")
    assert custody._fetch_exited(123) is exited


@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError, OSError])
def test_fetch_exit_observation_read_errors(monkeypatch, error):
    # The entry existed at a prior observation, but stat may disappear before read.
    monkeypatch.setattr(Path, "exists", lambda self: True)

    def read(self, **kw):
        raise error("proc observation")

    monkeypatch.setattr(Path, "read_text", read)
    if error is FileNotFoundError:
        assert custody._fetch_exited(123)
    else:
        with pytest.raises(error, match="proc observation"):
            custody._fetch_exited(123)
