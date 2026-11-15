"""Explicit cross-repository input; never import a host's installed courier."""
import importlib.util
from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption("--courier-client", help="Exact hermes-hub client source to test with this core")


@pytest.fixture
def courier(request, tmp_path, monkeypatch):
    source = request.config.getoption("--courier-client")
    if not source:
        pytest.skip("cross-repository test requires --courier-client PATH")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    spec = importlib.util.spec_from_file_location("transport_courier", Path(source).resolve())
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
