"""The links lane must explain CPU even on its unchanged-board path. [daveey]"""
from hermes_cli import loop_accounting as la
from tests.hermes_cli import test_kanban_sync_steady_links as helpers

kanban_home = helpers.kanban_home
ledger = helpers.ledger
live = helpers.live
scope = helpers.scope
spies = helpers.spies


def test_links_substeps_account_work_and_unchanged_pass(live, ledger, kanban_home, spies, scope):
    helpers._settled(live, ledger, kanban_home, spies)
    before = la.snapshot()["loops"]
    helpers._pass(kanban_home)
    after = la.snapshot()["loops"]
    for name in ("sync.links", "sync.links.sources", "sync.links.signature", "sync.links.inputs"):
        assert after.get(name, {}).get("runs", 0) == before.get(name, {}).get("runs", 0) + 1, name
    assert after["sync.links.board"]["skips"] == before["sync.links.board"]["skips"] + 1
    assert spies == {"reconcile": 0, "backfill": 0}


def test_links_reconcile_substeps_are_accounted(live, ledger, kanban_home, scope):
    before = la.snapshot()["loops"]
    helpers._seed(live, ledger)
    helpers._pass(kanban_home)
    after = la.snapshot()["loops"]
    for step in ("backfill", "plan", "local", "ledger", "owner_releases", "existing", "classify",
                 "machine_releases", "owner_links", "write"):
        name = f"sync.links.{step}"
        assert after.get(name, {}).get("runs", 0) > before.get(name, {}).get("runs", 0), name
