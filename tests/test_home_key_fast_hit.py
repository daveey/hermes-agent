"""A warm home key need not allocate/reparse another Path."""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard
import hermes_constants as hc


def test_warm_key_uses_input_string_without_reconstructing_path(monkeypatch, tmp_path):
    hc.reset_hermes_home_key_cache()
    key = hc.hermes_home_key(tmp_path)
    def unexpected(*args):
        raise AssertionError('a warm key reconstructed a Path')
    monkeypatch.setattr(hc, 'Path', unexpected)
    assert hc.hermes_home_key(tmp_path) == key
    assert hc.hermes_home_key(str(tmp_path)) == key
