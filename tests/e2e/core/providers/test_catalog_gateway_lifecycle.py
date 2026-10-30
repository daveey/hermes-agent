"""Catalog turns own the auto-started sandbox daemon as well as the short-lived CLI."""
import pytest

from gateway.status import live_gateway_pid_for_home
from tests.e2e.core.providers._catalog_helpers import FINAL, discover_catalog, drive_turn


# The sandbox daemon is reparented when its oneshot viewer exits; cleanup proves its home identity.
@pytest.mark.live_system_guard_bypass
def test_catalog_turn_stops_its_sandbox_gateway(tmp_path):
    catalog = discover_catalog()
    row = next(r for r in catalog if r.api_mode == "chat_completions" and r.skip_reason() is None)
    result = drive_turn(row, tmp_path, catalog)
    assert result.rc == 0 and FINAL in result.stdout, result.detail()
    home = tmp_path / "home" / ".hermes"
    assert live_gateway_pid_for_home(home) is None, "catalog row left its gateway running"
