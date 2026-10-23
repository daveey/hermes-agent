"""Class-safe compatibility for callers that probe auxiliary availability.

The returned facade defers actual allocation until request capabilities are
known. It never exposes a raw provider client that could bypass the class fence.
"""
from functools import wraps
from inspect import signature
from types import SimpleNamespace

from agent.auxiliary_class_routing import _class_request
from agent.model_routing import ModelRoutingError
from agent.model_routing_runtime import resolve_class_runtime


class DeferredClassClient:
    def __init__(self, task, model, main_runtime, async_mode=False):
        self.task = task
        self.model = model
        self.main_runtime = main_runtime
        self.async_mode = async_mode
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, *, model=None, **kwargs):
        from agent import auxiliary_client as aux
        if model not in (None, self.model):
            raise ModelRoutingError('A class auxiliary client cannot switch to a concrete model')
        if 'max_completion_tokens' in kwargs:
            if 'max_tokens' in kwargs:
                raise ModelRoutingError('Specify only one output token limit')
            kwargs['max_tokens'] = kwargs.pop('max_completion_tokens')
        if 'response_format' in kwargs:
            kwargs['extra_body'] = dict(kwargs.get('extra_body') or {}, response_format=kwargs.pop('response_format'))
        callback = aux.async_call_llm if self.async_mode else aux.call_llm
        # Do not let the class wrapper silently discard unsupported SDK options.
        signature(callback).bind(self.task, model=self.model, provider='auto',
                                 main_runtime=self.main_runtime, **kwargs)
        return callback(self.task, model=self.model, provider='auto',
                        main_runtime=self.main_runtime, **kwargs)

    def close(self):
        # No physical client is held: call_llm owns its request-scoped resources.
        return None


def class_readiness(callback):
    @wraps(callback)
    def wrapped(task='', *, main_runtime=None):
        request = _class_request(task or None, {'main_runtime': main_runtime})
        if request is None:
            return callback(task, main_runtime=main_runtime)
        model, _, requirements = request
        # Availability checks neither consume round-robin slots nor create sticky
        # sessions. Re-check the actual request at create() time, including images,
        # tools and context, rather than trusting this earlier readiness result.
        resolve_class_runtime(model, 'auto', session_id='aux-readiness',
                              requirements=requirements, state_path=':memory:')
        return DeferredClassClient(task or None, model, main_runtime), model
    return wrapped


def class_direct_resolver(callback):
    """Preserve the resolver's tuple API without exposing an unfenced client."""
    parameters = signature(callback)
    vision_resolver = callback.__name__ == 'resolve_vision_provider_client'

    @wraps(callback)
    def wrapped(*args, **kwargs):
        options = dict(parameters.bind(*args, **kwargs).arguments)
        task = 'vision' if vision_resolver or options.get('is_vision') else options.get('task')
        for source, target in (('explicit_base_url', 'base_url'), ('explicit_api_key', 'api_key')):
            if source in options:
                options[target] = options.pop(source)
        request = _class_request(task, options)
        if request is None:
            return callback(*args, **kwargs)
        if options.get('raw_codex'):
            raise ModelRoutingError('Class clients require normalized completions, not raw provider access')
        model, _, requirements = request
        resolve_class_runtime(model, 'auto', session_id='aux-readiness',
                              requirements=requirements, state_path=':memory:')
        client = DeferredClassClient(task, model, options.get('main_runtime'), options.get('async_mode', False))
        # Provider is deliberately unresolved until create knows the request's
        # capabilities; the concrete route is recorded by the shared allocator.
        return ('auto', client, model) if vision_resolver else (client, model)
    return wrapped
