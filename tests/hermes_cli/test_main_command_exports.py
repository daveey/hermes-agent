"""Command-only model wizards must not enter the server startup closure."""
import importlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_import_main_does_not_load_wizards(tmp_path):
    root = Path(__file__).resolve().parents[2]
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import sys, importlib.abc\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        "class RefuseWizards(importlib.abc.MetaPathFinder):\n"
        " def find_spec(self, fullname, path=None, target=None):\n"
        "  if fullname in {'hermes_cli.model_setup_flows',"
        "'hermes_cli.model_setup_flows_custom','hermes_cli.model_setup_flows_azure',"
        "'hermes_cli.model_setup_flows_bedrock','hermes_cli.model_setup_flows_local',"
        "'hermes_cli.main_provider_setup'}:\n"
        "   raise AssertionError('eager wizard: ' + fullname)\n"
        "sys.meta_path.insert(0, RefuseWizards())\n"
        "import hermes_cli.main\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(probe)], cwd=root, capture_output=True,
        text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_all_exports_are_real_objects_and_can_be_repatched(monkeypatch):
    from hermes_cli import main

    assert len(main._COMMAND_ATTR_SOURCES) == 32
    for name, module_name in main._COMMAND_ATTR_SOURCES.items():
        monkeypatch.delitem(vars(main), name, raising=False)
        expected = getattr(importlib.import_module(module_name), name)
        assert getattr(main, name) is expected
        assert main._command_export(name) is expected
        replacement = object()
        monkeypatch.setitem(vars(main), name, replacement)
        assert main._command_export(name) is replacement
        monkeypatch.delitem(vars(main), name)
        assert main._command_export(name) is expected


def test_resolution_preserves_override_and_propagates_import_failure(monkeypatch):
    from hermes_cli import main

    name = "_model_flow_openrouter"
    monkeypatch.delitem(vars(main), name, raising=False)
    sentinel = object()

    def resolve(module):
        monkeypatch.setitem(vars(main), name, sentinel)
        return SimpleNamespace(**{name: object()})

    with monkeypatch.context() as scoped:
        scoped.setattr(importlib, "import_module", resolve)
        assert main.__getattr__(name) is sentinel
    monkeypatch.delitem(vars(main), name)

    def fail(module):
        raise ImportError("controlled missing wizard")

    with monkeypatch.context() as scoped:
        scoped.setattr(importlib, "import_module", fail)
        with pytest.raises(ImportError, match="controlled missing wizard"):
            main.__getattr__(name)
        assert name not in vars(main)
    with pytest.raises(AttributeError):
        main.__getattr__("not_a_command_export")


@pytest.mark.parametrize("provider,target", [
    ("openrouter", "_model_flow_openrouter"), ("moa", "_model_flow_moa"),
    ("ai-gateway", "_model_flow_ai_gateway"), ("nous", "_model_flow_nous"),
    ("openai-codex", "_model_flow_openai_codex"), ("xai-oauth", "_model_flow_xai_oauth"),
    ("qwen-oauth", "_model_flow_qwen_oauth"), ("minimax-oauth", "_model_flow_minimax_oauth"),
    ("custom", "_model_flow_custom"), ("llamacpp", "_model_flow_local"),
    ("anthropic", "_model_flow_anthropic"), ("kimi-coding", "_model_flow_kimi"),
    ("stepfun", "_model_flow_stepfun"), ("bedrock", "_model_flow_bedrock"),
    ("vertex", "_model_flow_vertex"), ("azure-foundry", "_model_flow_azure_foundry"),
])
def test_dispatch_resolves_patches_at_each_call(monkeypatch, provider, target):
    from hermes_cli import main

    flow = main._PROVIDER_MODEL_FLOWS[provider]
    for _ in range(2):
        calls = []
        monkeypatch.setattr(main, target, lambda *a, **k: calls.append((a, k)))
        flow("config", "model", "args")
        args = ("config",) if provider == "custom" else ("config", "model")
        kwargs = {"args": "args"} if provider in {"nous", "xai-oauth", "minimax-oauth"} else {}
        assert calls == [(args, kwargs)]
