"""Producer attachment must mint local custody, not just projected ownership."""
import pytest

from hermes_cli import kanban_db as kb, kanban_origin_native as native
from hermes_cli import kanban_owner_chat as owner
from hermes_cli.kanban_origin_forward import authorize
from tests.hermes_cli import test_kanban_owner_chat as owner_fixtures

synced = owner_fixtures.synced
_fresh_cache = owner_fixtures._fresh_cache


@pytest.fixture(autouse=True)
def primary(synced, monkeypatch):
    from hermes_cli import kanban_db_sync as sync, kanban_origin
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("hub", {"hub"}))
    sync.set_mode(synced, "primary")
    assert native._primary_scope(synced) == ("hub", "default", "default")


def test_selfwork_owner_attachment_authorizes_worker_a2(synced, tmp_path):
    conn = synced
    assert native._primary_scope(conn) is not None, [tuple(r) for r in conn.execute("SELECT * FROM kanban_sync_state")]
    task = kb.create_task(conn, title="Selfwork intake", created_by="selfwork:hermes-core")
    sid = owner.session_id("selfwork:hermes-core")
    origin = {"gateway": "hub", "profile": "default", "session": sid}
    creator = {"gateway": "hub", "profile": "default", "board": "default", "task_id": task}
    binding = authorize(ledger=tmp_path / "absent.db", origin=origin, creator=creator,
        gateway="hub", profile="default", sender="gw:worker-a2", run_uid="run-a2",
        lease={"task_id": task, "holder": "worker-a2", "run_uid": "run-a2", "expires": 2000},
        now_ms=1000, session_exists=lambda value: value == sid,
        native_lookup=lambda o, c: native.lookup(o, c, gateway="hub"))
    assert binding["origin"] == origin
    assert binding["owner"] == creator
    assert binding["binding_id"] == "native-" + task


def test_link_projection_alone_does_not_mint_custody(synced):
    task = kb.create_task(synced, title="No producer", settle_owner_chat=False)
    sid = owner.ensure("selfwork:hermes-core")
    owner.link(synced, task, "selfwork:hermes-core", sid)
    assert native._receipt(synced, task) is None


def test_existing_custody_is_not_replaced_by_another_producer(synced):
    task = kb.create_task(synced, title="Original", created_by="original")
    binding = native._receipt(synced, task)
    assert binding is not None
    owner.attach(synced, task, "selfwork:hermes-core")
    assert native._receipt(synced, task) == binding
    other = binding["origin"] | {"session": owner.session_id("selfwork:hermes-core")}
    assert native.lookup(other, binding["owner"], gateway="hub") is None
    for field in ("profile", "board"):
        assert native.lookup(binding["origin"], binding["owner"] | {field: "other"}, gateway="hub") is None


def test_backfill_issues_only_for_newly_ensured_attachments(synced):
    historical = kb.create_task(synced, title="Historical", settle_owner_chat=False)
    sid = owner.ensure("old")
    owner.link(synced, historical, "old", sid)
    unowned = kb.create_task(synced, title="Needs owner", created_by="repair", settle_owner_chat=False)
    result = owner.backfill(synced, apply=True)
    assert result["linked"] == 1
    assert native._receipt(synced, historical) is None
    assert native._receipt(synced, unowned)["origin"]["session"] == owner.session_id("repair")
    assert owner.backfill(synced, apply=True)["linked"] == 0


def test_producer_custody_rolls_back_with_attachment(synced):
    task = kb.create_task(synced, title="Unowned", settle_owner_chat=False)
    synced.execute("SAVEPOINT attachment")
    owner.attach(synced, task, "selfwork:hermes-core")
    assert native._receipt(synced, task) is not None
    synced.execute("ROLLBACK TO attachment")
    assert native._receipt(synced, task) is None
    synced.execute("RELEASE attachment")


@pytest.mark.parametrize("mode", ["shadow", "replica", "off"])
def test_nonprimary_attachment_never_issues_custody(synced, mode):
    synced.execute("UPDATE kanban_sync_state SET mode=?", (mode,))
    task = kb.create_task(synced, title="Nonprimary", created_by="selfwork:hermes-core")
    assert native._receipt(synced, task) is None


def test_foreign_task_attachment_never_issues_custody(synced):
    task = kb.create_task(synced, title="Foreign", settle_owner_chat=False)
    synced.execute("UPDATE tasks SET created_on='foreign' WHERE id=?", (task,))
    owner.attach(synced, task, "selfwork:hermes-core")
    assert native._receipt(synced, task) is None


@pytest.mark.parametrize("holder", ["worker-a1", "worker-a3"])
def test_producer_receipt_does_not_relax_exact_worker_lease(synced, tmp_path, holder):
    task = kb.create_task(synced, title="Selfwork intake", created_by="selfwork:hermes-core")
    sid = owner.session_id("selfwork:hermes-core")
    with pytest.raises(ValueError, match="origin_forward_lease_invalid"):
        authorize(ledger=tmp_path / "absent.db",
            origin={"gateway": "hub", "profile": "default", "session": sid},
            creator={"gateway": "hub", "profile": "default", "board": "default", "task_id": task},
            gateway="hub", profile="default", sender="gw:worker-a2", run_uid="run-a2",
            lease={"task_id": task, "holder": holder, "run_uid": "run-a2", "expires": 2000},
            now_ms=1000, session_exists=lambda _: True,
            native_lookup=lambda o, c: native.lookup(o, c, gateway="hub"))
