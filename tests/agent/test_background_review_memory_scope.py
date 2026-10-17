"""The review fork's memory access must follow the trigger that fired (#105921).

``spawn_background_review_thread`` already received ``review_memory`` /
``review_skills`` but used them only to pick the prompt — the tool whitelist
granted the whole ``memory`` toolset whenever the profile had memory enabled,
so a skill-nudge fork held ``remove``/``replace`` on MEMORY.md it was never
asked to use. These tests pin the scope-aware whitelist and the pass-through
from ``spawn_background_review_thread`` down to it.
"""

from __future__ import annotations

import os
import sys
import pytest
from types import SimpleNamespace
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import agent.background_review as bg


def _review_agent(memory_enabled=True, user_profile_enabled=False) -> SimpleNamespace:
    """The whitelist only reads the profile's memory flags off the fork."""
    return SimpleNamespace(_memory_enabled=memory_enabled, _user_profile_enabled=user_profile_enabled)


class TestReviewToolWhitelistScope:
    def test_skill_only_review_omits_memory_tool(self):
        whitelist, _extra = bg._review_tool_whitelist(_review_agent(), None, review_memory=False)
        assert "memory" not in whitelist
        assert "skill_manage" in whitelist  # the skill review keeps its own surface

    def test_memory_review_keeps_memory_tool(self):
        whitelist, _extra = bg._review_tool_whitelist(_review_agent(), None, review_memory=True)
        assert "memory" in whitelist

    def test_memory_disabled_profile_stays_memory_free(self):
        whitelist, _extra = bg._review_tool_whitelist(
            _review_agent(memory_enabled=False, user_profile_enabled=False), None, review_memory=True)
        assert "memory" not in whitelist

    def test_default_scope_is_memoryless_fail_closed(self):
        # Unknown trigger (default) must not grant memory: the incident fork was a
        # skill review that used memory nobody asked it to touch.
        whitelist, _extra = bg._review_tool_whitelist(_review_agent(), None)
        assert "memory" not in whitelist


class TestSpawnForwardsScope:
    def test_target_passes_review_memory_to_worker(self):
        captured = {}

        def fake_worker(agent, messages_snapshot, prompt, task_cfg=None, review_run=None,
                        review_memory=False, explicit=False):
            captured["review_memory"] = review_memory

        agent = SimpleNamespace()
        with patch.object(bg, "_run_review_in_thread", fake_worker):
            target, _prompt = bg.spawn_background_review_thread(
                agent, [], review_memory=False, review_skills=True)
            target()
            assert captured["review_memory"] is False

            target, _prompt = bg.spawn_background_review_thread(
                agent, [], review_memory=True, review_skills=False)
            target()
            assert captured["review_memory"] is True


