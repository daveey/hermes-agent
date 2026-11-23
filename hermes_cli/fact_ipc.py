"""Length-bounded local socket framing, shared by owner and replicas. [daveey]"""
import socket
import struct

from hermes_cli.fact_wire import decode, encode

MAX_FRAME = 1024 * 1024


def _read(sock, count):
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise EOFError("fact host disconnected")
        data.extend(chunk)
    return bytes(data)


def receive(sock):
    size = struct.unpack("!I", _read(sock, 4))[0]
    if size > MAX_FRAME:
        raise ValueError("fact frame too large")
    return decode(_read(sock, size))


def send(sock, value):
    data = encode(value)
    if len(data) > MAX_FRAME:
        raise ValueError("fact frame too large")
    sock.sendall(struct.pack("!I", len(data)) + data)


def connect(path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(60)
    try:
        sock.connect(str(path))
    except OSError:
        sock.close()
        raise
    return sock
