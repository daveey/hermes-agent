"""Two-gateway pre-copy eligibility: unroutable cards consume no pickup budget."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_worker_node as support
from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml

svc = support.svc
gws = support.gws
PIN = [f"machine:{support.WORKER}"]
SYNC = "kanban:\n  sync:\n    enabled: true\n"


def _standard_only():
    """The fixture registry without its deep route: class:deep has no eligible route on this host."""
    lines = _managed_yaml().splitlines()
    start = lines.index("    deep:")
    end = lines.index("    standard:")
    return "\n".join(lines[:start] + lines[end:]) + "\n"


@pytest.fixture
def imports(monkeypatch):
    seen = []
    real = wn.import_cards

    def counting(conn, replica, ids):
        done = real(conn, replica, ids)
        seen.extend(done)
        return done
    monkeypatch.setattr(wn, "import_cards", counting)
    return seen


def _status(conn, tid):
    t = kb.get_task(conn, tid)
    return t.status if t else None


def _setup(gws, worker_yaml):
    hub, worker = gws
    (hub.home / "config.yaml").write_text(SYNC + _managed_yaml())
    (worker.home / "config.yaml").write_text(SYNC + "    pickup: true\n" + worker_yaml)
    return hub, worker


def test_deep_cards_unroutable_here_do_not_starve_or_churn(gws, imports):
    hub, worker = _setup(gws, _standard_only())
    hub.enter()
    deep = []
    for i in range(2):
        deep.append(kb.create_task(hub.conn, title=f"deep {i}", assignee="default", priority=1000 - i,
                                   runs_on=["linux"] if i % 2 else PIN,
                                   model_override="class:deep", provider_override="auto"))
    hub.round()
    pinned = support._hub_card(hub, title="pinned, eligible", priority=0, runs_on=PIN)
    worker.round()
    results = [worker.tick(max_spawn=2)]
    for _ in range(3):
        worker.round()
        results.append(worker.tick(max_spawn=2))
    print("IMPORTS", imports)
    print("SKIPPED", [r.skipped_ineligible for r in results])
    print("STATUS", {tid: _status(worker.conn, tid) for tid in deep + [pinned]})
    print("RUNS", {tid: len(kb.list_runs(worker.conn, tid)) for tid in deep + [pinned]})
    # A card local dispatch refuses before claim must not be copied again every tick (pinned start: printed only).
    assert all(imports.count(tid) == 0 for tid in deep), imports
    assert all(kb.list_runs(worker.conn, tid) == [] for tid in deep)
    assert len(kb.list_runs(worker.conn, pinned)) == 1
    assert imports.count(pinned) == 1
    assert all(_status(hub.conn, tid) == 'ready' for tid in deep)
    # Restoring eligible routes permits one normal import/run; no persisted hold to clear.
    (worker.home / 'config.yaml').write_text(SYNC + '    pickup: true\n' + _managed_yaml())
    worker.tick(max_spawn=3)  # one healthy sibling already occupies a slot
    assert all(imports.count(tid) == 1 for tid in deep)
    assert all(len(kb.list_runs(worker.conn, tid)) == 1 for tid in deep)


def test_ordinary_cards_on_a_worker_with_routing_disabled(gws, imports):
    hub, worker = _setup(gws, "model_routing:\n  enabled: false\n")
    tids = [support._hub_card(hub, title=f"ordinary {i}", priority=10, runs_on=["linux"]) for i in range(2)]
    worker.round()
    results = [worker.tick(max_spawn=2)]
    for _ in range(3):
        worker.round()
        results.append(worker.tick(max_spawn=2))
    print("IMPORTS", imports)
    print("SKIPPED", [r.skipped_ineligible for r in results])
    print("STATUS", {tid: _status(worker.conn, tid) for tid in tids})
    print("RUNS", {tid: len(kb.list_runs(worker.conn, tid)) for tid in tids})
    assert all(imports.count(tid) == 0 for tid in tids), imports
    assert all(kb.list_runs(worker.conn, tid) == [] for tid in tids)
    assert all(_status(hub.conn, tid) == 'ready' for tid in tids)
    (worker.home / 'config.yaml').write_text(SYNC + '    pickup: true\n' + _managed_yaml())
    worker.tick(max_spawn=2)
    assert all(imports.count(tid) == 1 for tid in tids)
    assert all(len(kb.list_runs(worker.conn, tid)) == 1 for tid in tids)
