"""Contracts for Hermes' shared YAML reader and writer."""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

import io
from concurrent.futures import ThreadPoolExecutor

import pytest

import hermes_yaml as yaml
from utils import fast_safe_load


@pytest.mark.parametrize("load", [yaml.safe_load, fast_safe_load])
def test_safe_load_accepts_existing_config_boolean_spellings(load):
    document = "flags: [on, off, yes, no, true, false]\nquoted: ['off', 'yes']\n"
    expected = {"flags": [True, False, True, False, True, False], "quoted": ["off", "yes"]}
    for stream in (document, document.encode(), io.StringIO(document), io.BytesIO(document.encode())):
        assert load(stream) == expected
    assert load("") is None


@pytest.mark.parametrize("load", [yaml.safe_load, fast_safe_load])
def test_safe_load_rejects_python_object_construction(load):
    with pytest.raises(yaml.YAMLError):
        load("!!python/object/apply:builtins.str ['must not construct']")


@pytest.mark.platforms("posix")
def test_safe_load_has_the_c_parser_where_its_wheel_ships():
    # Every YAML read (plugin manifests, config) goes through safe_load, and the pure parser
    # cost ~5 s of gateway startup on 1 vCPU. ruamel.yaml 0.18.16 only declares its C parser
    # below 3.14, so on 3.14 it exists only because pyproject.toml declares ruamel.yaml.clib.
    import platform
    import sys

    wheel_ships = sys.platform == "darwin" or (
        sys.platform == "linux" and platform.machine() in {"x86_64", "aarch64", "i686"})
    if platform.python_implementation() != "CPython" or not wheel_ships:
        pytest.skip("ruamel.yaml.clib ships no wheel for this interpreter or machine")
    import _ruamel_yaml  # the module safe_load's pure=False parser binds to


def test_safe_dump_preserves_data_and_readable_block_layout():
    data = {"z": [{"mode": "off", "choice": "y", "label": "こんにちは 🦀"}], "a": "yes"}
    text = yaml.safe_dump(data, sort_keys=False)
    assert yaml.safe_load(text) == data
    assert text.startswith("z:\n  - ")
    assert "こんにちは 🦀" in text
    stream = io.StringIO()
    assert yaml.safe_dump(data, stream, sort_keys=False) is None
    assert stream.getvalue() == text
    with pytest.raises(yaml.YAMLError):
        yaml.safe_dump({"object": object()})


def test_safe_dump_honors_the_options_used_by_callers():
    data = {"zebra": {"zed": 1, "alpha": 2}, "alpha": "hé"}
    for sort_keys in (False, True):
        loaded = yaml.safe_load(yaml.safe_dump(data, sort_keys=sort_keys))
        assert list(loaded) == (sorted(data) if sort_keys else list(data))
        assert list(loaded["zebra"]) == (sorted(data["zebra"]) if sort_keys else list(data["zebra"]))
    escaped = yaml.safe_dump(data, allow_unicode=False)
    assert "hé" not in escaped
    assert yaml.safe_load(escaped) == data
    flow = yaml.safe_dump(data, default_flow_style=True, width=100000)
    assert flow.startswith("{") and len(flow.splitlines()) == 1
    assert yaml.safe_load(flow) == data


def test_roundtrip_preserves_comments_quotes_and_scalar_types():
    editor = yaml.roundtrip_yaml()
    original = '# keep this\nname: "hello 🦀"  # note\nflag: off\n'
    data = editor.load(original)
    assert data["flag"] is False
    data["mode"] = "off"
    stream = io.StringIO()
    editor.dump(data, stream)
    text = stream.getvalue()
    assert text.startswith('# keep this\nname: "hello 🦀"  # note\n')
    assert yaml.safe_load(text) == {"name": "hello 🦀", "flag": False, "mode": "off"}


def test_native_yaml11_scalars_and_duplicate_key_policy():
    for load in (yaml.safe_load, fast_safe_load, yaml.roundtrip_yaml().load):
        assert load("[y, n, Y, N, 'y', 'n']") == [True, False, True, False, "y", "n"]
        with pytest.raises(yaml.YAMLError):
            load("model: first\nmodel: second\n")
    # Merge keys override defaults, not duplicates in the mapping itself.
    merged = yaml.safe_load("defaults: &defaults {enabled: true}\nlocal: {<<: *defaults, enabled: false}\n")
    assert merged["local"]["enabled"] is False


def test_parallel_calls_do_not_share_parser_or_emitter_state():
    def roundtrip(index):
        data = {"index": index, "words": ["yes", "no", "on", "off", "y", "n"]}
        text = yaml.safe_dump(data, sort_keys=bool(index % 2))
        assert yaml.safe_load(text) == data
        assert yaml.roundtrip_yaml().load(text) == data
        return data

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert [data["index"] for data in pool.map(roundtrip, range(32))] == list(range(32))
    # A failed parse/dump must not poison the next operation.
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load("key: [unterminated")
    with pytest.raises(yaml.YAMLError):
        yaml.safe_dump(object())
    assert yaml.safe_load(yaml.safe_dump({"healthy": True})) == {"healthy": True}


class TestParsedDocumentDiskCache:
    """Large documents are cached on disk by content; semantics must not change."""

    @staticmethod
    def _big(extra: str = "") -> str:
        return "# pad\n" * 4000 + "flag: yes\nname: x\n" + extra

    def test_second_load_skips_the_parser_and_matches(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yaml, "_cache_dir", lambda: tmp_path)
        doc = self._big()
        first = yaml.safe_load(doc)
        assert first == {"flag": True, "name": "x"}  # YAML 1.1 boolean
        assert len(list(tmp_path.glob("*.bin"))) == 1

        def boom(*_a, **_k):
            raise AssertionError("parser ran on a cache hit")

        monkeypatch.setattr(yaml, "_load", boom)
        second = yaml.safe_load(doc)
        assert second == first and second is not first
        second["name"] = "mutated"
        assert yaml.safe_load(doc)["name"] == "x"

    def test_changed_content_misses(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yaml, "_cache_dir", lambda: tmp_path)
        assert yaml.safe_load(self._big())["name"] == "x"
        assert yaml.safe_load(self._big("other: 1\n"))["other"] == 1

    def test_duplicate_keys_still_rejected_and_not_cached(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yaml, "_cache_dir", lambda: tmp_path)
        doc = self._big("name: y\n")
        for _ in range(2):
            with pytest.raises(Exception):
                yaml.safe_load(doc)
        assert not list(tmp_path.glob("*.bin"))

    def test_non_marshalable_values_are_not_cached(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yaml, "_cache_dir", lambda: tmp_path)
        out = yaml.safe_load(self._big("when: 2026-10-29\n"))
        assert str(out["when"]) == "2026-10-29"
        assert not list(tmp_path.glob("*.bin"))

    def test_corrupt_entry_falls_back_to_parse(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yaml, "_cache_dir", lambda: tmp_path)
        doc = self._big()
        yaml.safe_load(doc)
        for f in tmp_path.glob("*.bin"):
            f.write_bytes(b"\x00garbage")
        assert yaml.safe_load(doc)["name"] == "x"

    def test_small_documents_bypass_the_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(yaml, "_cache_dir", lambda: tmp_path)
        assert yaml.safe_load("a: 1\n") == {"a": 1}
        assert not list(tmp_path.glob("*.bin"))
