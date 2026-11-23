"""Producer-source attribution is diagnostic, not trust in generated code."""
import textwrap

import pytest

from tests.hermes_cli import test_code_attestation_producers as helpers

SOURCE = r'''
SOURCE = b"""
def build():
    ns = {}
    exec(compile('def value(): return 42', '<attrs generated>', 'exec'), ns)
    return ns['value']
value = build()
"""
'''


def test_unknown_generator_retains_refusal_with_verified_source_attribution(tmp_path):
    data = helpers.run(tmp_path, SOURCE + textwrap.dedent(r'''
        dist('attrs', {'emitter.py': SOURCE})
        BASE = set(a.unknowns())
        activate()
        import emitter
        assert emitter.value() == 42
        receipts = a.dependency_receipts()
        before = a.generation()
        receipts['unknown_producer_sources']['unrooted-exec:<attrs generated>'][0]['component'] = 'changed'
        receipts = a.dependency_receipts()
        assert a.generation() == before
        print(json.dumps({'new': new(), 'sources': receipts['unknown_producer_sources'],
                          'gen': generated(), 'core': a.executed_units(),
                          'source_sha256': hashlib.sha256(SOURCE).hexdigest()}))
    '''))
    reason = 'unrooted-exec:<attrs generated>'
    assert data['new'] == [reason]
    assert data['gen'] == [] and data['core'] == []
    receipt, = data['sources'][reason]
    assert receipt['component'] == 'dependency:attrs-1.0'
    assert receipt['source_sha256'] == data['source_sha256']
    assert receipt['filename'].endswith('/emitter.py')
    assert receipt['qualname'] == 'build'
    assert receipt['trust'] == 'producer source only; generated code remains unknown'


@pytest.mark.parametrize('case', ['skew', 'filename-spoof', 'unreviewed'])
def test_unreviewed_producer_does_not_gain_source_receipt(tmp_path, case):
    data = helpers.run(tmp_path, SOURCE + f'\nCASE={case!r}\n' + textwrap.dedent(r'''
        dist('attrs', {'emitter.py': SOURCE})
        if CASE == 'skew':
            (SITE / 'emitter.py').write_bytes(SOURCE + b'\n# changed\n')
        BASE = set(a.unknowns())
        activate()
        if CASE == 'skew':
            import emitter
        else:
            filename = str(SITE / 'emitter.py') if CASE == 'filename-spoof' else '<unreviewed>'
            changed = SOURCE.replace(b'return 42', b'return 43')
            exec(compile(changed, filename, 'exec'), {})
        print(json.dumps({'new': new(), 'sources': a.dependency_receipts()['unknown_producer_sources'],
                          'gen': generated()}))
    '''))
    assert 'unrooted-exec:<attrs generated>' in data['new']
    assert data['sources'] == {}
    assert data['gen'] == []


def test_producer_diagnostics_are_bounded_and_require_exact_identity(tmp_path):
    data = helpers.run(tmp_path, r'''
        code = (lambda: None).__code__
        source = {'component': 'dependency:example-1.0', 'sha256': 'digest'}
        key = (code.co_filename, 'source', None)
        a._dep_units[key] = source
        a._known[id(code)] = ((lambda: 1).__code__, source['component'])
        a._unknown_producer_source('wrong-identity', code)
        assert a._unknown_sources == {}
        a._known[id(code)] = (code, 'different-component')
        a._unknown_producer_source('wrong-component', code)
        assert a._unknown_sources == {}
        a._known[id(code)] = (code, source['component'])
        del a._dep_units[key]
        a._unknown_producer_source('missing-source', code)
        assert a._unknown_sources == {}
        a._dep_units[key] = source
        ca.MAX_DEP_REPORT = 1
        a._unknown_producer_source('kept', code)
        a._unknown_producer_source('kept', code)
        assert len(a._unknown_sources['kept']) == 1
        a._unknown_producer_source('overflow', code)
        assert set(a._unknown_sources) == {'kept'}
        for n in range(12):
            item = code.replace(co_qualname='emitter_' + str(n))
            a._known[id(item)] = (item, source['component'])
            a._unknown_producer_source('kept', item)
        print(json.dumps({'count': len(a._unknown_sources['kept'])}))
    ''')
    assert data['count'] == 8
