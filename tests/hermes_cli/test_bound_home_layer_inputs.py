import pytest
import providers
import hermes_constants as hc


@pytest.mark.parametrize('bound', [False, True])
def test_expanded_home_input_changes(tmp_path, monkeypatch, bound):
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir(); b.mkdir()
    monkeypatch.setenv('REVIEW_HOME_ROOT', str(a))
    monkeypatch.setenv('HERMES_HOME', '$REVIEW_HOME_ROOT')
    token = hc.set_hermes_home_override('$REVIEW_HOME_ROOT' if bound else None)
    try:
        layer_a, home_a, _ = providers._bound_home_layer()
        assert home_a == a
        monkeypatch.setenv('REVIEW_HOME_ROOT', str(b))
        assert hc.get_hermes_home() == b
        layer_b, home_b, key_b = providers._bound_home_layer()
        assert home_b == b
        assert key_b == hc.hermes_home_key(b)
        assert layer_b is not layer_a
    finally:
        hc.reset_hermes_home_override(token)


def test_reset_repopulated_home_key(tmp_path, monkeypatch):
    a, b, link = tmp_path / 'a', tmp_path / 'b', tmp_path / 'link'
    a.mkdir(); b.mkdir(); link.symlink_to(a, target_is_directory=True)
    monkeypatch.setenv('HERMES_HOME', str(link))
    token = hc.set_hermes_home_override(None)
    try:
        layer_a, _, key_a = providers._bound_home_layer()
        link.unlink(); link.symlink_to(b, target_is_directory=True)
        hc.reset_hermes_home_key_cache()
        key_b = hc.hermes_home_key(link)
        assert key_a != key_b
        layer_b, _, observed = providers._bound_home_layer()
        assert observed == key_b
        assert layer_b is not layer_a
    finally:
        hc.reset_hermes_home_override(token)


def test_literal_bound_home_change(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir(); b.mkdir()
    token = hc.set_hermes_home_override(a)
    try:
        layer_a, _, _ = providers._bound_home_layer()
        inner = hc.set_hermes_home_override(b)
        try:
            layer_b, home_b, _ = providers._bound_home_layer()
            assert home_b == b and layer_b is not layer_a
        finally:
            hc.reset_hermes_home_override(inner)
        assert providers._bound_home_layer()[0] is layer_a
    finally:
        hc.reset_hermes_home_override(token)
