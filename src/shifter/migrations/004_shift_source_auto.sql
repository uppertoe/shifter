-- Add 'auto' to shifts.source for shifts opened by the schedule-driven
-- auto-opener (a background task that opens a shift at its scheduled start
-- time when HA hasn't already done so). The auto-opener uses created_by
-- 'auto-opener' and leaves confirmed=0, mirroring HA-sourced shifts so they
-- surface for human review.
--
-- SQLite can't ALTER a CHECK constraint, so this rebuilds the table.

PRAGMA foreign_keys = OFF;

CREATE TABLE shifts_new (
    id                   INTEGER PRIMARY KEY,
    nanny_id             INTEGER NOT NULL REFERENCES nannies(id),
    start_time           TEXT NOT NULL,
    end_time             TEXT,
    rate_override_cents  INTEGER CHECK (rate_override_cents IS NULL OR rate_override_cents >= 0),
    flat_rate_cents      INTEGER CHECK (flat_rate_cents IS NULL OR flat_rate_cents >= 0),
    notes                TEXT,
    source               TEXT NOT NULL CHECK (source IN ('manual', 'ha', 'imported', 'auto')),
    confirmed            INTEGER NOT NULL DEFAULT 1,
    paid_on              TEXT,
    paid_amount_cents    INTEGER,
    paid_note            TEXT,
    timetagger_key       TEXT UNIQUE,
    created_by           TEXT,
    updated_by           TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO shifts_new SELECT * FROM shifts;
DROP TABLE shifts;
ALTER TABLE shifts_new RENAME TO shifts;

CREATE INDEX shifts_nanny_start ON shifts (nanny_id, start_time);
CREATE INDEX shifts_unpaid      ON shifts (nanny_id) WHERE paid_on IS NULL;
CREATE INDEX shifts_open        ON shifts (nanny_id) WHERE end_time IS NULL;
CREATE INDEX shifts_unconfirmed ON shifts (nanny_id) WHERE confirmed = 0;

PRAGMA foreign_keys = ON;
