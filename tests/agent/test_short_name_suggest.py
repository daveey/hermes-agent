"""suggest_short_name: the ⌘⇧N rename pre-fill."""
from types import SimpleNamespace

from agent.title_generator import build_short_name_input, suggest_short_name


def _resp(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_excerpt_keeps_user_and_assistant_text_only():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "add a shortcut"},
        {"role": "tool", "content": "noise"},
        {"role": "assistant", "content": "done"},
    ]
    assert build_short_name_input(msgs) == "user: add a shortcut\nassistant: done"


def test_excerpt_keeps_opening_and_latest_turns():
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(20)]
    out = build_short_name_input(msgs).splitlines()
    assert out[:4] == ["user: m0", "user: m1", "user: m2", "user: m3"]
    assert out[4] == "…"
    assert out[-1] == "user: m19"


def test_suggest_lowercases_and_parses_json():
    calls = []

    def llm(**kw):
        calls.append(kw)
        return _resp('{"title": "Hermes Shortcuts"}')

    assert suggest_short_name([{"role": "user", "content": "keybinds"}], llm=llm) == "hermes shortcuts"
    assert calls[0]["task"] == "title_generation"


def test_suggest_fails_open(caplog):
    def boom(**kw):
        raise RuntimeError("no provider")

    with caplog.at_level("WARNING", logger="agent.title_generator"):
        assert suggest_short_name([{"role": "user", "content": "x"}], llm=boom) is None
    assert "Short-name suggestion failed: RuntimeError: no provider" in caplog.text
    assert suggest_short_name([], llm=boom) is None
