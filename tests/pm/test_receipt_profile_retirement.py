"""Receipt publication never recreates a named home removed between writes."""

import pytest

from pm import receipt


@pytest.mark.platforms("posix")  # Windows cannot rename a directory holding this open lock file.
@pytest.mark.parametrize("publication", [1, 2])
def test_receipt_profile_rename_during_publication(tmp_path, monkeypatch, publication):
    from pm import filesystem

    home = tmp_path / "profiles" / "gamma"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("{}")
    monkeypatch.setenv("HERMES_HOME", str(home))
    original = filesystem.durable_write_bytes
    writes = []

    def write(path, data, **kwargs):
        writes.append(path)
        if len(writes) == publication:
            home.rename(home.with_name("delta"))
        return original(path, data, **kwargs)

    monkeypatch.setattr(filesystem, "durable_write_bytes", write)
    token = receipt.begin("plugin-check")
    assert receipt.finalize("ok", token=token) is None
    assert len(writes) == publication
    assert not home.exists()
    assert receipt.snapshot() is None
    assert (home.with_name("delta") / "config.yaml").is_file()
