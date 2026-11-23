"""Per-thread parser reuse retains YAML policy, isolation and failure recovery."""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

import hermes_yaml as yaml


@pytest.mark.parametrize('pure', [False, True])
def test_reuses_parser_but_resets_document_version(monkeypatch, pure):
    factory = yaml.YAML
    instances = []
    def create(**kwargs):
        parser = factory(**kwargs)
        instances.append(parser)
        return parser
    monkeypatch.setattr(yaml, 'YAML', create)
    # Fresh thread so pre-existing parsers cannot mask construction counts.
    def load():
        assert yaml._load('%YAML 1.2\n---\nflag: off', pure=pure) == {'flag': 'off' if pure else False}
        for _ in range(10):
            assert yaml._load('flag: off', pure=pure) == {'flag': False}
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(load).result()
    assert len(instances) == 1
    assert len(instances[0].doc_infos) <= 2


@pytest.mark.parametrize('pure', [False, True])
def test_bad_parse_discards_parser(monkeypatch, pure):
    factory = yaml.YAML
    instances = []
    def create(**kwargs):
        parser = factory(**kwargs)
        instances.append(parser)
        return parser
    monkeypatch.setattr(yaml, 'YAML', create)
    def load():
        with pytest.raises(yaml.YAMLError):
            yaml._load('a: [unterminated', pure=pure)
        assert yaml._load('a: yes', pure=pure) == {'a': True}
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(load).result()
    assert len(instances) == 2


def test_separate_parser_per_thread_and_pure_mode(monkeypatch):
    factory = yaml.YAML
    instances = []
    barrier = threading.Barrier(4)
    def create(**kwargs):
        parser = factory(**kwargs)
        instances.append((threading.get_ident(), kwargs['pure'], parser))
        return parser
    monkeypatch.setattr(yaml, 'YAML', create)
    def load(_):
        barrier.wait(3)
        for _ in range(3):
            for pure in (False, True):
                assert yaml._load('a: yes', pure=pure) == {'a': True}
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(load, range(4)))
    assert len(instances) == 8
    assert len({(thread, pure) for thread, pure, parser in instances}) == 8


@pytest.mark.parametrize('pure', [False, True])
def test_aliases_and_tag_directives_do_not_leak_between_documents(pure):
    assert yaml._load('a: &x yes\nb: *x', pure=pure) == {'a': True, 'b': True}
    with pytest.raises(yaml.YAMLError):
        yaml._load('a: *x', pure=pure)
    assert yaml._load('%TAG !e! tag:yaml.org,2002:\n---\na: !e!str yes', pure=pure) == {'a': 'yes'}
    with pytest.raises(yaml.YAMLError):
        yaml._load('a: !e!str yes', pure=pure)
    assert yaml._load('a: yes', pure=pure) == {'a': True}


def test_reentrant_load_never_shares_active_instance(monkeypatch):
    factory = yaml.YAML
    instances = []
    def create(**kwargs):
        parser = factory(**kwargs)
        instances.append(parser)
        if len(instances) == 1:
            original = parser.load
            def nested(document):
                assert yaml._load('nested: yes', pure=True) == {'nested': True}
                return original(document)
            parser.load = nested
        return parser
    monkeypatch.setattr(yaml, 'YAML', create)
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(yaml._load, 'outer: no', pure=True).result() == {'outer': False}
    assert len(instances) == 2
