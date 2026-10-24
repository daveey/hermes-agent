"""Class auxiliary jobs: shared allocation, fenced credentials, no legacy ladder.

Keep this boundary outside the legacy resolver: its discovery and auth recovery
may intentionally use paid credentials, even when an explicit model was supplied.
"""
from functools import wraps
from types import SimpleNamespace
from uuid import uuid4

from agent.model_routing import ModelRoutingError, is_class_selector
from agent.model_routing_runtime import resolve_class_runtime


def _requested_model(task_config, options, runtime):
    from agent import auxiliary_client as aux
    requested = options.get("model")
    if requested in (None, "", "auto"):
        requested = task_config.get("model")
    if requested not in (None, "", "auto"):
        return requested
    # A specialized provider/transport pin is not permission to route its job to
    # another provider, even when the main agent uses classes.
    for source in (options, task_config):
        if source.get("provider") not in (None, "", "auto"):
            return None
        if any(source.get(k) for k in ("base_url", "api_key", "api_mode", "key_env", "api_key_env")):
            return None
    if runtime.get("requested_class"):
        return "class:" + runtime["requested_class"]
    # A live concrete user switch beats configured class defaults. Never infer a
    # model's class from its name or from another process-global session.
    if runtime.get("model"):
        return runtime["model"]
    return aux._read_main_model()


def _class_request(task, options):
    from agent import auxiliary_client as aux
    task_config = aux._get_auxiliary_task_config(task) if task else {}
    runtime = aux._normalize_main_runtime(options.get("main_runtime"))
    requested = _requested_model(task_config, options, runtime)
    from agent.model_routing_managed import managed_config, pin_policy
    config = managed_config()
    if not is_class_selector(requested) and config is None:
        return None
    provider, model, base, key, mode = aux._resolve_task_provider_model(
        task, options.get("provider"), requested,
        options.get("base_url"), options.get("api_key"))
    declared_model = model
    pinned = not is_class_selector(model)
    if pinned:
        level, config = pin_policy(model, provider, config)
        model, provider = "class:" + level, "auto"
    overrides = (base, key, mode, options.get("api_mode"), options.get("api_key"),
                 task_config.get("base_url"), task_config.get("api_key"),
                 task_config.get("key_env"), task_config.get("api_key_env"))
    if provider not in (None, "", "auto") or any(overrides):
        raise ModelRoutingError("Auxiliary class requests require provider auto and no transport overrides")
    extra = aux._get_task_extra_body(task)
    extra.update(options.get("extra_body") or {})
    if set(extra) & {"model", "messages", "tools", "stream", "max_tokens", "max_completion_tokens"}:
        raise ModelRoutingError("Auxiliary class extra_body cannot override request identity or capabilities")
    headers = options.get("extra_headers") or {}
    if {k.lower() for k in headers} & {"authorization", "x-api-key", "api-key", "host"}:
        raise ModelRoutingError("Auxiliary class headers cannot override authentication or destination")
    owner = runtime.get("session_id") or str(uuid4())
    # Separate side jobs from the main allocation; retries/restarts of a named
    # session's same task reuse its durable route without pinning unrelated jobs.
    identity = f"aux:{owner}:{task or 'call'}:{declared_model}"
    requirements = _requirements(task, options, extra)
    return model, identity, requirements, config, declared_model


def _requirements(task, options, extra):
    from agent import auxiliary_client as aux
    messages = options.get("messages") or []
    vision = task == "vision" or any(
        isinstance(block, dict) and block.get("type") in {"image", "image_url", "input_image"}
        for msg in messages for block in (msg.get("content") if isinstance(msg.get("content"), list) else []))
    # Conservative byte bound avoids selecting a route too small before a
    # provider-specific tokenizer is known. Include output reservation and tools.
    import json
    context = len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
    context += len(json.dumps(options.get("tools") or []).encode("utf-8"))
    context += options.get("max_tokens") or 4096
    return {"tools": bool(options.get("tools")), "vision": vision,
            "streaming": True, "json": bool(extra.get("response_format")),
            "min_context_tokens": max(context, aux._task_minimum_context_length(task) or 0)}


def _client(runtime):
    """Construct from the already fenced credential, never select a second pool."""
    from agent import auxiliary_client as aux
    provider, model = runtime["provider"], runtime["model"]
    key, base = runtime["api_key"], runtime["base_url"]
    if runtime["api_mode"] == "anthropic_messages":
        from agent.anthropic_adapter import build_anthropic_client
        return aux.AnthropicAuxiliaryClient(build_anthropic_client(key, base), model, key, base,
                                           is_oauth=key.startswith("sk-ant-oat"))
    if provider in {"openai-codex", "xai-oauth"}:
        if provider == "openai-codex":
            headers = aux._codex_cloudflare_headers(key, base_url=base)
        else:
            from tools.xai_http import hermes_xai_default_headers
            headers = hermes_xai_default_headers()
        real = aux._create_openai_client(api_key=key, base_url=base, default_headers=headers)
        return aux.CodexAuxiliaryClient(real, model)
    client, resolved = aux.resolve_provider_client(
        provider, model, explicit_api_key=key, explicit_base_url=base, api_mode=runtime["api_mode"])
    if client is None or resolved != model:
        raise ModelRoutingError("Auxiliary class client did not preserve the selected route")
    return client


