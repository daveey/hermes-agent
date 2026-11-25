"""Installer ambiguity and overlapping-generation custody via actual audit events."""
import pytest

from tests.hermes_cli.test_code_attestation_attrs import run


def test_ambiguous_index_cannot_supply_identity_or_verified_bytes(tmp_path):
    import base64
    import hashlib
    from hermes_cli.code_attestation import _DependencyIndex

    site = tmp_path / 'site-packages'
    site.mkdir()
    source = site / 'source.py'
    data = b'value = 1\n'
    source.write_bytes(data)
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
    for name in ('aaa-1', 'zzz-1'):
        info = site / (name + '.dist-info')
        info.mkdir()
        (info / 'RECORD').write_text(f'source.py,sha256={digest},{len(data)}\n')
    index = _DependencyIndex(tmp_path, [site])
    index.build()
    assert index.identity(source) is None
    assert index.verify(source, data) is None
    index.build()  # Rebuilding must not erase latched ambiguity.
    assert index.identity(source) is None
    assert index.verify(source, data) is None


def test_same_version_receipts_keep_each_used_root(tmp_path):
    import base64
    import hashlib
    from hermes_cli.code_attestation import _DependencyIndex

    index = _DependencyIndex(tmp_path, [])
    expected = {}
    for generation in ('old', 'new'):
        site = tmp_path / generation / 'site-packages'
        info = site / 'pkg-1.dist-info'
        info.mkdir(parents=True)
        data = generation.encode()
        (site / 'source.py').write_bytes(data)
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
        record = f'source.py,sha256={digest},{len(data)}\n'.encode()
        (info / 'RECORD').write_bytes(record)
        (info / 'INSTALLER').write_text(generation)
        index.add_root(site)
        assert index.verify(site / 'source.py') == ('pkg-1', hashlib.sha256(data).hexdigest())
        expected[str(site / 'pkg-1')] = hashlib.sha256(record).hexdigest()
    receipts = index.receipts()
    assert {name: row['record_sha256'] for name, row in receipts.items()} == expected
    for name, row in receipts.items():
        assert name.startswith(row['dir'] + '/')
        assert row['installer_sha256'] == hashlib.sha256(
            ('old' if '/old/' in name else 'new').encode()).hexdigest()



@pytest.mark.parametrize('claim', ['aaa_conflict', 'zzz_conflict'])
@pytest.mark.parametrize('matching', [False, True])
def test_attrs_rejects_ambiguous_installer_claims(tmp_path, claim, matching):
    result = run(tmp_path, f'''
        other = SITE / {claim + '-1.0.dist-info'!r}
        other.mkdir()
        rows = (info / 'RECORD').read_text().splitlines()
        row = next(r for r in rows if r.startswith('attr/_make.py,'))
        if not {matching!r}:
            row = 'attr/_make.py,sha256=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA,1'
        (other / 'RECORD').write_text(row + '\\n')
        activate()
        import attr
        report()
    ''')
    assert result['unknowns']
    assert result['generated'] == []


@pytest.mark.parametrize('delete_record', [False, True])
@pytest.mark.parametrize('register_new_first', [False, True])
def test_attrs_installer_custody_survives_real_generation_adoption(
        tmp_path, delete_record, register_new_first):
    result = run(tmp_path, f'''
        from pm import environments_adopt as adoption
        from pm.environments import site_packages
        from hermes_cli import runtime_state
        old_env = pathlib.Path('old/venv').resolve()
        new_env = pathlib.Path('new/venv').resolve()
        old_site, new_site = site_packages(old_env), site_packages(new_env)
        shutil.copytree(SITE, old_site)
        shutil.copytree(SITE, new_site)
        new_record = new_site / info.name / 'RECORD'
        new_record.write_text('\\n'.join(reversed(new_record.read_text().splitlines())) + '\\n')
        if {register_new_first!r}:
            a.register_dependency_environment(new_site)
            a._dep_ready()
        a.register_dependency_environment(old_site)
        sys.path.insert(0, str(old_site))
        import attr._make as make
        ca._installed = a
        runtime_state.lease_generation = lambda selected: None
        assert adoption.adopt(old_env, new_env, old_env)
        a._dep_ready()
        source = pathlib.Path(make.__file__).read_bytes()
        original_record = old_site / info.name / 'RECORD'
        original_digest = hashlib.sha256(original_record.read_bytes()).hexdigest()
        if {delete_record!r}:
            original_record.unlink()
        namespace = dict(make.__dict__)
        exec(compile(source, make.__file__, 'exec'), namespace)
        before = a.dependency_receipts()['units']
        class GenerationProbe:
            pass
        namespace['_add_repr'](GenerationProbe, attrs=[])
        assert repr(GenerationProbe()) == 'GenerationProbe()'
        generated = [u for u in a.dependency_receipts()['units']
                     if u not in before and u.get('generator') == 'attrs:_compile_and_eval']
        print(json.dumps({{'generated': generated, 'original_digest': original_digest,
                          'unknowns': sorted(set(a.unknowns()) - BASE)}}))
    ''')
    if delete_record:
        assert result['unknowns']
        assert result['generated'] == []
    else:
        assert result['unknowns'] == []
        assert result['generated']
        assert all(u['producer_distribution']['record_sha256'] == result['original_digest']
                   for u in result['generated'])