class TestExplicitRefineOrigin:
    """``/refine`` (explicit) must not inherit the unattended-review origin: the user asked
    for that review, so its fork keeps the full memory operation set and the delete gate
    does not apply (#105921 review follow-up)."""

    def test_target_passes_explicit_to_worker(self):
        captured = {}

        def fake_worker(agent, messages_snapshot, prompt, task_cfg=None, review_run=None,
                        review_memory=False, explicit=False):
            captured["explicit"] = explicit

        with patch.object(bg, "_run_review_in_thread", fake_worker):
            target, _prompt = bg.spawn_background_review_thread(
                SimpleNamespace(), [], review_memory=True, explicit=True)
            target()
            assert captured["explicit"] is True

    @pytest.mark.parametrize("explicit", [True, False])
    def test_fork_keeps_background_review_origin_and_carries_attendedness(self, explicit):
        """Every curator/skill guard keys on the background_review origin, so an explicit /refine
        must NOT change the origin; attendedness rides on the fork as its own flag."""
        forks = []

        def fake_build(agent, task_cfg=None, *, max_iterations, write_origin="background_review"):
            fork = SimpleNamespace(
                _memory_enabled=True, _user_profile_enabled=False, _memory_write_origin=write_origin,
                run_conversation=lambda **kw: None, _session_messages=[])
            forks.append(fork)
            return fork, {}, False

        noop = lambda *a, **k: None
        with patch.object(bg, "build_cache_parity_fork", fake_build), \
                patch.object(bg, "_track_review_fork", noop), \
                patch.object(bg, "_snapshot_review_usage", lambda a: {}), \
                patch.object(bg, "_record_review_usage_to_parent", noop), \
                patch.object(bg, "finish_background_review_run", noop), \
                patch.object(bg, "_release_fork_clients", noop):
            bg._run_review_fork(SimpleNamespace(), [], "p", None, None, bg._ReviewForkState(), True, explicit)
        (fork,) = forks
        assert fork._memory_write_origin == "background_review"
        assert fork._review_attended is explicit

    @pytest.mark.parametrize("routed", [False, True])
    def test_fork_seeds_read_marks_from_the_history_it_replays(self, routed):
        """Read-before-write: the fork seeds marks from exactly the history it will see (full
        snapshot, or the digest when routed), after resetting and in its own context."""
        seeded, replayed = [], []
        snapshot = [{"role": "user", "content": "hi"}]

        def fake_build(agent, task_cfg=None, *, max_iterations, write_origin="background_review"):
            fork = SimpleNamespace(
                _memory_enabled=True, _user_profile_enabled=False, _memory_write_origin=write_origin,
                run_conversation=lambda **kw: replayed.append(kw["conversation_history"]),
                _session_messages=[])
            return fork, {}, routed

        noop = lambda *a, **k: None
        digest = [{"role": "user", "content": "digest"}]
        with patch.object(bg, "build_cache_parity_fork", fake_build), \
                patch.object(bg, "_digest_history", lambda m: digest), \
                patch.object(bg, "_track_review_fork", noop), \
                patch.object(bg, "_snapshot_review_usage", lambda a: {}), \
                patch.object(bg, "_record_review_usage_to_parent", noop), \
                patch.object(bg, "finish_background_review_run", noop), \
                patch.object(bg, "_release_fork_clients", noop), \
                patch("tools.skill_manager_guards.seed_background_review_reads_from_history",
                      lambda history, session_id=None, stale=None: seeded.append((history, session_id)) or 0):
            bg._run_review_fork(SimpleNamespace(session_id="s1"), snapshot, "p", None, None,
                                bg._ReviewForkState())
        expected = digest if routed else snapshot
        assert seeded == [(expected, "s1")]
        assert replayed == [expected]


    def test_fork_prompt_names_skills_whose_replayed_copy_is_stale(self):
        """The parent loaded kanban-orchestrator; another session's review edited it since. The
        fork must be told its copy is stale, else it patches blind and read-before-write refuses
        (coach bug: kanban-orchestrator refusals in long-lived sessions)."""
        prompts = []

        def fake_build(agent, task_cfg=None, *, max_iterations, write_origin="background_review"):
            fork = SimpleNamespace(
                _memory_enabled=True, _user_profile_enabled=False, _memory_write_origin=write_origin,
                run_conversation=lambda **kw: prompts.append(kw["user_message"]),
                _session_messages=[])
            return fork, {}, False

        def fake_seed(history, session_id=None, stale=None):
            stale.extend([("kanban-orchestrator", None), ("bb", "references/x.md")])
            return 0

        noop = lambda *a, **k: None
        with patch.object(bg, "build_cache_parity_fork", fake_build), \
                patch.object(bg, "_track_review_fork", noop), \
                patch.object(bg, "_snapshot_review_usage", lambda a: {}), \
                patch.object(bg, "_record_review_usage_to_parent", noop), \
                patch.object(bg, "finish_background_review_run", noop), \
                patch.object(bg, "_release_fork_clients", noop), \
                patch("tools.skill_manager_guards.seed_background_review_reads_from_history", fake_seed):
            bg._run_review_fork(SimpleNamespace(session_id="s1"), [{"role": "user", "content": "hi"}],
                                "p", None, None, bg._ReviewForkState())
        assert len(prompts) == 1
        assert "kanban-orchestrator (SKILL.md)" in prompts[0]
        assert "bb (references/x.md)" in prompts[0]
        assert "stale" in prompts[0] and "skill_view" in prompts[0]


