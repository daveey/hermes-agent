"""The lazy-crypto fixture must not remove files inventoried as product source."""

import subprocess

import pytest

from tests.ci import test_update_ci_routing as routing
from tests.hermes_cli import test_lazy_secrets_dispatch as dispatch


@pytest.mark.live_system_guard_bypass
def test_crypto_probe_does_not_invalidate_product_inventory(monkeypatch):
    run = subprocess.run
    inventories = []

    def inventory_then_run(*args, **kwargs):
        # Pause the scanner after enumeration until the real probe has exited
        # and its fixture cleanup has run. No scheduler luck or sleeps required.
        inventories.append(routing._product_python())
        return run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", inventory_then_run)
    dispatch.TestUpdatePathE2E().test_main_update_check_crypto_absent_in_sys_modules()
    assert len(inventories) == 1
    assert routing._REPO / "hermes_cli/main.py" in inventories[0]
    for path in inventories[0]:
        # Bypass memoization so a previous ownership test cannot mask deletion.
        routing._imports.__wrapped__(path)
