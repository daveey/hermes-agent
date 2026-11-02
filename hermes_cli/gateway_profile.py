"""CLI renderer for the live gateway's on-demand profiler."""
import json
import sys


def profile_command(args):
    from gateway.control_profile import validate_params
    from gateway.control_socket import query_gateway_control
    from hermes_constants import get_hermes_home

    params = dict(seconds=args.seconds, hz=args.hz, top=args.top)
    try:
        seconds, _, _ = validate_params(params)
    except ValueError as exc:
        print(f'gateway profile: {exc}', file=sys.stderr)
        raise SystemExit(2) from exc
    result = query_gateway_control(get_hermes_home(), 'profile', params=params, timeout=seconds + 10)
    if result is None:
        print('gateway profile: no result (gateway unavailable, unsupported, or another profile is running)', file=sys.stderr)
        raise SystemExit(1)
    if args.json:
        print(json.dumps(result))
        return
    print(f"{result['samples']} wall samples over {result['seconds']}s at {result['hz']} Hz")
    print('CPU seconds   Samples  Native ID  Thread')
    for row in result['threads']:
        cpu = 'unknown' if row['cpu_s'] is None else f"{row['cpu_s']:.6f}"
        partial = ' (partial/lower bound)' if row.get('cpu_partial') else ''
        print(f"{cpu:>11} {row['samples']:>9} {row['native_id']:>10}  {row['name']}{partial}")
        for field, title in [('top_self', 'SELF'), ('top_inclusive', 'INCLUSIVE')]:
            print(f'  {title} (wall samples, not CPU attribution)')
            for frame, count in row[field]:
                print(f'    {count:>6}  {frame}')
    if result.get('truncated'):
        print(f"Truncated: {result['omitted_threads']} threads and {result['omitted_frames']} frame entries omitted")
