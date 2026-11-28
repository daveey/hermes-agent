"""Dynamic stdlib code must not acquire provider identity from the inherited cwd."""
from collections import namedtuple
from dataclasses import make_dataclass
import importlib.machinery
import importlib.util
import py_compile

from hermes_cli.serve_producer_protocol import ProtocolError

import pytest

from hermes_cli import serve_producer_execution as execution
from tests.hermes_cli.test_serve_producer_shared_broker import manifest, produce, start, write_provider


@pytest.mark.parametrize('inside', [False, True])
@pytest.mark.parametrize('kind', ['namedtuple', 'dataclass', 'exec', 'relative'])
def test_dynamic_code_is_independent_of_provider_cwd(tmp_path, monkeypatch, inside, kind):
    root = tmp_path / 'provider'
    root.mkdir()
    path = root / 'provider.py'
    path.write_text('VALUE = 1\n')
    monkeypatch.chdir(root if inside else tmp_path)
    with execution.authenticated_execution(manifest(path), root, entry={}):
        if kind == 'namedtuple':
            assert namedtuple('Row', 'value')(7).value == 7
        elif kind == 'dataclass':
            assert make_dataclass('Row', [('value', int)])(7).value == 7
        else:
            namespace = {}
            code = 'value = 7' if kind == 'exec' else compile('value = 7', 'generated.py', 'exec')
            exec(code, namespace)
            assert namespace['value'] == 7


@pytest.mark.parametrize('filename', ['<string>', 'generated.py'])
def test_relative_loader_origin_still_authenticates_dynamic_filename(tmp_path, monkeypatch, filename):
    path = tmp_path / 'provider.py'
    path.write_text('VALUE = "stale"\n')
    py_compile.compile(str(path), cfile=str(tmp_path / 'hidden.pyc'), dfile=filename, doraise=True)
    path.write_text('VALUE = "fresh"\n')
    monkeypatch.chdir(tmp_path)
    with execution.authenticated_execution(manifest(path), tmp_path, entry={}):
        loader = importlib.machinery.SourcelessFileLoader('hidden', 'hidden.pyc')
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        with pytest.raises(ProtocolError, match='undeclared provider module executed'):
            loader.exec_module(module)
        assert not hasattr(module, 'VALUE')


@pytest.mark.platforms('linux')
@pytest.mark.parametrize('inside', [False, True])
def test_real_broker_namedtuple_with_inherited_provider_cwd(tmp_path, monkeypatch, inside):
    root = tmp_path / 'provider'
    root.mkdir()
    source = ('from collections import namedtuple\n'
              'def produce(payload):\n'
              '    row = namedtuple("Row", "value")(7)\n'
              '    return {"value": row.value}\n')
    spec = write_provider(root, source)
    monkeypatch.chdir(root if inside else tmp_path)
    relay = start(spec, tmp_path)
    try:
        assert produce(relay)['value'] == 7
    finally:
        assert relay.retire(timeout=10)
