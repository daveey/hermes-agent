"""Final drain of a PTY master after its child exits.

``PtyProcess.isalive()`` turns False the moment the child is reaped, which can be before the reader
thread has read anything the child wrote: a reader starved of CPU from spawn to exit used to leave the
loop with the whole output still in the master's buffer and publish an exited session with no output.
"""

from tools.environments.fd_wait import open_fd_waiter


def drain_exited_pty(pty, handle_chunk, max_bytes: int) -> None:
    """Read what the master holds right now, without blocking. A descendant that keeps the slave open
    must not hold up completion, so only already-readable bytes are read, at most *max_bytes* of them."""
    fd = getattr(pty, "fd", None)
    if not isinstance(fd, int) or fd < 0:
        return
    waiter = open_fd_waiter(fd)
    if waiter is None:
        return
    drained = 0
    try:
        while drained < max_bytes and waiter.wait(0):
            try:
                chunk = pty.read(4096)
            except (EOFError, OSError):  # EIO: the slave side is closed and the buffer is empty
                return
            if not chunk:
                return
            drained += len(chunk)
            handle_chunk(chunk)
    finally:
        waiter.close()
