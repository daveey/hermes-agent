"""A local-backend miss names the real files beside the missing one.

Regression: an agent that lost a figure list to compaction asked vision_analyze for four
invented ``figure-page-N-0.png`` names; each miss said only "media file not found", so it
kept guessing until the loop guard fired.
"""

import importlib
from pathlib import Path

import pytest


def _reload(monkeypatch, hermes_home: Path):
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    import hermes_constants
    importlib.reload(hermes_constants)
    import tools.image_source as isrc
    importlib.reload(isrc)
    return isrc


@pytest.mark.asyncio
async def test_miss_lists_matching_siblings(tmp_path, monkeypatch):
    isrc = _reload(monkeypatch, tmp_path / "hermes")
    figs = tmp_path / "figs"
    figs.mkdir()
    real = [f"figure-page-{n}-0.png" for n in (3, 12, 75, 88)]
    for name in real + ["notes.txt"]:
        (figs / name).write_bytes(b"x")
    with pytest.raises(isrc.SourceNotFound) as excinfo:
        await isrc.resolve_image_source(str(figs / "figure-page-79-0.png"), isrc.ResolveContext())
    msg = str(excinfo.value)
    assert msg.startswith(f"media file not found: '{figs / 'figure-page-79-0.png'}'")
    assert "files in that dir:" in msg
    for name in real:
        assert name in msg
    assert "notes.txt" not in msg


@pytest.mark.asyncio
async def test_miss_caps_sibling_list_at_ten(tmp_path, monkeypatch):
    isrc = _reload(monkeypatch, tmp_path / "hermes")
    figs = tmp_path / "figs"
    figs.mkdir()
    for n in range(30):
        (figs / f"figure-page-{n}-0.png").write_bytes(b"x")
    with pytest.raises(isrc.SourceNotFound) as excinfo:
        await isrc.resolve_image_source(str(figs / "figure-page-99-0.png"), isrc.ResolveContext())
    listed = str(excinfo.value).split("files in that dir:", 1)[1]
    assert listed.count("figure-page-") == 10
    assert "(+20 more)" in listed


@pytest.mark.asyncio
async def test_miss_in_missing_dir_keeps_plain_message(tmp_path, monkeypatch):
    isrc = _reload(monkeypatch, tmp_path / "hermes")
    target = tmp_path / "nope" / "figure-page-1-0.png"
    with pytest.raises(isrc.SourceNotFound) as excinfo:
        await isrc.resolve_image_source(str(target), isrc.ResolveContext())
    assert str(excinfo.value) == f"media file not found: '{target}'"