def _prepare(task, options, request, failed):
    from agent import auxiliary_client as aux
    model, identity, requirements, config, _ = request
    runtime = resolve_class_runtime(model, "auto", config=config, session_id=identity,
                                    requirements=requirements, excluded=failed)
    client = _client(runtime)
    client._managed_subscription_runtime = runtime
    if hasattr(client, "_real_client"):
        client._real_client._managed_subscription_runtime = runtime
    extra = aux._get_task_extra_body(task)
    extra.update(options.get("extra_body") or {})
    kwargs = aux._build_call_kwargs(
        runtime["provider"], runtime["model"], options["messages"],
        temperature=options.get("temperature"), max_tokens=options.get("max_tokens"),
        tools=options.get("tools"), timeout=aux._effective_aux_timeout(task, options.get("timeout")),
        extra_body=extra, reasoning_config=options.get("reasoning_config"),
        base_url=runtime["base_url"], task=task,
        no_progress_timeout=aux._get_task_no_progress_timeout(task)
        if isinstance(client, aux.CodexAuxiliaryClient) else None)
    if options.get("extra_headers"):
        kwargs["extra_headers"] = dict(options["extra_headers"])
    aux._set_relay_auxiliary_route(runtime["provider"], runtime["model"], runtime["api_mode"])
    info = options.get("route_info")
    if info is not None:
        info.update(runtime["model_routing"], provider=runtime["provider"], model=runtime["model"])
    return runtime, client, kwargs


def _retryable(exc):
    from agent import auxiliary_client as aux
    return any(predicate(exc) for predicate, _ in aux._FALLBACK_REASONS)


def _complete(task, runtime, client, kwargs):
    from agent import auxiliary_client as aux
    return aux._validate_llm_response(aux._relay_sync_completion(
        client, kwargs, provider=runtime["provider"], api_mode=runtime["api_mode"],
        create=lambda request: aux._create_with_progress(
            client, request, task, force_stream=aux._provider_requires_stream(
                runtime["provider"], runtime["base_url"]))), task)


def _stream(task, runtime, client, kwargs, options):
    from agent import auxiliary_client as aux
    try:
        if aux._client_streams_internally(client):
            # Native adapters aggregate their SDK stream; expose one normalized
            # chunk, preserving tool calls, rather than iterating a response object.
            response = _complete(task, runtime, client, kwargs)
            choice = response.choices[0]
            yield SimpleNamespace(choices=[SimpleNamespace(index=0, delta=choice.message,
                                                           finish_reason=choice.finish_reason)],
                                  model=runtime["model"], usage=getattr(response, "usage", None))
        else:
            kwargs["stream"] = True
            if options.get("stream_options"):
                kwargs["stream_options"] = options["stream_options"]
            yield from aux._relay_sync_stream(client, kwargs, provider=runtime["provider"],
                                              api_mode=runtime["api_mode"])
        # Never replay a stream after yielding any output.
    finally:
        aux._close_cached_client(client, close_async=True)


def _stream_with_recovery(task, options, request):
    # Lazy allocation: an unconsumed generator must not leak a client or consume
    # an allocation. Once any chunk escapes, replay would duplicate output.
    failed = []
    while True:
        runtime, client, kwargs = _prepare(task, options, request, failed)
        delivered = False
        stream = _stream(task, runtime, client, kwargs, options)
        try:
            for chunk in stream:
                delivered = True
                yield chunk
            return
        except Exception as exc:
            if delivered or not _retryable(exc):
                raise
            failed.append((runtime["provider"], runtime["model"]))
        finally:
            stream.close()


def class_routed_sync(callback):
    @wraps(callback)
    def wrapped(task=None, **options):
        from agent import auxiliary_client as aux
        request = _class_request(task, options)
        if request is None:
            return callback(task, **options)
        if options.get("stream"):
            return _stream_with_recovery(task, options, request)
        failed = []
        while True:
            runtime, client, kwargs = _prepare(task, options, request, failed)
            try:
                return _complete(task, runtime, client, kwargs)
            except Exception as exc:
                if not _retryable(exc):
                    raise
                failed.append((runtime["provider"], runtime["model"]))
            finally:
                aux._close_cached_client(client, close_async=True)
    return wrapped


def class_routed_async(callback):
    @wraps(callback)
    async def wrapped(task=None, **options):
        from agent import auxiliary_client as aux
        request = _class_request(task, options)
        if request is None:
            return await callback(task, **options)
        failed = []
        while True:
            runtime, sync_client, kwargs = _prepare(task, options, request, failed)
            client, _ = aux._to_async_client(sync_client, runtime["model"])
            client._managed_subscription_runtime = runtime
            try:
                async def create(request_kwargs):
                    return await aux._acreate_with_progress(
                        client, request_kwargs, task, force_stream=aux._provider_requires_stream(
                            runtime["provider"], runtime["base_url"]))
                return aux._validate_llm_response(await aux._relay_async_completion(
                    client, kwargs, provider=runtime["provider"], api_mode=runtime["api_mode"], create=create), task)
            except Exception as exc:
                if not _retryable(exc):
                    raise
                failed.append((runtime["provider"], runtime["model"]))
            finally:
                aux._close_cached_client(client, close_async=True)
                aux._close_cached_client(sync_client, close_async=True)
    return wrapped
