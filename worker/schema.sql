-- D1 schema for the phantom-click Gemini bridge Worker.
-- Apply with: wrangler d1 execute phantom-click-tokens --remote --file=schema.sql
--
-- One table. The Worker performs all writes via a single atomic UPDATE
-- statement (see src/index.ts); no app-side locking needed because SQLite
-- serializes writes per database.

CREATE TABLE IF NOT EXISTS tokens (
    token              TEXT PRIMARY KEY,
    label              TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    -- NULL or 0 = unlimited; positive int = hard daily cap.
    max_calls_per_day  INTEGER,
    calls_today        INTEGER NOT NULL DEFAULT 0,
    calls_total        INTEGER NOT NULL DEFAULT 0,
    -- ISO date (YYYY-MM-DD) the counter belongs to. NULL until first call.
    day                TEXT,
    last_call_at       TEXT
);

CREATE INDEX IF NOT EXISTS tokens_label_idx ON tokens(label);
