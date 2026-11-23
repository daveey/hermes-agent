"""Fresh queue schema names without one SQLite/Python handoff per column. [daveey]"""
import sqlite3

from hermes_cli.kanban_db_values import _lossy_text


def column_names(conn, table):
    """Pack ordinary schema text; retain literal minimal/custom-reader semantics.

    No caching or snapshot changes. Legacy SQLite builds without the optional
    packing aggregate use the predecessor PRAGMA; unrelated failures propagate.
    """
    from hermes_cli.kanban_table_columns import table_columns

    factory = getattr(conn, 'text_factory', None)
    if factory is str or factory is _lossy_text:
        try:
            return table_columns(conn, table)
        except sqlite3.OperationalError as exc:
            if str(exc) != 'no such function: json_group_array':
                raise
    return {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
