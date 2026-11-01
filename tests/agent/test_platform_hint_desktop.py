"""System-prompt assembly for the desktop chat surface.

Pins the second half of the fix: the new ``PLATFORM_HINTS["desktop"]``
entry, the deletion of the standalone desktop-hint block from
``build_environment_hints()``, and the lookup-site extension that
appends the embedded-terminal-pane clarifier to the ``tui`` platform hint
when ``HERMES_DESKTOP_TERMINAL=1``.

These tests run against the real prompt builders (no mocks) because
cache-stability and byte-for-byte text contracts are what we are
verifying — mocking the resolver would hide exactly the class of bug
this test covers.
"""

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.prompt_builder import PLATFORM_HINTS
from agent.system_prompt import (
    _tui_embedded_pane_clarifier,
    build_system_prompt_parts,
)

def _stable_prompt(agent):
    with (
        patch("agent.prompt_builder.load_soul_md", return_value=""),
        patch("agent.prompt_builder.build_environment_hints", return_value=""),
        patch("agent.prompt_builder.build_context_files_prompt", return_value=""),
    ):
        return build_system_prompt_parts(agent)["stable"]

def _make_agent(platform="", **overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        _platform_hint_overrides={},
        model="",
        provider="",
        pass_session_id=False,
        session_id="",
    )
    base["platform"] = platform
    base.update(overrides)
    return SimpleNamespace(**base)

class TestPlatformHintResolutionInStablePrompt:
    """End-to-end through ``build_system_prompt_parts`` — the platform tag on
    the agent drives BOTH which PLATFORM_HINTS entry gets appended AND
    whether the embedded-pane clarifier follows it. The desktop-hint block
    that used to live in ``build_environment_hints()`` is gone."""

    def test_desktop_platform_yields_desktop_hint_no_tui_framing(self, monkeypatch):
        monkeypatch.setenv("HERMES_DESKTOP", "1")
        monkeypatch.delenv("HERMES_DESKTOP_TERMINAL", raising=False)
        stable = _stable_prompt(_make_agent(platform="desktop"))
        assert PLATFORM_HINTS["desktop"] in stable
        assert "terminal UI" not in stable
        assert "Runtime surface:" not in stable
        assert "embedded terminal pane" not in stable

    def test_embedded_tui_yields_tui_hint_with_clarifier(self, monkeypatch):
        monkeypatch.setenv("HERMES_DESKTOP", "1")
        monkeypatch.setenv("HERMES_DESKTOP_TERMINAL", "1")
        stable = _stable_prompt(_make_agent(platform="tui"))
        assert PLATFORM_HINTS["tui"] in stable
        assert "embedded terminal pane" in stable
        assert "Shift-drag" in stable or "Option-drag" in stable or "⌥" in stable

class TestEmbeddedTuiPaneClarifier:
    """When ``HERMES_DESKTOP_TERMINAL=1``, a standalone ``hermes --tui`` is
    running inside the desktop's embedded terminal pane. The user can
    ⌥-drag-select its output and ⌘/Ctrl+L to send it to the chat composer.
    That clarifier must be appended to the ``tui`` platform hint at the
    resolution site, NOT baked into the static ``PLATFORM_HINTS["tui"]``
    string (which is shared with every standalone TUI session and must
    stay byte-stable)."""

    @pytest.mark.parametrize("val", ["0", "false", ""])
    def test_falsy_env_does_not_trigger_clarifier(self, monkeypatch, val):
        monkeypatch.setenv("HERMES_DESKTOP_TERMINAL", val)
        out = _tui_embedded_pane_clarifier(PLATFORM_HINTS["tui"])
        assert out == PLATFORM_HINTS["tui"], (
            f"HERMES_DESKTOP_TERMINAL={val!r} should not trigger clarifier"
        )

_INLINE_PREVIEW_TSX = (
    Path(__file__).resolve().parents[2]
    / "apps/desktop/src/components/assistant-ui/inline-preview-directive.tsx"
)

def _renderer_const(name):
    m = re.search(rf"export const {name} = (\d+)", _INLINE_PREVIEW_TSX.read_text())
    assert m, f"{name} not found in {_INLINE_PREVIEW_TSX}"
    return int(m.group(1))

class TestDesktopWidgetSendRules:
    """Inline widgets lost a whole rating session (desktop-13785ebc41): the agent
    sent every rating in one ``hermes.send`` (over the cap, refused), ignored the
    refusal, and promised localStorage saving the sandboxed frame blocks. The
    hint is the only place agents learn the widget contract, so it must state
    the real limits, read from the renderer that enforces them."""

    def test_hint_states_the_real_length_cap(self):
        assert f"{_renderer_const('MAX_INTENT_LENGTH')} characters" in PLATFORM_HINTS["desktop"]

    def test_hint_states_the_real_throttle(self):
        # The hint says "one per second"; a changed throttle must change it too.
        assert _renderer_const("INTENT_THROTTLE_MS") == 1000
        assert "one per second" in PLATFORM_HINTS["desktop"]

    def test_hint_says_to_check_the_ack_retry_and_chunk(self):
        hint = PLATFORM_HINTS["desktop"]
        assert "{ok:false" in hint and "retryAfterMs" in hint
        assert "chunk" in hint

    def test_hint_says_browser_storage_is_unavailable(self):
        hint = PLATFORM_HINTS["desktop"]
        assert "localStorage" in hint and "unavailable" in hint
