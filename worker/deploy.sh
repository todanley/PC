#!/bin/bash
# Deploy the phantom-click Gemini bridge Worker.
#
# Idempotent — re-running is safe. First run does the heavy lifting:
#   1. wrangler login (browser opens once)
#   2. Create D1 database (if missing)
#   3. Substitute the DB ID into wrangler.toml
#   4. Apply schema
#   5. Set GEMINI_API_KEY as a Worker secret (from your env)
#   6. wrangler deploy (binds custom route to bridge.z1nexusn1.org)
#
# Required env:
#   GEMINI_API_KEY  — passed to wrangler secret put on first run
#
# Output: prints the Worker URL when done.

set -euo pipefail

cd "$(dirname "$0")"

if [[ -z "${GEMINI_API_KEY:-}" ]]; then
    echo "error: GEMINI_API_KEY not set in env" >&2
    exit 1
fi

DB_NAME="phantom-click-tokens"

# --- 1. wrangler login (one-time) ---
# wrangler stores the auth at ~/.config/.wrangler/config or similar; the
# easiest reliable check is `wrangler whoami` exiting non-zero when logged out.
if ! npx --yes wrangler whoami > /dev/null 2>&1; then
    echo "→ wrangler not authorized; running login (browser will open) …"
    npx --yes wrangler login
fi
echo "→ logged in as: $(npx --yes wrangler whoami 2>&1 | grep -oE '[A-Za-z0-9._-]+@[A-Za-z0-9.-]+' | head -1 || echo '?')"

# --- 2. Materialize wrangler.toml from template (idempotent) ---
if [[ ! -f wrangler.toml ]]; then
    cp wrangler.toml.template wrangler.toml
    echo "→ created wrangler.toml from template"
fi

# --- 3. Create D1 if missing, capture its UUID ---
DB_ID=$(npx --yes wrangler d1 list --json 2>/dev/null \
    | python3 -c "
import json, sys
data = json.load(sys.stdin)
for db in (data if isinstance(data, list) else []):
    if db.get('name') == '$DB_NAME':
        print(db.get('uuid') or db.get('id') or '')
        break
" 2>/dev/null || true)

if [[ -z "$DB_ID" ]]; then
    echo "→ creating D1 database '$DB_NAME' …"
    CREATE_OUT=$(npx --yes wrangler d1 create "$DB_NAME" 2>&1)
    echo "$CREATE_OUT"
    DB_ID=$(echo "$CREATE_OUT" \
        | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' \
        | head -1)
    if [[ -z "$DB_ID" ]]; then
        echo "error: couldn't parse DB UUID from wrangler output" >&2
        exit 1
    fi
fi
echo "→ DB_ID = $DB_ID"

# --- 4. Substitute DB ID into wrangler.toml (idempotent) ---
if grep -q "@@DB_ID@@" wrangler.toml; then
    sed -i.bak "s|@@DB_ID@@|$DB_ID|" wrangler.toml
    rm -f wrangler.toml.bak
    echo "→ patched wrangler.toml with DB_ID"
fi

# --- 5. Apply schema (CREATE TABLE IF NOT EXISTS — re-running is safe) ---
echo "→ applying schema to D1 …"
npx --yes wrangler d1 execute "$DB_NAME" --remote --file=schema.sql

# --- 6. Set GEMINI_API_KEY secret (Worker-side, encrypted at rest) ---
# Piping into stdin so we don't have to deal with the interactive prompt.
echo "→ setting GEMINI_API_KEY as Worker secret …"
echo "$GEMINI_API_KEY" | npx --yes wrangler secret put GEMINI_API_KEY 2>&1 \
    | grep -vE "Enter a secret value|deprecated|wrangler is updated"|| true

# --- 7. Deploy ---
echo "→ deploying Worker …"
npx --yes wrangler deploy 2>&1 | tee /tmp/wrangler-deploy.log

WORKER_URL=$(grep -oE 'https://[a-z0-9.-]+workers\.dev' /tmp/wrangler-deploy.log | head -1)
CUSTOM_URL=$(grep -oE 'https://bridge\.z1nexusn1\.org[a-z/*]*' /tmp/wrangler-deploy.log | head -1)

cat <<EOF

=============================================================
  Worker deployed
=============================================================
  workers.dev : ${WORKER_URL:-(not parsed)}
  custom URL  : ${CUSTOM_URL:-https://bridge.z1nexusn1.org/*}

  Test:
    curl ${CUSTOM_URL:-https://bridge.z1nexusn1.org}/healthz
    -> {"ok":true,"worker":true}

  Mint a token:
    ./worker/issue-token.sh public-pool 5000

  Live logs:
    cd worker && npx wrangler tail
=============================================================
EOF
