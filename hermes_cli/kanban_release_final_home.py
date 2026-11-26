"""HOME final application transport; reply loss resolves immutable private result."""
from hermes_cli import kanban_release_application as app, kanban_release_final as final
from hermes_cli import kanban_release_materialize as materialize, kanban_release_replica as replica


def process(conn, hub, row, own, board):
    from hermes_cli import kanban_release_home as home
    from hermes_cli.kanban_sync_client import HubError
    home._identity(conn, hub, row, own, board)
    final.install(conn)
    wire = final.saved(conn, row['intent_uid'])
    if wire is None:
        proof = replica.verified_consumption(conn, home.queue.wire(row)['client_op_id'])
        if proof is None:
            raise ValueError('HOME final application lacks winning private consumption')
        wire = final.freeze(conn, proof, delivery_uid=row['delivery_uid'])
    # Completed private result first, before mutable owner/comments/TTL. It never
    # promotes from the historical task image or clears a later independent hold.
    prior = app.verified_application(conn, row['intent_uid'])
    if prior is not None:
        if materialize.verified(conn, row['intent_uid']) is None:
            raise ValueError('HOME application result lacks canonical private certificate')
        return prior
    try:
        value = hub.owner_release_application(board, row['intent_uid'])
    except HubError as exc:
        if exc.status != 404:
            raise
    else:
        home._identity(conn, hub, row, own, board)
        return materialize.materialize(conn, final.validate(value, wire))
    home._identity(conn, hub, row, own, board)
    # No writer during I/O. Even a rejected/unknown/lost POST has to reconcile the
    # same immutable operation; no new wire is ever created from a missing reply.
    try:
        hub.ops(board, [wire])
    except (OSError, HubError):
        value = hub.owner_release_application(board, row['intent_uid'])
    else:
        value = hub.owner_release_application(board, row['intent_uid'])
    home._identity(conn, hub, row, own, board)
    return materialize.materialize(conn, final.validate(value, wire))
