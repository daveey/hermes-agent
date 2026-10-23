"""Class-safe compatibility for callers that probe auxiliary availability.

The returned facade defers actual allocation until request capabilities are
known. It never exposes a raw provider client that could bypass the class fence.
"""
from functools import wraps
from types import SimpleNamespace

from agent.auxiliary_class_routing import _class_request
from agent.model_routing import ModelRoutingError
from agent.model_routing_runtime import resolve_class_runtime


class DeferredClassClient:
    def __init__(self, task, model, main_runtime):
        self.task = task
        self.model = model
        self.main_runtime = main_runtime
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, *, model=None, **kwargs):
        from agent import auxiliary_client as aux
        if model not in (None, self.model):
            raise ModelRoutingError('A class auxiliary client cannot switch to a concrete model')
        return aux.call_llm(self.task, model=self.model, provider='auto',
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
