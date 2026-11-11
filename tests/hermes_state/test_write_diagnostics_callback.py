"""Callback attribution uses only bounded code metadata, never user properties."""
import logging

import pytest

from hermes_state_write_diagnostics import log_write_duration


@pytest.mark.parametrize("bound", [False, True])
def test_callback_code_identity_not_mutable_name(caplog, bound):
    def operation(self=None):
        pass

    operation.__qualname__ = "mutable-secret"
    callback = operation.__get__(object()) if bound else operation
    with caplog.at_level(logging.WARNING, logger="hermes_state"):
        log_write_duration(1, "committed", callback)
    assert "callback=test_callback_code_identity_not_mutable_name.<locals>.operation" in caplog.text
    assert "mutable-secret" not in caplog.text


def test_opaque_callable_never_inspected(caplog):
    class Opaque:
        def __getattribute__(self, name):
            raise AssertionError("property-secret")

        def __repr__(self):
            raise AssertionError("repr-secret")

        def __call__(self, conn):
            return 1

    with caplog.at_level(logging.WARNING, logger="hermes_state"):
        log_write_duration(1, "committed", Opaque())
    assert "callback=opaque" in caplog.text
    assert "secret" not in caplog.text
