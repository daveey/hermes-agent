"""Socket observation for the Board's kept-alive HTTP transport. [daveey]"""


def peer_closed(conn) -> bool:
    """True when an idle kept-alive socket is readable (the server sent FIN) or already gone."""
    import select
    sock = getattr(conn, "sock", None)
    if sock is None:
        return True
    try:  # poll, not select: select raises for fds >= 1024, which a busy gateway has
        if not hasattr(select, "poll"):  # Windows: select has no fd ceiling there
            return bool(select.select([sock], [], [], 0)[0])
        poller = select.poll()
        poller.register(sock, select.POLLIN | select.POLLPRI)
        return bool(poller.poll(0))
    except (OSError, ValueError):
        return True
