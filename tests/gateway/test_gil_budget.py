"""gateway/gil_budget.py: the gateway runs with a 1 ms interpreter switch interval (API loop vs CPU threads)."""
import ast
import inspect
import sys

import pytest

from gateway import gil_budget


@pytest.fixture
def restore_interval():
    prev = sys.getswitchinterval()
    yield
    sys.setswitchinterval(prev)


def test_default_is_one_millisecond():
    assert gil_budget.gateway_switch_interval({}) == pytest.approx(0.001)


@pytest.mark.parametrize("raw,expected", [("2.5", 0.0025), ("0.5", 0.0005), ("0", None), ("-1", None),
                                          ("fast", None), ("  ", 0.001)])
def test_env_override(raw, expected):
    got = gil_budget.gateway_switch_interval({gil_budget.ENV: raw})
    assert got == (pytest.approx(expected) if expected is not None else None)


def test_apply_sets_the_process_interval(restore_interval):
    sys.setswitchinterval(0.005)
    assert gil_budget.apply_gateway_switch_interval({}) == pytest.approx(0.001)
    assert sys.getswitchinterval() == pytest.approx(0.001)


def test_disabled_override_keeps_the_current_interval(restore_interval):
    sys.setswitchinterval(0.005)
    assert gil_budget.apply_gateway_switch_interval({gil_budget.ENV: "0"}) == pytest.approx(0.005)


def test_start_gateway_applies_it_before_serving():
    """The bootstrap calls apply_gateway_switch_interval() before it builds the runner."""
    from gateway import run_bootstrap

    def _name_calls(fn):
        src = inspect.getsource(fn)
        return src, [n.func.id for n in ast.walk(ast.parse(src))
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]

    # start_gateway is the public entry; it delegates to the runtime body under the no-prompt guard.
    _, entry_calls = _name_calls(run_bootstrap.start_gateway)
    assert "_start_gateway_runtime" in entry_calls
    src, calls = _name_calls(run_bootstrap._start_gateway_runtime)
    assert "apply_gateway_switch_interval" in calls
    assert src.index("apply_gateway_switch_interval()") < src.index("GatewayRunner(")
