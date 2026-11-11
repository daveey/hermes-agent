"""Google Gemini (AI Studio) provider profile.

Reports api_mode="chat_completions" but supplies its own client (``client.GeminiClient``): each call
goes to the Interactions API (``interactions.py``) or to ``generateContent`` (GeminiNativeClient), per
model. This profile also carries auth/endpoint metadata and the thinking_config translation hook.
"""

import logging
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)


class GeminiProfile(ProviderProfile):
    """Gemini — translate reasoning_config to thinking_config in extra_body."""

    def build_extra_body(self, *, session_id: str | None = None, **context: Any) -> dict[str, Any]:
        """Native: ``thinking_config``; OpenAI-compat /openai subpath:
        ``extra_body.google.thinking_config`` (snake_case)."""
        from agent.transports.chat_completions import (
            _build_gemini_thinking_config,
            _is_gemini_openai_compat_base_url,
            _snake_case_gemini_thinking_config,
        )

        raw = _build_gemini_thinking_config(context.get("model") or "", context.get("reasoning_config"))
        if not raw:
            return {}
        base_url = context.get("base_url") or self.base_url
        if self.name == "gemini" and _is_gemini_openai_compat_base_url(base_url):
            thinking_config = _snake_case_gemini_thinking_config(raw)
            return {"extra_body": {"google": {"thinking_config": thinking_config}}} if thinking_config else {}
        from agent.gemini_long_decode import long_decode_max_output_tokens
        from agent.gemini_native_adapter import is_native_gemini_base_url

        # Long decode exists only on native generateContent (not /openai, not proxies).
        budget = long_decode_max_output_tokens(context.get("model") or "", context.get("reasoning_config"))
        return {"thinking_config": raw, **({"max_output_tokens": budget} if budget and is_native_gemini_base_url(base_url) else {})}

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """[daveey] Native AI Studio ``/models`` rejects Bearer auth (401) and returns
        ``{"models": [{"name": "models/<id>", "supportedGenerationMethods": [...]}]}``, so list it
        with ``x-goog-api-key`` and parse that shape. ``/openai`` bases, Vertex express and custom
        proxies keep the OpenAI-compatible base implementation."""
        from agent.gemini_native_adapter import is_native_gemini_base_url

        base = ((base_url or "").strip() or self.base_url or "").rstrip("/")
        if not (is_native_gemini_base_url(base) and "generativelanguage.googleapis.com" in base.lower()):
            return super().fetch_models(api_key=api_key, base_url=base_url, timeout=timeout)
        if not api_key or not self.supports_model_listing:
            return None

        import json
        import urllib.parse
        import urllib.request

        from hermes_cli.chat_catalog import chat_catalog_ids
        from hermes_cli.urllib_security import open_credentialed_url
        from providers.base import _profile_user_agent

        items: list[dict[str, Any]] = []
        page_token = ""
        try:
            for _ in range(20):  # pagination guard
                query = {"pageSize": "1000", **({"pageToken": page_token} if page_token else {})}
                req = urllib.request.Request(f"{base}/models?{urllib.parse.urlencode(query)}")
                req.add_header("x-goog-api-key", api_key)
                req.add_header("Accept", "application/json")
                req.add_header("User-Agent", _profile_user_agent())
                for k, v in self.default_headers.items():
                    req.add_header(k, v)
                with open_credentialed_url(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode())
                for model in data.get("models") or []:
                    if not isinstance(model, dict):
                        continue
                    if "generateContent" not in (model.get("supportedGenerationMethods") or []):
                        continue
                    name = str(model.get("name") or "").strip()
                    mid = name.removeprefix("models/")
                    if mid:
                        items.append({"id": mid})
                page_token = str(data.get("nextPageToken") or "")
                if not page_token:
                    break
        except Exception as exc:  # never log the key: exc carries status/reason only
            import logging

            logging.getLogger(__name__).debug("fetch_models(%s): %s", self.name, exc)
            return None
        return chat_catalog_ids(items)

    def create_client(self, **client_kwargs: Any) -> Any:
        """The plugin's client (Interactions or generateContent per model) on a native Gemini base; None on
        the OpenAI-compatible ``/openai`` surface and proxies, which keep the standard OpenAI client."""
        from agent.gemini_native_adapter import is_native_gemini_base_url

        base_url = str(client_kwargs.get("base_url") or self.base_url or "")
        if not is_native_gemini_base_url(base_url):
            return None
        from .client import GeminiClient

        allowed = {"api_key", "base_url", "default_headers", "timeout", "http_client"}
        kwargs = {k: v for k, v in client_kwargs.items() if k in allowed}
        if "http_client" not in kwargs and (http_client := _keepalive_http_client(base_url)) is not None:
            kwargs["http_client"] = http_client
        return GeminiClient(**kwargs)


def _keepalive_http_client(base_url: str) -> Any:
    """The keepalive client (and the provider's ``ssl_ca_cert`` / ``ssl_verify``) the core gives a native
    Gemini client; the profile hook receives the client kwargs after the core has consumed the TLS keys."""
    try:
        from agent.process_bootstrap import build_keepalive_http_client
        from agent.ssl_verify import resolve_httpx_verify
        from hermes_cli.config_providers import get_custom_provider_tls_settings

        tls = get_custom_provider_tls_settings(base_url)
        verify = resolve_httpx_verify(ca_bundle=tls.get("ssl_ca_cert"), ssl_verify=tls.get("ssl_verify"), base_url=base_url)
        return build_keepalive_http_client(base_url, verify=verify)
    except Exception:  # GeminiNativeClient then builds its own httpx client
        logger.warning("Gemini: keepalive client unavailable; using a default client", exc_info=True)
        return None


gemini = GeminiProfile(
    name="gemini", aliases=("google", "google-gemini", "google-ai-studio"), api_mode="chat_completions",
    env_vars=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    base_url="https://generativelanguage.googleapis.com/v1beta", auth_type="api_key",
    default_aux_model="gemini-3.8-flash",  # [daveey] 3.8 is the id Google points new keys at
)

register_provider(gemini)
