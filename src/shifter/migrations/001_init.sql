-- All timestamps are ISO 8601 strings with offset, e.g. "2026-05-04T08:02:11+10:00".
-- All dates are ISO 8601 dates, e.g. "2026-05-04".
-- All wall-clock times are 'HH:MM' (24h, local).
-- Money is stored as INTEGER cents.

CREATE TABLE nannies (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE pay_rates (
    id              INTEGER PRIMARY KEY,
    nanny_id        INTEGER NOT NULL REFERENCES nannies(id) ON DELETE CASCADE,
    rate_cents      INTEGER NOT NULL CHECK (rate_cents >= 0),
    effective_from  TEXT NOT NULL,             -- DATE
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (nanny_id, effective_from)
);

CREATE INDEX pay_rates_lookup ON pay_rates (nanny_id, effective_from DESC);

CREATE TABLE shifts (
    id                   INTEGER PRIMARY KEY,
    nanny_id             INTEGER NOT NULL REFERENCES nannies(id),
    start_time           TEXT NOT NULL,         -- ISO8601 with offset
    end_time             TEXT,                  -- NULL while shift is open
    rate_override_cents  INTEGER CHECK (rate_override_cents IS NULL OR rate_override_cents >= 0),
    flat_rate_cents      INTEGER CHECK (flat_rate_cents IS NULL OR flat_rate_cents >= 0),
    notes                TEXT,
    source               TEXT NOT NULL CHECK (source IN ('manual', 'ha', 'imported')),
    confirmed            INTEGER NOT NULL DEFAULT 1,    -- 0 = needs review (HA-sourced)
    paid_on              TEXT,                            -- DATE; NULL = unpaid
    paid_amount_cents    INTEGER,                         -- NULL = use computed
    paid_note            TEXT,
    timetagger_key       TEXT UNIQUE,                     -- idempotent re-import
    created_by           TEXT,
    updated_by           TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX shifts_nanny_start ON shifts (nanny_id, start_time);
CREATE INDEX shifts_unpaid      ON shifts (nanny_id) WHERE paid_on IS NULL;
CREATE INDEX shifts_open        ON shifts (nanny_id) WHERE end_time IS NULL;
CREATE INDEX shifts_unconfirmed ON shifts (nanny_id) WHERE confirmed = 0;

CREATE TABLE expenses (
    id              INTEGER PRIMARY KEY,
    shift_id        INTEGER NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
    amount_cents    INTEGER NOT NULL CHECK (amount_cents >= 0),
    description     TEXT NOT NULL,
    paid_on         TEXT,                                 -- DATE; NULL = unpaid
    pending_review  INTEGER NOT NULL DEFAULT 0,           -- 1 = auto-extracted from import
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX expenses_shift  ON expenses (shift_id);
CREATE INDEX expenses_unpaid ON expenses (shift_id) WHERE paid_on IS NULL;

-- Recurring weekly schedule (nanny works day_of_week from active_from to active_until).
CREATE TABLE schedule_patterns (
    id            INTEGER PRIMARY KEY,
    nanny_id      INTEGER NOT NULL REFERENCES nannies(id) ON DELETE CASCADE,
    day_of_week   INTEGER NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),  -- 0=Mon..6=Sun
    start_time    TEXT NOT NULL,                         -- 'HH:MM'
    end_time      TEXT NOT NULL,
    active_from   TEXT NOT NULL,                         -- DATE
    active_until  TEXT,                                   -- DATE; NULL = open-ended
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX schedule_patterns_nanny ON schedule_patterns (nanny_id, day_of_week);

-- Materialised expected shifts. Pattern-generated rows + manual one-offs.
-- Manual rows take precedence (e.g. cancellations recorded as cancelled=1 row).
CREATE TABLE expected_shifts (
    id          INTEGER PRIMARY KEY,
    nanny_id    INTEGER NOT NULL REFERENCES nannies(id) ON DELETE CASCADE,
    date        TEXT NOT NULL,                            -- DATE
    start_time  TEXT NOT NULL,                            -- 'HH:MM'
    end_time    TEXT NOT NULL,
    pattern_id  INTEGER REFERENCES schedule_patterns(id) ON DELETE SET NULL,
    source      TEXT NOT NULL CHECK (source IN ('pattern', 'manual')),
    cancelled   INTEGER NOT NULL DEFAULT 0,
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (nanny_id, date, start_time)
);

CREATE INDEX expected_shifts_date ON expected_shifts (date) WHERE cancelled = 0;

-- All HA events received. Events arrive unattributed (no nanny info from HA).
-- Resolution decides which nanny + which actual shift the event belongs to.
CREATE TABLE ha_events (
    id                   INTEGER PRIMARY KEY,
    occurred_at          TEXT NOT NULL,                   -- ISO8601 with offset
    source               TEXT,                            -- e.g. 'frigate-front-door'
    event_type_hint      TEXT CHECK (event_type_hint IS NULL OR event_type_hint IN ('arrival','departure')),
    nanny_id             INTEGER REFERENCES nannies(id) ON DELETE SET NULL,
    shift_id             INTEGER REFERENCES shifts(id)   ON DELETE SET NULL,
    expected_shift_id    INTEGER REFERENCES expected_shifts(id) ON DELETE SET NULL,
    resolution           TEXT NOT NULL CHECK (resolution IN ('arrival','departure','unresolved','ignored')),
    resolution_note      TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX ha_events_occurred  ON ha_events (occurred_at DESC);
CREATE INDEX ha_events_unresolved ON ha_events (occurred_at DESC) WHERE resolution = 'unresolved';

CREATE TABLE screenshots (
    id            INTEGER PRIMARY KEY,
    ha_event_id   INTEGER NOT NULL REFERENCES ha_events(id) ON DELETE CASCADE,
    filename      TEXT,                                   -- relative to SCREENSHOT_DIR; NULL = pruned
    content_type  TEXT,
    size_bytes    INTEGER,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX screenshots_event ON screenshots (ha_event_id);
