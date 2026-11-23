"""Exact attrs producer/output custody; real audit events in fresh processes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

ROOT = Path(__file__).resolve().parents[2]
PRELUDE = r'''
import hashlib, importlib.metadata, json, pathlib, shutil, subprocess, sys
sys.path.insert(0, sys.argv[1])
from hermes_cli import code_attestation as ca
root = pathlib.Path(sys.argv[1])
a = ca.Attestation(root, subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip())
original = importlib.metadata.distribution('attrs')
SITE = pathlib.Path('env/lib/python3/site-packages').resolve()
SITE.mkdir(parents=True)
for name in ('attr', 'attrs'):
    shutil.copytree(original.locate_file(name), SITE / name)
info = SITE / original._path.name
shutil.copytree(original._path, info)
BASE = set(a.unknowns())
def activate():
    a.register_dependency_environment(SITE)
    sys.path.insert(0, str(SITE))
def report():
    print(json.dumps({'unknowns': sorted(set(a.unknowns()) - BASE),
                      'generated': [u for u in a.dependency_receipts()['units']
                                    if u.get('generator') == 'attrs:_compile_and_eval']}))
'''


def run(tmp_path, body):
    script = tmp_path / 'probe.py'
    script.write_text(PRELUDE + textwrap.dedent(body))
    result = subprocess.run([sys.executable, str(script), str(ROOT)], cwd=tmp_path,
                            env={'PATH': os.defpath, 'HOME': str(tmp_path),
                                 'TMPDIR': str(tmp_path), 'HERMES_HOME': str(tmp_path / 'home')},
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def test_exact_attrs_output_is_bound_to_observed_compile_and_exec(tmp_path):
    result = run(tmp_path, '''
        activate()
        import attrs
        @attrs.define
        class Box:
            value: int
        assert Box(7) == Box(7) and Box(7).value == 7
        report()
    ''')
    assert result['unknowns'] == []
    assert result['generated']
    for unit in result['generated']:
        assert unit['component'].startswith('dependency:attrs-')
        assert unit['producer_sha256'] == '1fb387d9e592e429c1ccb5233451355b299f3eb985d6bf1fbf644f76df4cb980'
        assert unit['producer_distribution']['version'] == '26.1.0'
        assert unit['producer_distribution']['record_sha256']


@pytest.mark.parametrize('change', ['skew', 'unreviewed', 'missing-record', 'missing-root'])
def test_attrs_source_custody_is_required(tmp_path, change):
    result = run(tmp_path, f'''
        change = {change!r}
        source = SITE / 'attr/_make.py'
        if change in ('skew', 'unreviewed'):
            data = source.read_bytes() + b'\\n# not the reviewed producer\\n'
            source.write_bytes(data)
            if change == 'unreviewed':
                import base64
                record = info / 'RECORD'
                rows = record.read_text().splitlines()
                digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
                rows = [f'attr/_make.py,sha256={{digest}},{{len(data)}}' if x.startswith('attr/_make.py,') else x for x in rows]
                record.write_text('\\n'.join(rows) + '\\n')
        if change == 'missing-record':
            (info / 'RECORD').unlink()
        if change == 'missing-root':
            sys.path.insert(0, str(SITE))
        else:
            activate()
        import attr
        report()
    ''')
    assert result['unknowns']
    assert result['generated'] == []


def test_filename_and_real_helper_are_not_generic_output_permissions(tmp_path):
    result = run(tmp_path, '''
        activate()
        import attr._make as make
        before = len([u for u in a.dependency_receipts()['units'] if u.get('generator') == 'attrs:_compile_and_eval'])
        make._compile_and_eval('sentinel = 1', {}, filename='<attrs generated forged>')
        make._linecache_and_compile('sentinel = 2', '<attrs generated forged indirect>', {})
        exec(compile('sentinel = 3', '<attrs generated forged raw>', 'exec'))
        new = [u for u in a.dependency_receipts()['units'] if u.get('generator') == 'attrs:_compile_and_eval']
        assert len(new) == before
        report()
    ''')
    assert result['generated']  # legitimate import-time attrs output still attested
    assert result['unknowns']


SEAM = r'''
activate()
import attr._make as make
from types import SimpleNamespace
from types import CodeType
# This seam injects malformed observations into a descriptor obtained by REAL
# module execution. The positive case above uses only actual audit events.
descriptor = a._generators[id(make._compile_and_eval.__code__)]
caller_code = descriptor.attrs_codes['_add_eq']
caller = SimpleNamespace(f_code=caller_code, f_back=None)
frame = SimpleNamespace(f_code=descriptor.code, f_back=caller,
                        f_lasti=next(k for k,v in descriptor.sites.items() if v == 'compile'),
                        f_locals={'script': 'sentinel = 1', 'filename': '<attrs seam>'})
# These helpers are called under the same suppression fence as the production
# audit callback: compiling the expected code must not become a new observation.
a._tls.busy = True
output = b'sentinel = 1'
code = compile(output, '<attrs seam>', 'exec', flags=0x1000000, dont_inherit=True)
frame.f_locals['bytecode'] = code
def fire(event, args):
    a._attrs_event(event, args, frame, descriptor)
def compile_event():
    fire('compile', (output, '<attrs seam>'))
def exec_event():
    frame.f_lasti = next(k for k,v in descriptor.sites.items() if v == 'eval')
    fire('exec', (code,))
BEFORE = len([u for u in a.dependency_receipts()['units'] if u.get('generator') == 'attrs:_compile_and_eval'])
'''


@pytest.mark.parametrize('case', [
    'wrong-site', 'absent-caller', 'forged-caller', 'unknown-caller', 'cached-forged',
    'cached-absent', 'identity', 'receipt', 'source', 'unreadable-source',
    'unreadable-record', 'record', 'script-type', 'filename-type', 'script-limit',
    'encoded-limit', 'compile-source', 'compile-filename', 'missing-compile',
    'different-frame', 'changed-output', 'changed-filename', 'changed-origin',
    'bytecode-identity', 'bytecode-output', 'bytecode-filename',
])
def test_attrs_output_guards_refuse_causal_skew(tmp_path, case):
    result = run(tmp_path, SEAM + textwrap.dedent(f'''
        case = {case!r}
        if case == 'wrong-site':
            frame.f_lasti = -1
        elif case == 'absent-caller':
            frame.f_back = None
        elif case == 'forged-caller':
            caller.f_code = caller_code.replace()
            a._known[id(caller.f_code)] = (caller.f_code, 'dependency:attrs-26.1.0')
        elif case == 'unknown-caller':
            a._known.pop(id(caller_code))
        elif case in ('cached-forged', 'cached-absent'):
            cached = descriptor.attrs_codes['_linecache_and_compile']
            frame.f_back = SimpleNamespace(f_code=cached.replace() if case == 'cached-forged' else cached, f_back=None)
        elif case == 'identity':
            a._deps.identity = lambda path: ('other', '26.1.0', 'attr/_make.py')
        elif case == 'receipt':
            a._deps.verify = lambda *args: None
        elif case == 'source':
            source = pathlib.Path(descriptor.code.co_filename)
            source.write_bytes(source.read_bytes() + b'\\n# changed\\n')
            a._deps.verify = lambda *args: ('attrs-26.1.0', descriptor.source_sha256)
        elif case == 'unreadable-source':
            pathlib.Path(descriptor.code.co_filename).unlink()
        elif case == 'unreadable-record':
            descriptor.record_path.unlink()
        elif case == 'record':
            descriptor.record_path.write_text('forged receipt')
        elif case == 'script-type':
            frame.f_locals['script'] = output
        elif case == 'filename-type':
            frame.f_locals['filename'] = None
        elif case == 'script-limit':
            frame.f_locals['script'] = 'x' * (ca.MAX_SOURCE + 1)
        elif case == 'encoded-limit':
            frame.f_locals['script'] = '\\u00e9' * ca.MAX_SOURCE
        elif case == 'compile-source':
            output = b'sentinel = 2'
        elif case == 'compile-filename':
            frame.f_locals['filename'] = '<forged>'
        if case != 'missing-compile':
            compile_event()
        if case == 'different-frame':
            frame = SimpleNamespace(**frame.__dict__)
        elif case == 'changed-output':
            frame.f_locals['script'] += '\\n'
        elif case == 'changed-filename':
            frame.f_locals['filename'] = '<forged>'
        elif case == 'changed-origin':
            a._known[id(caller_code)] = (caller_code, 'dependency:other')
        elif case == 'bytecode-identity':
            frame.f_locals['bytecode'] = code.replace()
        elif case in ('bytecode-output', 'bytecode-filename'):
            code = compile(b'sentinel = 2' if case == 'bytecode-output' else output,
                           '<forged>' if case == 'bytecode-filename' else '<attrs seam>',
                           'exec', flags=0x1000000, dont_inherit=True)
            frame.f_locals['bytecode'] = code
        exec_event()
        assert a._tls.attrs_output is None
        after = [u for u in a.dependency_receipts()['units'] if u.get('generator') == 'attrs:_compile_and_eval']
        assert len(after) == BEFORE
        a._tls.busy = False
        report()
    '''))
    assert any(reason.startswith('attrs-') for reason in result['unknowns']), result
