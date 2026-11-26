"""Scalar validation before replay current-row projection touches SQLite."""
import math

from hermes_cli import kanban_db_sync as sync

MAX_INTEGER = (1 << 63) - 1


def integer(value, label, *, minimum=0):
    """Require an exact bounded integer, never bool or a coercible wire value."""
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise ValueError(f'invalid replay {label}')
    return value


def row_image(conn, table, image):
    """Validate actual projected columns against the destination's declared types.

    Wire-only and unknown columns are not projected. JSON server columns retain
    their structured contract; scalar columns must not borrow SQLite's coercions.
    """
    if table not in sync.SYNCED_TABLES or not isinstance(image, dict):
        raise ValueError('invalid replay current-row image')
    if not isinstance(image.get('uid'), str) or not image['uid']:
        raise ValueError('invalid replay current-row uid')
    integer(image.get('version'), 'current-row version', minimum=1)
    for key in ('hlc', 'origin_gateway'):
        if image.get(key) is not None and not isinstance(image[key], str):
            raise ValueError(f'invalid replay current-row {key}')
    if table == 'tasks':  # LOCAL columns the lease mirror still binds from the wire
        if image.get('lease_holder') is not None and not isinstance(image['lease_holder'], str):
            raise ValueError('invalid replay current-row lease_holder')
        if image.get('lease_expires') is not None:
            integer(image['lease_expires'], 'current-row lease_expires')
    allowed = sync.SYNCED_COLUMNS[table] | sync.SERVER_COLUMNS.get(table, frozenset())
    for column in conn.execute(f'PRAGMA table_info({table})'):
        name, kind = column[1], column[2].upper()
        wire = sync.REF_COLUMNS.get((table, name), name)
        if name not in allowed or wire not in image:
            continue
        value = image[wire]
        if value is None:
            if column[3]:
                raise ValueError(f'invalid replay current-row {wire}')
            continue
        if (table, name) in sync.REF_COLUMNS:
            ok = isinstance(value, str) and bool(value)
        elif name in sync.JSON_SERVER_COLUMNS.get(table, ()):
            ok = isinstance(value, (str, dict, list))
        else:
            ok = _scalar_ok(value, kind)
        if not ok:
            raise ValueError(f'invalid replay current-row {wire}')
    return image


def _scalar_ok(value, kind):
    """Whether ``value`` fits a declared SQLite column type without coercion."""
    low = -(MAX_INTEGER + 1)
    if 'INT' in kind:
        return type(value) is int and low <= value <= MAX_INTEGER
    if any(token in kind for token in ('REAL', 'FLOAT', 'DOUBLE')):
        if type(value) is int:
            return low <= value <= MAX_INTEGER
        return type(value) is float and math.isfinite(value)
    if 'BLOB' in kind:
        if not isinstance(value, dict) or set(value) != {'$blob'} or not isinstance(value['$blob'], str):
            return False
        try:
            bytes.fromhex(value['$blob'])
        except ValueError:
            return False
        return True
    return 'TEXT' not in kind or isinstance(value, str)
