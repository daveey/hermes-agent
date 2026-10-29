"""Message hygiene at the resolved auxiliary client boundary."""

from openai import AsyncOpenAI, OpenAI
from typing import Any

from agent.transports.chat_completions import ChatCompletionsTransport


def prepare_chat_messages(client, kwargs: dict) -> dict:
    """Sanitize actual Chat Completions SDK requests, not native adapter replay.

    Auxiliary and MoA callers can retain a prepared request before the virtual
    transport sanitizes its copy. The resolved SDK client identifies the wire;
    native Messages/Responses adapters must retain their reasoning sidecars.
    """
    if not isinstance(client, (OpenAI, AsyncOpenAI)) or "messages" not in kwargs:
        return kwargs
    from agent.reasoning_carriers import shape_wire_carriers

    base_url = str(getattr(client, "base_url", "") or "")
    messages = ChatCompletionsTransport().convert_messages(
        shape_wire_carriers(kwargs["messages"], model=kwargs.get("model"), base_url=base_url),
        model=kwargs.get("model"), base_url=base_url,
    )
    return {**kwargs, "messages": messages}


def translate_anthropic_response_format(anthropic_kwargs: dict[str, Any], response_format: Any) -> None:
    """Merge an OpenAI response format into Anthropic ``output_config``."""
    if not isinstance(response_format, dict):
        return
    format_type = response_format.get("type")
    if format_type == "json_schema":
        json_schema = response_format.get("json_schema")
        if not isinstance(json_schema, dict) or "schema" not in json_schema:
            return
        schema = json_schema["schema"]
    elif format_type == "json_object":
        # Anthropic SDK has no schema-less JSON mode; only ``json_schema``.
        schema = {"type": "object"}
    else:
        return
    output_config = anthropic_kwargs.get("output_config")
    if not isinstance(output_config, dict):
        output_config = {}
        anthropic_kwargs["output_config"] = output_config
    output_config["format"] = {"type": "json_schema", "schema": schema}


def relay_chat_metadata(metadata: dict) -> dict:
    """Relay metadata for an auxiliary attempt.

    Relay intercepts ``client.chat.completions.create``, so it decodes a chat-completions
    request even when the adapter speaks Responses or Messages to its provider.
    """
    return {**metadata, "api_mode": "chat_completions"}
