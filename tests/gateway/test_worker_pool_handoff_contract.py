"""Dependency-free handoff contract, also executable on updater Python versions."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


class WorkerPoolHandoff(unittest.TestCase):
    def test_resolver_pool_survives_for_each_subscription_provider(self):
        path = Path(__file__).resolve().parents[2] / 'gateway/session_worker_construct.py'
        spec = importlib.util.spec_from_file_location('worker_construct_contract', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for provider in ('anthropic', 'openai-codex'):
            with self.subTest(provider=provider):
                pool = object()
                runtime = {'provider': provider, 'api_key': 'fake-token', 'credential_pool': pool}
                module.route_runtime = lambda *a, **k: (runtime, None)
                policy = SimpleNamespace(effective=lambda: ({'model': {}}, 'test-model'))
                result = module.worker_route(policy, {'api_key': None})
                self.assertIs(result.get('credential_pool'), pool)
                self.assertEqual(result['api_key'], 'fake-token')


if __name__ == '__main__':
    unittest.main()
