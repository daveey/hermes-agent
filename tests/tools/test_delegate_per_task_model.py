"""Per-task ``model`` / ``provider`` on delegate_task ``tasks[]`` entries [daveey].

With ``delegation.allow_task_model`` on, a task's own pin overrides ``delegation.model``/``delegation.provider``
for THAT child only; tasks without a pin keep the call-wide route. Off (upstream default) the fields are not
advertised and are stripped. Entry path: the registered ``delegate_task`` tool handler (what the model calls).
"""
import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from tools import delegate_tool as dt
from tools.registry import registry


def _parent(depth=1):
    parent = MagicMock()
    parent.base_url = "https://api.anthropic.com"
    parent.api_key = "sk-parent"
    parent.provider = "anthropic"
    parent.api_mode = "anthropic_messages"
    parent.model = "claude-opus-5-5"
    parent.platform = "cli"
    parent.providers_allowed = parent.providers_ignored = parent.providers_order = parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent.request_overrides = None
    parent._fallback_chain = None
    return parent


def _child():
    child = MagicMock()
    child.run_conversation.return_value = {"final_response": "done", "completed": True, "api_calls": 1, "messages": []}
    child._delegate_saved_tool_names = []
    child._credential_pool = None
    child.session_prompt_tokens = child.session_completion_tokens = 0
    child.model = "x"
    return child


class TestTaskRouteCfg(unittest.TestCase):
    def test_no_pin_uses_call_route(self):
        self.assertEqual(dt._task_route_cfg({"goal": "g"}, {"model": "m"}), (None, None))
        self.assertEqual(dt._task_route_cfg({"goal": "g", "model": "  "}, {}), (None, None))

    def test_model_only_keeps_configured_provider_and_endpoint(self):
        cfg, err = dt._task_route_cfg({"model": "claude-opus-5-5"},
                                      {"model": "claude-sonnet-5-5", "provider": "anthropic", "base_url": "u"})
        self.assertIsNone(err)
        self.assertEqual(cfg, {"model": "claude-opus-5-5", "provider": "anthropic", "base_url": "u"})

    def test_new_provider_drops_old_endpoint_and_model(self):
        cfg, _ = dt._task_route_cfg({"provider": "openrouter"},
                                    {"model": "claude-sonnet-5-5", "provider": "anthropic", "base_url": "u",
                                     "api_key": "k", "api_mode": "anthropic_messages", "max_iterations": 9})
        self.assertEqual(cfg, {"model": "", "provider": "openrouter", "max_iterations": 9})

    def test_non_string_pin_is_an_error(self):
        cfg, err = dt._task_route_cfg({"model": 5}, {})
        self.assertIsNone(cfg)
        self.assertIn("'model' must be a string", err)


