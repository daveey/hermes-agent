"""Local-work seam for auxiliary LLM calls."""
import functools
import inspect


def aux_call_scope(function):
    """Local-work seam: reserve at aux-call entry, release at exit (sync or async)."""
    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def _async_scoped(*args, **kwargs):
            from tools.code_kernel_scope import scope

            with scope("aux_call", "async-call-llm"):
                return await function(*args, **kwargs)

        return _async_scoped

    @functools.wraps(function)
    def _scoped(*args, **kwargs):
        from tools.code_kernel_scope import scope

        with scope("aux_call", "call-llm"):
            return function(*args, **kwargs)

    return _scoped
