"""Bounded observation recovery; an uncertain cron fire is never submitted again."""
import asyncio

from hermes_cli.gateway_client import GatewayClientError, GatewayRPCError, GatewayUnavailableError

RECONNECT_SECONDS = 120.0
RECONNECT_DELAY = 2.0


async def observe_remote(connect, observe, attempted):
    """Keep an external firer alive across owner transport loss, not owner refusal.

    The caller journals before submit and recovers that identity on every re-entry.
    The retry window is 120 seconds plus one bounded connection/RPC attempt;
    healthy long jobs retain their ordinary inactivity policy. Missing/unknown
    receipts still fail closed.
    """
    deadline = None
    loop = asyncio.get_running_loop()
    while True:
        try:
            async with connect() as client:
                async def call(op, data):
                    nonlocal deadline
                    result = await client.rpc('cron.' + op, **data)
                    if op == 'status':
                        deadline = None  # verified observation, not merely an open socket/receipt
                    return result
                return await observe(call)
        except GatewayClientError as exc:
            if (not attempted() or isinstance(exc, GatewayRPCError)
                    or isinstance(exc, GatewayUnavailableError)
                    and exc.state not in {'absent', 'starting', 'draining'}):
                raise
            if deadline is None:
                deadline = loop.time() + RECONNECT_SECONDS
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise
            await asyncio.sleep(min(RECONNECT_DELAY, remaining))
