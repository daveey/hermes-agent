"""A profile disappearing after the liveness check must not be recreated by mkdir."""

import pytest

from hermes_constants import mkdir_under_hermes_home


@pytest.mark.parametrize("depth", [1, 2])
def test_named_home_rename_during_child_mkdir(tmp_path, monkeypatch, depth):
    import hermes_dir_acl

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    home = tmp_path / "profiles" / "gamma"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("{}")
    target = home / "logs" / "update_receipts"
    original = hermes_dir_acl.make_user_dir
    calls = []

    def mkdir(path, **kwargs):
        calls.append(path)
        if len(calls) == depth:
            home.rename(home.with_name("delta"))
        return original(path, **kwargs)

    monkeypatch.setattr(hermes_dir_acl, "make_user_dir", mkdir)
    with pytest.raises(FileNotFoundError):
        mkdir_under_hermes_home(target)
    assert not home.exists()
    assert (home.with_name("delta") / "config.yaml").is_file()
