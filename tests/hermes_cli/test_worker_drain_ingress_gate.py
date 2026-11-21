"""No-ingress gate is independent of turn/task accounting."""
from types import SimpleNamespace
import os

import pytest


from hermes_cli import worker_drain_ingress as ingress
from hermes_cli.worker_drain_baseline import CaptureRefused

pytestmark = pytest.mark.platforms('linux')


def runner(*, enabled=False, live=False):
    return SimpleNamespace(config=SimpleNamespace(platforms={'relay': SimpleNamespace(enabled=enabled)}),
                           adapters={'relay': object()} if live else {},
                           _profile_configs={}, _profile_adapters={})


@pytest.fixture
def gate(monkeypatch, tmp_path):
    monkeypatch.setattr(ingress, 'revoke', lambda reason: None)
    monkeypatch.setattr(ingress, '_inventory_fd', None)
    monkeypatch.setattr(ingress, '_inventory_revoked', False)
    yield tmp_path
    if ingress._inventory_fd is not None:
        os.close(ingress._inventory_fd)
        ingress._inventory_fd = None


def test_no_ingress_config_and_actual_empty_maps_eligible(gate):
    ingress.attest_runner(runner(), [str(gate)])
    ingress.validate_ingress(os.getpid(), [str(gate)])


@pytest.mark.parametrize('kw', [{'enabled': True}, {'live': True}])
def test_any_enabled_or_live_adapter_is_unknown(gate, kw):
    ingress.attest_runner(runner(**kw), [str(gate)])
    with pytest.raises(CaptureRefused):
        ingress.validate_ingress(os.getpid(), [str(gate)])


def test_unreadable_running_list_is_unknown(gate):
    r = runner()
    del r.adapters
    ingress.attest_runner(r, [str(gate)])
    with pytest.raises(CaptureRefused):
        ingress.validate_ingress(os.getpid(), [str(gate)])


def test_secondary_enabled_or_live_adapter_is_unknown(gate):
    r = runner()
    r._profile_adapters = {'other': {'relay': object()}}
    ingress.attest_runner(r, [str(gate)])
    with pytest.raises(CaptureRefused):
        ingress.validate_ingress(os.getpid(), [str(gate)])


def test_inventory_missing_until_actual_runner_is_attested(gate):
    with pytest.raises(CaptureRefused):
        ingress.validate_ingress(os.getpid(), [str(gate)])


def test_config_change_invalidates_live_inventory(gate):
    ingress.attest_runner(runner(), [str(gate)])
    (gate / 'config.yaml').write_text('gateway:\n  platforms:\n    relay:\n      enabled: true\n')
    with pytest.raises(CaptureRefused):
        ingress.validate_ingress(os.getpid(), [str(gate)])


def test_unreadable_config_is_unknown(gate):
    (gate / 'config.yaml').write_text('gateway: [broken')
    assert not ingress.attest_runner(runner(), [str(gate)])
    with pytest.raises(CaptureRefused):
        ingress.validate_ingress(os.getpid(), [str(gate)])


def test_enabling_adapter_is_one_way_even_when_disabled_later(gate):
    assert ingress.attest_runner(runner(), [str(gate)])
    ingress.disable_ingress_exemption('adapter enabled')
    assert not ingress.attest_runner(runner(), [str(gate)])
    with pytest.raises(CaptureRefused):
        ingress.validate_ingress(os.getpid(), [str(gate)])


def test_adapter_creation_revokes_before_constructor(monkeypatch):
    from gateway.run_adapters import GatewayAdapterLifecycleMixin
    calls = []
    monkeypatch.setattr(ingress, 'disable_ingress_exemption', lambda reason: calls.append('revoke'))
    r = SimpleNamespace(_instantiate_adapter=lambda *args: calls.append('construct'))
    GatewayAdapterLifecycleMixin._create_adapter(r, 'relay', object())
    assert calls == ['revoke', 'construct']