class TestDelegateToolPerTaskModel(unittest.TestCase):
    def _run(self, tasks, delegation_cfg, allow=True):
        handler = registry.get_entry("delegate_task").handler
        cfg = {"max_spawn_depth": 3, "allow_task_model": allow, **delegation_cfg}
        with patch("tools.delegate_tool._load_config", return_value=cfg), \
                patch("run_agent.AIAgent") as MockAgent:
            MockAgent.side_effect = lambda **kw: _child()
            out = handler({"tasks": tasks}, parent_agent=_parent(depth=1))
            models = [c.kwargs["model"] for c in MockAgent.call_args_list]
            providers = [c.kwargs["provider"] for c in MockAgent.call_args_list]
        return out, models, providers

    def test_schema_advertises_per_task_model_only_when_allowed(self):
        for allow in (True, False):
            with patch("tools.delegate_tool._load_config", return_value={"allow_task_model": allow}):
                ov = dt._build_dynamic_schema_overrides()
            props = ov["parameters"]["properties"]["tasks"]["items"]["properties"]
            self.assertEqual({"model", "provider"} <= set(props), allow)
            self.assertEqual("`tasks[]` entry pins `model`" in ov["description"], allow)
        # The static schema (what upstream's invariant test reads) never carries a route field.
        static = dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"]["properties"]
        self.assertFalse({"model", "provider"} & set(static))

    def test_pins_are_stripped_when_not_allowed(self):
        _out, models, _ = self._run(
            [{"goal": "adversarial review of the diff in /tmp/x", "model": "claude-opus-5-5"}],
            {"model": "claude-sonnet-5-5"}, allow=False,
        )
        self.assertEqual(models, ["claude-sonnet-5-5"])

    def test_task_model_overrides_delegation_model_for_that_child_only(self):
        out, models, providers = self._run(
            [{"goal": "adversarial review of the diff in /tmp/x", "model": "claude-opus-5-5"},
             {"goal": "summarize the changelog in /tmp/y please"}],
            {"model": "claude-sonnet-5-5"},
        )
        self.assertNotIn('"error"', out[:200], out)
        self.assertEqual(models, ["claude-opus-5-5", "claude-sonnet-5-5"])
        self.assertEqual(providers, ["anthropic", "anthropic"])  # inherited from the parent

    def test_task_model_with_no_delegation_model_beats_parent_inherit(self):
        _out, models, _ = self._run([{"goal": "cheap summarization task here", "model": "claude-sonnet-5-5"}], {})
        self.assertEqual(models, ["claude-sonnet-5-5"])

    def test_task_provider_pin_resolves_through_runtime_provider(self):
        seen = []

        def fake_resolve(cfg, parent_agent):
            seen.append(dict(cfg))
            prov = cfg.get("provider") or None
            return {"model": cfg.get("model") or None, "provider": prov,
                    "base_url": "https://openrouter.ai/api/v1" if prov else None,
                    "api_key": "sk-or" if prov else None, "api_mode": None, "request_overrides": None}

        with patch("tools.delegate_tool._resolve_delegation_credentials", side_effect=fake_resolve):
            _out, models, providers = self._run(
                [{"goal": "route this one elsewhere please", "model": "z-ai/glm-5.3", "provider": "openrouter"},
                 {"goal": "stay on the default route here"}],
                {"model": "claude-sonnet-5-5"},
            )
        self.assertEqual(models, ["z-ai/glm-5.3", "claude-sonnet-5-5"])
        self.assertEqual(providers, ["openrouter", "anthropic"])
        self.assertEqual(seen[0]["provider"], "openrouter")
        self.assertNotIn("provider", seen[1])

    def test_unavailable_default_does_not_block_explicit_provider(self):
        def resolve(*, requested, target_model):
            if requested == "anthropic":
                raise ValueError("No Anthropic credentials found")
            return {"provider": "openai-codex", "model": target_model,
                    "base_url": "https://chatgpt.com/backend-api/codex",
                    "api_key": "synthetic-token", "api_mode": "codex_responses"}

        with patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=resolve) as resolver:
            out, models, providers = self._run(
                [{"goal": "review this change using the selected provider", "provider": "openai-codex",
                  "model": "test-model"}],
                {"provider": "anthropic", "model": "claude-sonnet-5-5"},
            )
        self.assertNotIn("error", json.loads(out), out)
        self.assertEqual(models, ["test-model"])
        self.assertEqual(providers, ["openai-codex"])
        resolver.assert_called_once_with(requested="openai-codex", target_model="test-model")

    def test_unavailable_default_still_refuses_mixed_batch_before_spawn(self):
        def resolve(cfg, parent):
            if cfg.get("provider") == "anthropic":
                raise ValueError("No Anthropic credentials found")
            return {"model": "test-model", "provider": "openai-codex", "base_url": "https://example.com",
                    "api_key": "synthetic-token", "api_mode": "codex_responses"}

        with patch("tools.delegate_tool._resolve_delegation_credentials", side_effect=resolve):
            out, models, _ = self._run(
                [{"goal": "review this change on the selected route", "provider": "openai-codex"},
                 {"goal": "review this change on the default route"}],
                {"provider": "anthropic"},
            )
        self.assertIn("No Anthropic credentials found", json.loads(out)["error"])
        self.assertEqual(models, [])

    def test_bad_task_pin_refuses_whole_call_before_spawn(self):
        out, models, _ = self._run([{"goal": "this has a bogus model value", "model": ["x"]}], {})
        self.assertIn("Task 0", json.loads(out)["error"])
        self.assertEqual(models, [])


if __name__ == "__main__":
    unittest.main()
