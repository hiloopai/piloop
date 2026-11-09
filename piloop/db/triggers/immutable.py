"""Rows that never change: events are only ever appended.

The authorizer of ``piloop sql`` (``piloop.db.sql``) denies an UPDATE or a DELETE of these tables. TEMP triggers
(``EVENTS``) refuse an insert of any id that is not the next.
"""

from piloop.db.triggers.ddl import Trigger

APPEND_ONLY = ('events',)
"""Tables that a client only appends to: no UPDATE, no DELETE, and each new row takes the next id (``EVENTS``).

An event id is a reader's cursor (``piloop.reports.feed``). SQLite reuses the ids of deleted newest rows, an UPDATE can
move an id, and an explicit id can land below a cursor: a reader past it would miss the event."""


NEXT_ID = "SELECT RAISE(ABORT, 'events take the next id: leave id out')"
EVENTS = (
    # An id left out reads -1 in a BEFORE trigger, like an explicit -1, which the AFTER trigger refuses
    Trigger(
        'events_checked',
        'events',
        'BEFORE INSERT',
        body=';\n'.join(
            [
                f'{NEXT_ID} WHERE NEW.id != -1',
                "SELECT RAISE(ABORT, 'events.actor is text') WHERE typeof(NEW.actor) != 'text'",
                "SELECT RAISE(ABORT, 'events.target_id is an integer') WHERE typeof(NEW.target_id) != 'integer'",
                "SELECT RAISE(ABORT, 'events.at is a time in text, like 2026-10-20 08:00:00')"
                " WHERE typeof(NEW.at) != 'text' OR julianday(NEW.at) IS NULL",
                "SELECT RAISE(ABORT, 'events.payload is JSON text, not a blob') WHERE typeof(NEW.payload) = 'blob'",
                "SELECT RAISE(ABORT, 'events.payload is a JSON object with its kind in $.kind')"
                " WHERE json_type(NEW.payload, '$.kind') IS NOT 'text'",
            ]
        ),
        temp=True,
    ),
    Trigger('events_next_id', 'events', 'AFTER INSERT', when='NEW.id < 1', body=NEXT_ID, temp=True),
)
"""TEMP: an event takes the next id, and its columns have the types that a reader of the feed reads.

Every piloop connection has them. The payload is an object with its kind. SQLite reads JSON5, so a payload may still
hold ``NaN``, which the reader of the feed shows as stored."""
