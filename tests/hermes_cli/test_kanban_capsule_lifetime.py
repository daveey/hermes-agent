"""A trusted create scope is a lexical lease, not authority inherited by children."""
import asyncio
import contextvars
import os

import pytest

from hermes_cli.kanban_capabilities import CapabilityMismatchError
from tests.hermes_cli import test_kanban_capsule_admission as _admission

env = _admission.env  # shared fixture


def test_copied_context_cannot_create_after_scope_exit(env):
    handle = env.register()
    with handle.create_scope():
        copied = contextvars.copy_context()
    with pytest.raises(CapabilityMismatchError):
        copied.run(env.create)
    assert env.count() == 0


def test_async_child_cannot_create_after_scope_exit(env):
    handle = env.register()

    async def main():
        release = asyncio.Event()

        async def child():
            await release.wait()
            return env.create()

        with handle.create_scope():
            task = asyncio.create_task(child())
        release.set()
        with pytest.raises(CapabilityMismatchError):
            await task

    asyncio.run(main())
    assert env.count() == 0


def test_expired_scope_cannot_be_revived_by_a_new_scope(env):
    handle = env.register()
    with handle.create_scope():
        copied = contextvars.copy_context()
    with handle.create_scope():
        with pytest.raises(CapabilityMismatchError):
            copied.run(env.create)
        env.create()
    assert env.count() == 1


@pytest.mark.platforms("posix")
def test_forked_child_has_no_live_handle_or_scope(env):
    handle = env.register()
    read_fd, write_fd = os.pipe()
    try:
        with handle.create_scope():
            pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                try:
                    os.write(write_fd, b"live" if handle.live else b"revoked")
                finally:
                    os._exit(0)
            os.close(write_fd)
            write_fd = None
            observed = os.read(read_fd, 32)
            _, status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0
            assert observed == b"revoked"
            assert handle.live
            env.create()
        assert env.count() == 1
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
