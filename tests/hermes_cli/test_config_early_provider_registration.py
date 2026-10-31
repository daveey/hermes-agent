"""Plugin registration before a first lazy env-table touch cannot replace a core entry."""
from tests.hermes_cli.test_config_lazy_env_registry import _py


def test_early_plugin_provider_cannot_overwrite_core_env_entry(tmp_path):
    result = _py('''
        import json
        import hermes_cli.config as c
        import providers
        from hermes_cli.config_provider_env import sync_registered_providers
        from types import SimpleNamespace
        table = c.OPTIONAL_ENV_VARS
        before = dict.__getitem__(table, "ACTUAL_API_KEY").copy()
        providers._REGISTRY["test-early"] = SimpleNamespace(
            name="test-early", display_name="Early plugin", env_vars=("ACTUAL_API_KEY", "EARLY_PLUGIN_KEY"),
            auth_type="api_key", signup_url="https://example.test")
        providers._SOURCES["test-early"] = "runtime"
        sync_registered_providers()
        after = dict.__getitem__(table, "ACTUAL_API_KEY")
        print("RESULT " + json.dumps({"retained": before == after,
                                      "added": "EARLY_PLUGIN_KEY" in table,
                                      "core": "EARLY_PLUGIN_KEY" in c.core_declared_env_names()}))
    ''', tmp_path)
    assert result == {"retained": True, "added": True, "core": True}
