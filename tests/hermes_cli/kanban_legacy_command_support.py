"""Seed pre-existing legacy command damage without rekeying captured identity.

Reader fault fixtures need corrupt historical rows, not a production UPDATE that
must now be refused. Keep real schema/guards installed; replace only a single
legacy (NULL-epoch) fixture row and retain every other column byte-for-byte.
"""


def seed_legacy_command(conn, **changes):
    rows = conn.execute('SELECT * FROM kanban_actionability_commands').fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row['home_generations'] is None, 'never rewrite epoch-bearing custody'
    assert changes.keys() <= row.keys()
    row.update(changes)
    columns = ','.join(row)
    placeholders = ','.join('?' for _ in row)
    conn.execute('DELETE FROM kanban_actionability_commands')
    conn.execute(f'INSERT INTO kanban_actionability_commands ({columns}) VALUES ({placeholders})', tuple(row.values()))
    actual = dict(conn.execute('SELECT * FROM kanban_actionability_commands').fetchone())
    assert actual == row
