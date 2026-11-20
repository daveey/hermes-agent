"""Composed producer -> sender -> Board -> registry -> scale-in decision, through the real census.

Real: ``machine_persistent_jobs.collect``, the manifest census, lease adapter (real flock files), results gate
(real sqlite outbox + sync lock), ``fresh`` and ``send_compatible``. Stand-ins: the Board accepts the sample,
the registry stores it, and ``scale_in_allowed`` models the consumer contract (exact integer zero, fresh,
for the exact instance id). The real Board/registry/scaler live in the state repo and are NOT exercised here.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from hermes_cli import machine_persistent_jobs as jobs
from hermes_cli import worker_compute_capacity as capacity
from hermes_cli import worker_drain_census as census
from hermes_cli import worker_drain_fence as fence
from hermes_cli import worker_account_boards
from hermes_cli.kanban_db_sync import SYNC_SCHEMA_SQL
from hermes_cli.kanban_sync_guard import board_sync_lock
from hermes_cli.machine_drain_metrics import send_compatible

IID = 'i-0123456789abcdef0'
IDENT = fence.Identity(IID, 'boot-1', 'epoch-1')


class Board:
    """Accepts samples and keeps the newest per instance (the registry)."""

    def __init__(self):
        self.registry: dict = {}

    def send(self, iid):
        def _send(sample):
            self.registry[iid] = dict(sample, received_ms=int(time.time() * 1000))
            return {'ok': True}
        return _send


def scale_in_allowed(registry_entry, iid, expected_iid, now_ms, max_age_ms=5000):
    if iid != expected_iid or registry_entry is None:
        return False
    count, at = registry_entry.get('persistent_jobs'), registry_entry.get('persistent_jobs_at_ms')
    return type(count) is int and count == 0 and type(at) is int and 0 <= now_ms - at <= max_age_ms


@pytest.fixture
def node(tmp_path, monkeypatch):
    fence.install_state(tmp_path, IDENT)
    board = tmp_path / 'kanban.db'
    conn = sqlite3.connect(board)
    conn.executescript(SYNC_SCHEMA_SQL)
    conn.commit()
    conn.close()
    monkeypatch.setattr(capacity, 'policy_root', lambda: tmp_path)
    monkeypatch.setattr(capacity, 'occupied_owners', lambda *a, **k: set())
    monkeypatch.setattr(worker_account_boards, 'paths', lambda: [board])
    monkeypatch.setattr(fence, 'load_activation', lambda path=None: {'iid': IID})
    monkeypatch.setattr(census, 'observe_processes', lambda *a, **k: census.Observation(census.ZERO))
    return tmp_path, board


def produce_and_send(iid=IID):
    b = Board()
    send_compatible(b.send(iid), jobs.collect())
    return b


def test_idle_node_with_every_proof_reaches_scaler_as_zero(node):
    b = produce_and_send()
    sample = b.registry[IID]
    assert sample['persistent_jobs'] == 0
    assert scale_in_allowed(sample, IID, IID, int(time.time() * 1000))
    assert not scale_in_allowed(sample, IID, 'i-fedcba9876543210f', int(time.time() * 1000))  # other instance


def _insert_outbox_row(board):
    conn = sqlite3.connect(board)
    conn.execute("INSERT INTO kanban_sync_outbox(op_id,table_name,row_uid,op,fields,hlc,created_at) "
                 "VALUES ('o','task_runs','r','upsert','{}','h',1)")
    conn.commit()
    conn.close()


def _hold_sync_round(board, held):
    lock = board_sync_lock(board)
    lock.__enter__()
    held.append(lock)


@pytest.mark.parametrize('breaker', ['activation', 'process_unknown', 'lease', 'outbox_row', 'sync_round', 'board_missing'])
def test_any_missing_proof_never_reaches_scaler_as_zero(node, monkeypatch, breaker):
    root, board = node
    held = []
    breakers = {
        'activation': lambda: monkeypatch.setattr(fence, 'load_activation', lambda path=None: None),
        'process_unknown': lambda: monkeypatch.setattr(
            census, 'observe_processes', lambda *a, **k: census.Observation(census.UNKNOWN)),
        'lease': lambda: held.append(fence.acquire_lease(root, IDENT, 'live-work')),
        'outbox_row': lambda: _insert_outbox_row(board),
        'sync_round': lambda: _hold_sync_round(board, held),
        'board_missing': board.unlink,
    }
    breakers[breaker]()
    try:
        b = produce_and_send()
        sample = b.registry[IID]
        assert not scale_in_allowed(sample, IID, IID, int(time.time() * 1000))
        assert sample.get('persistent_jobs') != 0
    finally:
        for h in held:
            h.close() if hasattr(h, 'close') else h.__exit__(None, None, None)


def test_positive_work_is_a_positive_lower_bound_end_to_end(node):
    root, _ = node
    lease = fence.acquire_lease(root, IDENT, 'live-work')
    try:
        monkey_count = census.manifest_count()
        assert monkey_count == 1
    finally:
        lease.close()


def test_stale_sample_is_dropped_at_the_send_boundary(node, monkeypatch):
    sample = jobs.collect()
    assert sample['persistent_jobs'] == 0
    old = dict(sample, persistent_jobs_at_ms=sample['persistent_jobs_at_ms'] - 60000)
    b = Board()
    send_compatible(b.send(IID), old)
    assert 'persistent_jobs' not in b.registry[IID]
    assert not scale_in_allowed(b.registry[IID], IID, IID, int(time.time() * 1000))
