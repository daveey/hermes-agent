"""Execution DB identity, not caller board labels, owns worker capacity."""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import worker_compute_capacity as capacity
from tests.hermes_cli import test_worker_resource_admission as helpers
from tests.hermes_cli.test_kanban_hub_admission_grant_echo import claim

worker_rig = helpers.worker_rig


def _job(rig, pool, owner):
    name = capacity.owner_slice(*owner)
    group = pool / name
    group.mkdir()
    (group / 'cgroup.events').write_text('populated 1\n')
    directory = rig.home / 'worker-resources' / 'owners'
    directory.mkdir(exist_ok=True)
    (directory / f'{name}.json').write_text(json.dumps(owner))


@pytest.mark.parametrize('lane', ['ready', 'review'])
@pytest.mark.parametrize('echo', [False, True])
def test_same_owner_resume_at_capacity(worker_rig, lane, echo):
    rig, pool = worker_rig
    rig.fill(1)
    task = rig.card(lane)
    owner = capacity.owner_for_connection(rig.conn, task)
    _job(rig, pool, owner)
    if echo:
        rig.echo_between_gate_and_txn()
    assert claim(rig.conn, task, lane) is not None
    from hermes_cli.worker_resource_admission import occupied_count_for_host
    assert occupied_count_for_host() == 2


@pytest.mark.parametrize('lane', ['ready', 'review'])
def test_same_task_in_foreign_installation_does_not_resume(worker_rig, tmp_path, lane):
    rig, pool = worker_rig
    rig.fill(1)
    task = rig.card(lane)
    _job(rig, pool, capacity.owner_for_db(tmp_path / 'foreign' / 'kanban.db', task))
    assert claim(rig.conn, task, lane) is None


def test_db_pin_alias_and_managed_owner_ignore_board_label(worker_rig, tmp_path, monkeypatch):
    rig, _ = worker_rig
    task = rig.card('ready')
    owner = capacity.owner_for_connection(rig.conn, task)
    alias = tmp_path / 'alias.db'
    alias.symlink_to(owner[0])
    assert capacity.owner_for_db(alias, task) == owner
    seen = []
    name = capacity.owner_slice(*owner)
    monkeypatch.setattr(capacity, 'configure_card', lambda *args: seen.append(args) or name)
    policy = SimpleNamespace(kanban_json=json.dumps({'board': 'wrong-label', 'db': str(alias), 'task_id': task}))
    assert capacity.managed_target(policy, tmp_path / name / 'run.scope')
    assert seen == [owner]


def test_dispatch_uses_pinned_execution_db_not_board_label(worker_rig, monkeypatch):
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_aggregate_scope as aggregate
    from tools import process_registry as processes
    rig, _ = worker_rig
    task = rig.card('ready')
    owner = capacity.owner_for_connection(rig.conn, task)
    monkeypatch.setenv('HERMES_KANBAN_DB', owner[0])
    monkeypatch.setattr(aggregate, 'configure_pool', lambda: None)
    monkeypatch.setattr(dispatch._worker_scope, 'apply_oom_recovery', lambda argv, *args: argv)
    monkeypatch.setattr(processes, 'restart_safe_gateway_child_argv', lambda argv, **kw: SimpleNamespace(
        argv=['systemd-run', '--user', '--scope', '--', *argv], mode='scope'))
    seen = []
    name = capacity.owner_slice(*owner)
    monkeypatch.setattr(capacity, 'configure_card', lambda *args: seen.append(args) or name)
    from hermes_cli import kanban_db as kb
    with kb.pin_first_board_resolution():
        argv = dispatch._restart_safe_worker_argv(SimpleNamespace(id=task, current_run_id=1), ['worker'], board='wrong-label')
    assert seen == [owner]
    assert f'--slice={name}' in argv
    assert argv[argv.index('--') + 1:] == ['nice', '-n', '15', 'worker']


@pytest.mark.parametrize('db', [None, '', 'relative.db'])
def test_managed_owner_requires_absolute_db(worker_rig, tmp_path, db):
    policy = SimpleNamespace(kanban_json=json.dumps({'board': 'default', 'db': db, 'task_id': 't_x'}))
    with pytest.raises(ValueError, match='database'):
        capacity.managed_target(policy, tmp_path / 'run.scope')
