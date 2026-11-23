"""Private one-request worker; stdout contains only one bounded binary frame."""
from contextlib import redirect_stdout
import json
import struct
import sys

MAX_REQUEST = 16 * 1024
MAX_RESPONSE = 4 * 1024 * 1024


def capture(name, selector):
    from hermes_cli.web_routers import profiles
    from hermes_cli.web_json_response import native_json_response
    readers = {'sessions': profiles.get_profiles_sessions,
               'sessions_sidebar': profiles.get_profiles_sessions_sidebar.__wrapped__,
               'sessions_stamps': profiles.get_profiles_session_stamps}
    # No native binding is installed in this fresh child. Keep the canonical
    # SQL/policy/ordering and all seven annotation families, bypassing sidebar TTL.
    return native_json_response(readers[name](**selector)).body


def main():
    incoming, outgoing = sys.stdin.buffer, sys.stdout.buffer
    header = incoming.read(4)
    if len(header) != 4:
        return 2
    size = struct.unpack('!I', header)[0]
    if size > MAX_REQUEST:
        return 2
    request = incoming.read(size)
    if len(request) != size or incoming.read(1):
        return 2
    value = json.loads(request)
    if (not isinstance(value, dict) or set(value) != {'name', 'selector'}
            or value['name'] not in ('sessions', 'sessions_sidebar', 'sessions_stamps')
            or not isinstance(value['selector'], dict)):
        return 2
    # Libraries/plugins may print. Never permit such output to enter IPC framing.
    with redirect_stdout(sys.stderr):
        body = capture(value['name'], value['selector'])
    if len(body) > MAX_RESPONSE:
        return 3
    outgoing.write(struct.pack('!I', len(body)))
    outgoing.write(body)
    outgoing.flush()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
