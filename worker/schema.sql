-- D1 schema for the phantom-click Gemini bridge Worker.
-- Apply with: wrangler d1 execute phantom-click-tokens --remote --file=schema.sql
--
-- One table. The Worker performs all writes via a single atomic UPDATE
-- statement (see src/index.ts); no app-side locking needed because SQLite
-- serializes writes per database.

-- Each token is a prepaid WALLET: balance_uusd holds the remaining quota in
-- micro-USD (integer; never float). The Worker deducts the marked-up real
-- Gemini cost per call and refuses (HTTP 402) when balance_uusd <= 0.
-- The legacy call-cap columns (max_calls_per_day/calls_today/day) are kept for
-- backward compatibility but are no longer enforced.
CREATE TABLE IF NOT EXISTS tokens (
    token              TEXT PRIMARY KEY,
    label              TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    -- prepaid wallet (micro-USD): face value the user bought, drawn down to 0.
    balance_uusd       INTEGER NOT NULL DEFAULT 0,
    spent_uusd         INTEGER NOT NULL DEFAULT 0,
    tier               TEXT,                       -- 'paid' | 'test'
    -- legacy / unused (kept so old rows and tooling don't break):
    max_calls_per_day  INTEGER,
    calls_today        INTEGER NOT NULL DEFAULT 0,
    calls_total        INTEGER NOT NULL DEFAULT 0,
    day                TEXT,
    last_call_at       TEXT
);

CREATE INDEX IF NOT EXISTS tokens_label_idx ON tokens(label);

-- Migration for an ALREADY-deployed DB (SQLite: one ADD COLUMN per statement;
-- safe to re-run — it errors only if the column exists, which you can ignore):
--   wrangler d1 execute phantom-click-tokens --remote --command="ALTER TABLE tokens ADD COLUMN balance_uusd INTEGER NOT NULL DEFAULT 0;"
--   wrangler d1 execute phantom-click-tokens --remote --command="ALTER TABLE tokens ADD COLUMN spent_uusd   INTEGER NOT NULL DEFAULT 0;"
--   wrangler d1 execute phantom-click-tokens --remote --command="ALTER TABLE tokens ADD COLUMN tier         TEXT;"
