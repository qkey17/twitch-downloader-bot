"""Versioned HTML wire format only; spool/registry/source rows stay unchanged.

row = [time, user/color reference, text, fragments, badges reference, mask?, extra?]
mask bits follow FIELDS; omitted mask means all six fields present. References
are table indices or one inline value (user is a pair, badges uses [value]).
Tables have hard entry/key limits; high cardinality falls back to inline rows.
"""
import json

FORMAT = 'tcd-compact-v1'
FIELDS = ('t', 'user', 'text', 'fragments', 'color', 'badges')
USER_LIMIT = 2048
BADGE_LIMIT = 128


class _Table:
    def __init__(self, limit, key_limit):
        self.limit, self.key_limit = limit, key_limit
        self.values, self.index = [], {}

    def reference(self, value, *, wrapped=False):
        key = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        if len(key) <= self.key_limit:
            found = self.index.get(key)
            if found is not None:
                return found
            if len(self.values) < self.limit:
                found = len(self.values)
                # Retain a detached value, not any source row or mutable subobject.
                self.values.append(json.loads(key))
                self.index[key] = found
                return found
        return [value] if wrapped else value


class CompactRows:
    def __init__(self):
        self.users = _Table(USER_LIMIT, 256)
        self.badges = _Table(BADGE_LIMIT, 2048)

    def encode(self, row):
        user = self.users.reference([row.get('user'), row.get('color')])
        badges = self.badges.reference(row.get('badges'), wrapped=True)
        record = [row.get('t'), user, row.get('text'), row.get('fragments'), badges]
        mask = sum(1 << i for i, key in enumerate(FIELDS) if key in row)
        extra = {key: value for key, value in row.items() if key not in FIELDS}
        if mask != 63 or extra:
            record.append(mask)
        if extra:
            record.append(extra)
        return record


def decode_row(payload, record):
    """Reference decoder for validation/tools; viewer decodes lazily in JS."""
    if payload.get('format') != FORMAT:
        return record
    user = payload['users'][record[1]] if type(record[1]) is int else record[1]
    badges = payload['badges'][record[4]] if type(record[4]) is int else record[4][0]
    mask = record[5] if len(record) > 5 else 63
    values = (record[0], user[0], record[2], record[3], user[1], badges)
    result = dict(record[6]) if len(record) > 6 else {}
    result.update((key, values[i]) for i, key in enumerate(FIELDS) if mask & (1 << i))
    return result
