"""Existing command custody adapted to final processed-prefix verdicts. [daveey]"""
from hermes_cli import kanban_actionability_transport as commands

FROZEN = ('retain', 'actionability_record')
CONTROLS = (*FROZEN, 'release')


def routing(wire):
    """Command identity routes to its task without enriching its signed bytes."""
    return ('tasks', wire['task']) if wire['op'] in CONTROLS else (wire['table'], wire['uid'])


def validate(conn, row, wire):
    """Immutable semantic command custody is independent of wire_json row caches."""
    if commands.outbound(conn, row) != wire:
        raise ValueError('issued command custody changed')


def settle(conn, row, verdict):
    """A retained final refusal is definitive, not an uncertain legacy response."""
    if verdict['status'] not in ('applied', 'rejected', 'cancelled'):
        raise ValueError('unsupported final command disposition; preserve custody')
    result = dict(verdict)
    if result['status'] == 'cancelled':
        result.update(status='rejected', reason='cancelled: ' + str(result.get('reason') or ''))
    return commands.record_result(conn, row, result, final=True)


def confirmed_transport(conn, owned):
    """Strict confirmations inspect receipts; never replay through fenced legacy ops."""
    from hermes_cli import kanban_replay_journal as journal
    if not journal.bound_custody(conn):
        return False
    if owned.get('delivery_state') not in ('acknowledged', 'confirmed'):
        raise ValueError('command still in replay custody; recover exact transport before confirmation')
    return True
