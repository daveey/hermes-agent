"""The bundled platform manifest scan parses each ``plugin.yaml`` once per file signature. [daveey]

David-MBP profile (t_01a199a0a5d38255): every Desktop ``/api/status`` poll ran ``load_gateway_config`` ->
``_bundled_env_fallbacks`` -> ``Platform._scan_bundled_plugin_platforms``, which YAML-parsed all ~27 bundled platform
manifests again (~10 ms of a ~12 ms config load on the Mac). The manifest ``name:`` is now memoized on the file's
(device, inode, size, mtime_ns), so an edited or replaced manifest is still read afresh.
"""

from __future__ import annotations

import os

import gateway.config as gc


def _spy(monkeypatch):
    calls = []
    real = gc.fast_safe_load

    def spy(text):
        calls.append(text)
        return real(text)

    monkeypatch.setattr(gc, "fast_safe_load", spy)
    return calls


def test_unchanged_manifest_is_parsed_once(tmp_path, monkeypatch):
    plugin = tmp_path / "irc"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text("name: irc-platform\n", encoding="utf-8")
    calls = _spy(monkeypatch)
    assert [gc._bundled_platform_manifest_name(plugin) for _ in range(5)] == ["irc-platform"] * 5
    assert len(calls) == 1


def test_edited_manifest_is_read_again(tmp_path, monkeypatch):
    plugin = tmp_path / "irc"
    plugin.mkdir()
    manifest = plugin / "plugin.yaml"
    manifest.write_text("name: irc-platform\n", encoding="utf-8")
    assert gc._bundled_platform_manifest_name(plugin) == "irc-platform"
    st = manifest.stat()
    manifest.write_text("name: irc-renamed\n", encoding="utf-8")
    os.utime(manifest, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert gc._bundled_platform_manifest_name(plugin) == "irc-renamed"
    manifest.unlink()
    (plugin / "plugin.yml").write_text("name: irc-yml\n", encoding="utf-8")
    assert gc._bundled_platform_manifest_name(plugin) == "irc-yml"


def test_repeated_scans_parse_no_manifest(monkeypatch):
    first = gc.Platform._scan_bundled_plugin_platforms()
    calls = _spy(monkeypatch)
    for _ in range(3):
        assert gc.Platform._scan_bundled_plugin_platforms() == first
    assert calls == []