class TestConsolidationProposalSurfaces:
    """The fork's own review summary is never published back, so a consolidation the delete
    gate staged must surface through ``summarize_background_review_actions`` — otherwise the
    near-limit denial path drops both the requested update and the proposal, silently (#105921)."""

    def _store(self, tmp_path, monkeypatch):
        import json as _json
        from tools.memory_tool_store import MemoryStore

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
        store = MemoryStore(memory_char_limit=500, user_char_limit=300)
        store.load_from_disk()
        return store

    def test_staged_proposal_surfaces_in_summary(self, tmp_path, monkeypatch):
        import json

        from tools.memory_tool import memory_tool
        from tools.skill_provenance import set_current_write_origin, reset_current_write_origin

        store = self._store(tmp_path, monkeypatch)
        assert store.add("memory", "standing rule entry")["success"] is True

        token = set_current_write_origin("background_review")
        try:
            raw = memory_tool(
                action="patch", old_text="standing rule", content="consolidated entry", store=store)
        finally:
            reset_current_write_origin(token)
        result = json.loads(raw)
        assert result["staged"] is True and result["proposal_staged"] is True

        review_messages = [
            {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "memory", "arguments": json.dumps(
                {"action": "patch", "old_text": "standing rule", "content": "consolidated entry"})}}]},
            {"role": "tool", "tool_call_id": "c1", "content": raw},
        ]
        actions = bg.summarize_background_review_actions(review_messages, [])
        assert any("staged for your approval" in a for a in actions)

    def test_near_limit_denial_end_to_end(self, tmp_path, monkeypatch):
        """add rejected by the budget -> fork follows the 'consolidate now' hint with a
        patch -> the delete gate stages it -> the proposal surfaces; the store never
        changed and nothing was silently lost."""
        import json

        from tools.memory_tool import memory_tool
        from tools.skill_provenance import set_current_write_origin, reset_current_write_origin

        store = self._store(tmp_path, monkeypatch)
        assert store.add("memory", "seed entry one")["success"] is True
        # Near-limit: a further add is rejected and the store's hint says to consolidate.
        assert store.add("memory", "x" * 600)["success"] is False

        token = set_current_write_origin("background_review")
        try:
            add_raw = memory_tool(action="add", content="y" * 600, store=store)
            replace_raw = memory_tool(
                action="patch", old_text="seed entry one", content="merged entry", store=store)
        finally:
            reset_current_write_origin(token)
        assert json.loads(add_raw)["success"] is False  # the budget still rejects the add
        replace_result = json.loads(replace_raw)
        assert replace_result["staged"] is True and replace_result["proposal_staged"] is True

        # Fail-closed: nothing was applied or dropped.
        assert "seed entry one" in store._entries_for("memory")
        assert "merged entry" not in store._entries_for("memory")

        review_messages = [
            {"role": "assistant", "tool_calls": [
                {"id": "c1", "function": {"name": "memory", "arguments": json.dumps(
                    {"action": "add", "content": "y" * 600})}},
                {"id": "c2", "function": {"name": "memory", "arguments": json.dumps(
                    {"action": "patch", "old_text": "seed entry one", "content": "merged entry"})}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": add_raw},
            {"role": "tool", "tool_call_id": "c2", "content": replace_raw},
        ]
        actions = bg.summarize_background_review_actions(review_messages, [])
        assert any("staged for your approval" in a for a in actions)


class TestReviewPromptLiveBudget:
    """An unattended memory review gets live per-store free space plus the staging rule; the
    replayed system-prompt header is frozen at session start and was days stale."""

    def _agent(self, tmp_path, monkeypatch):
        from tools.memory_tool_store import MemoryStore
        monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
        store = MemoryStore(memory_char_limit=200, user_char_limit=100)
        store.load_from_disk()
        store.add("memory", "m" * 150)
        return SimpleNamespace(_memory_store=store)

    def test_unattended_memory_review_prompt_has_live_budget(self, tmp_path, monkeypatch):
        agent = self._agent(tmp_path, monkeypatch)
        _, prompt = bg.spawn_background_review_thread(agent, [], review_memory=True, review_skills=False)
        assert "Live memory budget" in prompt
        assert "150/200 chars — a new entry of at most 47 chars fits" in prompt
        assert "staged for the user's approval" in prompt
        _, combined = bg.spawn_background_review_thread(agent, [], review_memory=True, review_skills=True)
        assert "Live memory budget" in combined

    def test_skill_only_and_explicit_reviews_omit_it(self, tmp_path, monkeypatch):
        agent = self._agent(tmp_path, monkeypatch)
        _, skills = bg.spawn_background_review_thread(agent, [], review_memory=False, review_skills=True)
        _, explicit = bg.spawn_background_review_thread(
            agent, [], review_memory=True, review_skills=False, explicit=True)
        assert "Live memory budget" not in skills
        assert "Live memory budget" not in explicit

    def test_agent_without_store_keeps_plain_prompt(self):
        _, prompt = bg.spawn_background_review_thread(SimpleNamespace(), [], review_memory=True)
        assert prompt == bg._MEMORY_REVIEW_PROMPT
