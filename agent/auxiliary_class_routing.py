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
    extra = _task_extra_body(task, options)
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


# Tasks whose structured output is only a hint: the caller also parses prose
# (title_generator._extract_title_text), so under class routing they are served
# without response_format instead of failing closed on routes without attested JSON.
_BEST_EFFORT_JSON_TASKS = frozenset({"title_generation"})


def _task_extra_body(task, options):
    from agent import auxiliary_client as aux
    extra = aux._get_task_extra_body(task)
    extra.update(options.get("extra_body") or {})
    if task in _BEST_EFFORT_JSON_TASKS:
        extra.pop("response_format", None)
    return extra


def _requirements(task, options, extra):
    from agent import auxiliary_client as aux
    messages = options.get("messages") or []
    vision = task == "vision" or any(
        isinstance(block, dict) and block.get("type") in {"image", "image_url", "input_image"}
        for msg in messages for block in (msg.get("content") if isinstance(msg.get("content"), list) else []))
    # Same rough token estimator as compression/preflight: route caps are in
    # tokens and UTF-8 bytes overstate them ~3-4x. Includes tools and the
    # output reservation.
    from agent.model_metadata import estimate_request_tokens_rough
    context = estimate_request_tokens_rough(
        [m for m in messages if isinstance(m, dict)], tools=options.get("tools") or None)
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
    extra = _task_extra_body(task, options)
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


def _classify(runtime, client, exc):
    """The attempt's ``Failure``, classified once on the route it was sent to. [daveey]

    Upstream's ``_classify_failure`` returns an already-judged ``_ClassifiedAttempt``'s
    failure unchanged, and recovery passes this ``Failure`` on, so classifier hooks
    fire once per failed request.
    """
    from agent import auxiliary_client as aux
    from agent import auxiliary_error_policy as aux_errors
    if isinstance(exc, aux_errors.Failure):
        return exc
    return aux._classify_failure(exc, runtime.get("provider"), client, runtime.get("model"),
                                 runtime.get("base_url"))


def _provider_error(exc):
    """The provider's own error: never surface the ladder's ``_ClassifiedAttempt`` wrapper."""
    failure = getattr(exc, "failure", None)
    error = getattr(failure, "error", None)
    return error if isinstance(error, BaseException) else exc


def _recover(runtime, client, exc, attempted, failed):
    """Recover a failed attempt in place (None), or return the provider error to raise. [daveey]

    A managed-route fence violation is never recovered: it fails closed whatever a
    classifier hook says. Otherwise upstream's verdict decides, rung by rung: a
    credential verdict rotates the fenced pool first (even when it declines a
    fallback), and only a fallback label lets another class member take the request.
    """
    from agent import auxiliary_error_policy as aux_errors
    if isinstance(_provider_error(exc), ModelRoutingError):
        return _provider_error(exc)
    failure = _classify(runtime, client, exc)
    if _recover_class_pool(runtime, failure, attempted):
        return None
    if aux_errors.fallback_label(failure) is None:
        return failure.error
    failed.append((runtime["provider"], runtime["model"]))
    return None


def _recover_class_pool(runtime, exc, attempted):
    """An account quota is not a failed route. Rotate only the fenced pool. [daveey]

    Re-resolution still enforces the original class/pin and OAuth-only boundary.
    A (provider, model, account) is recovered at most once per logical call,
    including when concurrent writers or a broken pool reselect that account.
    ``exc`` is a raw error or its already-classified ``Failure``. The rotation is
    sized as upstream's ``_recover_provider_pool`` sizes it: the verdict's status
    and ``pool_failure_reason`` (usage-window and unverified-billing benches).
    Auth verdicts are left to class re-allocation: no token refresh is wired here.
    """
    from agent import auxiliary_error_policy as aux_errors
    from agent.credential_pool_model_cooldowns import pool_failure_reason
    from agent.error_classifier import FailoverReason
    failure = _classify(runtime, None, exc)
    verdict, error = failure.verdict, failure.error
    # Overload, transport, upstream-wrapped 429: the account itself is fine.
    if not verdict.should_rotate_credential or verdict.is_auth:
        return False
    pool = runtime.get("credential_pool")
    entry = pool.current() if pool else None
    if entry is None:
        return False
    identity = runtime["provider"], runtime["model"], entry.id
    if identity in attempted:
        return False
    attempted.add(identity)
    status = verdict.status_code
    error_context: dict[str, object] = {"message": str(error)}
    if status is not None:
        error_context["status_code"] = status
    else:
        status = (402 if aux_errors.benches_provider(verdict)
                  else 400 if verdict.reason is FailoverReason.model_entitlement else 429)
    return pool.mark_exhausted_and_rotate(
        status_code=status, credential_id=entry.id, model=runtime["model"],
        failure_reason=pool_failure_reason(verdict.reason, verdict.error_context, verdict.billing_unverified),
        error_context=error_context,
    ) is not None


def _complete(task, runtime, client, kwargs):
    from agent import auxiliary_client as aux
    return aux._validate_llm_response(aux._relay_sync_completion(
        client, kwargs, provider=runtime["provider"], api_mode=runtime["api_mode"],
        create=lambda request: aux._create_with_progress(
            client, request, task, force_stream=aux._provider_requires_stream(
                runtime["provider"], runtime["base_url"]), provider=runtime["provider"])), task)


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
    attempted = set()
    while True:
        runtime, client, kwargs = _prepare(task, options, request, failed)
        delivered = False
        stream = _stream(task, runtime, client, kwargs, options)
        terminal = None
        try:
            for chunk in stream:
                delivered = True
                yield chunk
            return
        except Exception as exc:  # health: allow BLE001 -- classified once, then recovered or re-raised below
            # Output already escaped: never replay, and raise the provider's error as is.
            terminal = _provider_error(exc) if delivered else _recover(runtime, client, exc, attempted, failed)
        finally:
            stream.close()
        if terminal is not None:
            raise terminal


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
        attempted = set()
        while True:
            runtime, client, kwargs = _prepare(task, options, request, failed)
            terminal = None
            try:
                return _complete(task, runtime, client, kwargs)
            except Exception as exc:  # health: allow BLE001 -- classified once, then recovered or re-raised below
                terminal = _recover(runtime, client, exc, attempted, failed)
            finally:
                aux._close_cached_client(client, close_async=True)
            if terminal is not None:
                raise terminal
    return wrapped


def class_routed_async(callback):
    @wraps(callback)
    async def wrapped(task=None, **options):
        from agent import auxiliary_client as aux
        request = _class_request(task, options)
        if request is None:
            return await callback(task, **options)
        failed = []
        attempted = set()
        while True:
            runtime, sync_client, kwargs = _prepare(task, options, request, failed)
            client, _ = aux._to_async_client(sync_client, runtime["model"])
            client._managed_subscription_runtime = runtime
            terminal = None
            try:
                async def create(request_kwargs):
                    return await aux._acreate_with_progress(
                        client, request_kwargs, task, force_stream=aux._provider_requires_stream(
                            runtime["provider"], runtime["base_url"]), provider=runtime["provider"])
                return aux._validate_llm_response(await aux._relay_async_completion(
                    client, kwargs, provider=runtime["provider"], api_mode=runtime["api_mode"], create=create), task)
            except Exception as exc:  # health: allow BLE001 -- classified once, then recovered or re-raised below
                terminal = _recover(runtime, sync_client, exc, attempted, failed)
            finally:
                aux._close_cached_client(client, close_async=True)
                aux._close_cached_client(sync_client, close_async=True)
            if terminal is not None:
                raise terminal
    return wrapped
