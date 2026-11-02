def load_tests(loader, tests, pattern):
    """Refuse unittest discovery: it imports test modules without conftest isolation."""
    raise RuntimeError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")
