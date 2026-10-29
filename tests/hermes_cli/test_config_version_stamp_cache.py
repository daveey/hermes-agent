"""``read_config_version_stamp`` re-parses config.yaml only when the file changes.

``/api/status`` reads the stamp on every Desktop poll. A full YAML parse of a ~2500-line config there
held the GIL ~40 ms per call and stretched every request queued behind it in a wake burst. [daveey]
"""

import os

import pytest

from hermes_cli import config_version_stamp as cvs


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    getattr(cvs, "_stamp_cache", {}).clear()
    yield home
    getattr(cvs, "_stamp_cache", {}).clear()


@pytest.fixture
def parses(monkeypatch):
    from hermes_cli import config

    real = config.fast_safe_load
    count = [0]

    def counting(stream):
        count[0] += 1
        return real(stream)

    monkeypatch.setattr(config, "fast_safe_load", counting)
    return count


def _write(home, text):
    path = home / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_an_unchanged_config_is_parsed_once(hermes_home, parses):
    _write(hermes_home, "_config_version: 7\nmodel:\n  default: x\n")
    assert cvs.check_config_version()[0] == 7
    assert cvs.check_config_version()[0] == 7
    assert cvs.read_config_version_stamp()[0] == 7
    assert parses[0] == 1


def test_a_rewrite_is_seen_on_the_next_read(hermes_home, parses):
    path = _write(hermes_home, "_config_version: 7\n")
    assert cvs.check_config_version()[0] == 7
    path.write_text("_config_version: 9\n", encoding="utf-8")  # same size
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert cvs.check_config_version()[0] == 9
    _write(hermes_home, "model: {}\n")  # stamp removed
    assert cvs.read_config_version_stamp()[0] is None
    assert parses[0] == 3


def test_parse_errors_are_not_cached_and_still_raise_for_strict_callers(hermes_home, parses):
    from hermes_cli.config import InvalidUserConfigError

    _write(hermes_home, "_config_version: [unclosed\n")
    _, latest = cvs.check_config_version()
    assert cvs.check_config_version() == (latest, latest)  # tolerant: latest/latest, as before
    with pytest.raises(InvalidUserConfigError):
        cvs.check_config_version(raise_on_parse_error=True)
    assert parses[0] >= 3  # every call re-read the broken file


def test_a_non_mapping_root_stays_unusable_from_the_cache(hermes_home, parses):
    from hermes_cli.config import InvalidUserConfigError

    _write(hermes_home, "- a\n- b\n")
    assert cvs.read_config_version_stamp()[0] is None
    with pytest.raises(InvalidUserConfigError, match="must be a mapping, got list"):
        cvs.check_config_version(raise_on_parse_error=True)
    assert parses[0] == 1
