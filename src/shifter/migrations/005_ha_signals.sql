-- ha_signals: raw HA signals replacing ha_events for the new architecture.
-- Each signal is one atomic observation from HA (keypad, PIR, Frigate, presence).
-- Arrival/departure resolution is done lazily in Python; snapshot_path is
-- filled after the HA screenshot upload (a second HTTP call).

CREATE TABLE IF NOT EXISTS ha_signals (
    id               INTEGER PRIMARY KEY,
    occurred_at      TEXT    NOT NULL,            -- ISO8601 with offset
    source           TEXT    NOT NULL,            -- 'rosslare' | 'app' | 'entry_pir' | ...
    signal           TEXT    NOT NULL,            -- see VALID_SIGNALS in ha_signals.py
    person           TEXT,                        -- set for homeowner_home / homeowner_away
    nanny_id         INTEGER REFERENCES nannies(id) ON DELETE SET NULL,
    shift_id         INTEGER REFERENCES shifts(id) ON DELETE SET NULL,
    resolution       TEXT    NOT NULL DEFAULT 'recorded'
                     CHECK (resolution IN ('arrival','departure','recorded','ignored')),
    resolution_note  TEXT,
    snapshot_path    TEXT,                        -- relative path; set after screenshot upload
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS ix_ha_signals_occurred ON ha_signals (occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_ha_signals_signal   ON ha_signals (signal);
CREATE INDEX IF NOT EXISTS ix_ha_signals_shift    ON ha_signals (shift_id) WHERE shift_id IS NOT NULL;
