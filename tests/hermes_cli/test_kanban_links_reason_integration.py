"""Real membership insertion and degraded-read custody with diagnostic counters."""
from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_sync_client as sync
from hermes_cli import kanban_links_diagnostics as diag
from tests.hermes_cli import test_kanban_origin_publications as membership
from tests.hermes_cli import test_kanban_links_refresh_accounting as refresh
from tests.hermes_cli import test_kanban_sync_links_chat_key as key_helpers

harness = refresh.harness
source = membership.source
kanban_home = key_helpers.kanban_home
ledger = key_helpers.ledger
live = key_helpers.live
scope = key_helpers.scope
spies = key_helpers.spies
clock = key_helpers.clock


def test_same_profile_insertion_refreshes_real_membership_and_publishes(source, harness, monkeypatch):
    _, origin = harness
    monkeypatch.setattr(activity, "_links_fingerprint", lambda: tuple(source.execute(
        "SELECT gw, key FROM replica WHERE coll='tasks'")))
    monkeypatch.setattr(activity, "_project_remote_links", lambda: [
        {"member": membership.reader(source).has_other(membership.owner(), {"created_at": 1})}])
    first = activity._links_lookup(origin, False)
    assert first[3] == [{"member": False}]
    membership.put(source)  # p/board/new under the already discovered profile p
    before = refresh.runs()
    second = activity._links_lookup(origin, False)
    assert second[3] == [{"member": True}]
    assert refresh.delta(before, "links_strict.miss.inputs") == 1
    assert refresh.delta(before, "links_strict.publish") == 1


def test_required_read_error_remains_uncertified_and_retries_by_original_age(
        live, ledger, kanban_home, spies, scope, clock):
    key_helpers._state(kanban_home, table=False)
    before = refresh.runs()
    key_helpers.helpers._settled(live, ledger, kanban_home, spies)
    name = diag.board_name("default")
    assert refresh.delta(before, name + ".certificate.degraded") >= 1
    assert sync._LINKS_DONE[str(kanban_home / "kanban.db")][2] is None
    clock[0] += sync.LINKS_MAX_AGE_S
    before = refresh.runs()
    key_helpers.helpers._pass(kanban_home)
    assert spies["reconcile"] == 1
    assert refresh.delta(before, name + ".reason.age") == 1
    assert refresh.delta(before, name + ".certificate.degraded") == 1
    assert sync._LINKS_DONE[str(kanban_home / "kanban.db")][2] is None
    assert refresh.delta(before, "sync.links.ledger_slices") == 1
    assert refresh.delta(before, "sync.links.enter") == refresh.delta(before, "sync.links.exit") == 1
