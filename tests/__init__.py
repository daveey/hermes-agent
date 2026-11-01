"""The repository suite requires pytest's collection-time safety fixtures."""


def load_tests(loader, tests, pattern):
    """Stop unittest discovery before it imports children without conftest isolation.

    Explicit, self-contained unittest modules remain runnable. Repository discovery
    bypasses credential stripping and live-state write fences as well as temporary
    Hermes homes.
    """
    raise RuntimeError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")
