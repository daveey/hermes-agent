"""Private one-request worker; stdout contains only one bounded binary frame."""
from contextlib import redirect_stdout
import json
import struct
import sys

MAX_REQUEST = 16 * 1024
MAX_RESPONSE = 4 * 1024 * 1024


def capture(name, selector):
    from contextlib import nullcontext
    import os
    from hermes_cli.kanban_annotation_refresh import capture_role

    # health: allow HX002 -- creator-defined rollback switch, never a credential
    role = nullcontext() if os.environ.get('HERMES_SESSIONS_CAPTURE_COPY_FIRST') == '0' else capture_role()
    with role:
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
    # Libraries/plugins may print. Never permit such output to enter IPC framing.
    with redirect_stdout(sys.stderr):
        if isinstance(value, dict) and set(value) == {'views'}:
            body = capture_many(value['views'])
        elif (isinstance(value, dict) and set(value) == {'name', 'selector'}
              and valid_view([value['name'], value['selector']])):
            body = capture(value['name'], value['selector'])
        else:
            return 2
    if len(body) > MAX_RESPONSE:
        return 3
    outgoing.write(struct.pack('!I', len(body)))
    outgoing.write(body)
    outgoing.flush()
    return 0


def valid_view(view):
    return (isinstance(view, (list, tuple)) and len(view) == 2
            and view[0] in ('sessions', 'sessions_sidebar', 'sessions_stamps')
            and isinstance(view[1], dict))


def capture_many(views):
    if not isinstance(views, list) or not 1 <= len(views) <= 4 or not all(map(valid_view, views)):
        raise ValueError('invalid views')
    from hermes_cli.kanban_session_activity import one_annotation_snapshot
    results = []
    with one_annotation_snapshot():
        for name, selector in views:
            try:
                results.append({'body': json.loads(capture(name, selector))})
            except Exception:  # health: allow BLE001 -- one failed selector must not hide successful peers
                results.append({'unavailable': 'reader_error'})
    return json.dumps({'results': results}, separators=(',', ':'), allow_nan=False).encode()


if __name__ == '__main__':
    raise SystemExit(main())
