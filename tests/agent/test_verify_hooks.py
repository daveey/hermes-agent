"""Unit tests for the verification-loop policy (agent/verify_hooks.py).

The `pre_verify` user-hook aggregation lives in `hermes_cli.plugins`
(`get_pre_verify_continue_message`) and is tested in
`tests/hermes_cli/test_plugins.py`, alongside `get_pre_tool_call_block_message`.
"""

from __future__ import annotations

from agent import verify_hooks


class TestMaxVerifyNudges:
    def test_default_when_unset(self):
        assert (
            verify_hooks.max_verify_nudges({})
            == verify_hooks.DEFAULT_MAX_VERIFY_NUDGES
        )
        assert (
            verify_hooks.max_verify_nudges({"agent": {}})
            == verify_hooks.DEFAULT_MAX_VERIFY_NUDGES
        )

    def test_reads_and_coerces(self):
        assert verify_hooks.max_verify_nudges({"agent": {"max_verify_nudges": 5}}) == 5
        assert verify_hooks.max_verify_nudges({"agent": {"max_verify_nudges": "2"}}) == 2
        assert verify_hooks.max_verify_nudges({"agent": {"max_verify_nudges": -1}}) == 0

    def test_bad_value_falls_back(self):
        assert (
            verify_hooks.max_verify_nudges({"agent": {"max_verify_nudges": "x"}})
            == verify_hooks.DEFAULT_MAX_VERIFY_NUDGES
        )


class TestCodingVerifyGuidance:
    def test_enabled_by_default(self):
        assert (
            verify_hooks.coding_verify_guidance({})
            == verify_hooks.CODING_VERIFY_GUIDANCE
        )
        assert (
            verify_hooks.coding_verify_guidance({"agent": {}})
            == verify_hooks.CODING_VERIFY_GUIDANCE
        )

    def test_reads_truthy_config(self):
        cfg = {"agent": {"verify_guidance": "yes"}}
        assert verify_hooks.coding_verify_guidance(cfg) == verify_hooks.CODING_VERIFY_GUIDANCE

    def test_opt_out_via_config(self):
        off = {"agent": {"verify_guidance": False}}
        assert verify_hooks.coding_verify_guidance(off) is None


class TestPreVerifyEveryTurn:
    def test_off_by_default(self):
        assert verify_hooks.pre_verify_every_turn({}) is False
        assert verify_hooks.pre_verify_every_turn({"agent": {}}) is False

    def test_opt_in(self):
        assert verify_hooks.pre_verify_every_turn({"agent": {"pre_verify_every_turn": True}}) is True
        assert verify_hooks.pre_verify_every_turn({"agent": {"pre_verify_every_turn": "yes"}}) is True


class TestPreVerifyGateWithoutEdits:
    """turn_stop_gates._pre_verify_nudge: no edits -> hook only consulted when opted in."""

    def _run(self, monkeypatch, every_turn, edited):
        from types import SimpleNamespace
        from agent import turn_stop_gates
        from hermes_cli import lifecycle
        from hermes_cli import plugins

        calls = []
        monkeypatch.setattr(lifecycle, "has_hook", lambda name: name == "pre_verify")
        monkeypatch.setattr(verify_hooks, "pre_verify_every_turn", lambda config=None: every_turn)
        monkeypatch.setattr(verify_hooks, "max_verify_nudges", lambda config=None: 3)

        def fake(**kw):
            calls.append(kw)
            return "keep going"

        monkeypatch.setattr(plugins, "get_pre_verify_continue_message", fake)
        agent = SimpleNamespace(_turn_file_mutation_paths=set(edited), session_id="s", platform="cli",
                                model="m", _resolved_is_coding=False)
        return turn_stop_gates._pre_verify_nudge(agent, "Done.", 0), calls

    def test_no_edits_default_skips_hook(self, monkeypatch):
        msg, calls = self._run(monkeypatch, every_turn=False, edited=[])
        assert msg is None and calls == []

    def test_no_edits_opted_in_calls_hook_with_empty_paths(self, monkeypatch):
        msg, calls = self._run(monkeypatch, every_turn=True, edited=[])
        assert msg == "keep going" and calls[0]["changed_paths"] == []

    def test_edits_still_call_hook_when_off(self, monkeypatch):
        msg, calls = self._run(monkeypatch, every_turn=False, edited=["a.py"])
        assert msg == "keep going" and calls[0]["changed_paths"] == ["a.py"]
