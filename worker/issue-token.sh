#!/bin/bash
# Mint a new bearer token in the Worker's D1 database.
#
# Usage:
#   ./worker/issue-token.sh <label> [max_per_day]
#
# Example:
#   ./worker/issue-token.sh public-pool 5000
#
# Prints the token to stdout — copy it into the CN-client build
# (PHANTOM_BRIDGE_TOKEN env when running build-mac.sh).
#
# Token format: `pc_` + 32 url-safe random chars. Same shape as the legacy
# `python -m bridge.issue_token` so the Worker accepts existing dev tokens
# if you ever migrate the DB by hand.

set -euo pipefail

cd "$(dirname "$0")"

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <label> [max_per_day]" >&2
    echo "  max_per_day defaults to 500; 0 means unlimited" >&2
    exit 2
fi

LABEL="$1"
MAX_PER_DAY="${2:-500}"
DB_NAME="phantom-click-tokens"

# Generate random token. openssl is available on every macOS by default.
TOKEN="pc_$(openssl rand -base64 32 | tr -d '=+/' | head -c 32)"
NOW=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")
TODAY=$(date -u +"%Y-%m-%d")

# Use --command with parameter substitution by hand. D1 CLI doesn't expose
# bind params, so we have to quote the strings — they're alphanumeric only so
# no SQL-injection risk here. But run a regex check anyway.
if [[ ! "$TOKEN" =~ ^pc_[A-Za-z0-9]+$ ]]; then
    echo "error: generated token has unexpected chars" >&2
    exit 1
fi
# Label could contain anything — escape single quotes the SQL way (double them).
LABEL_ESC=$(printf '%s' "$LABEL" | sed "s/'/''/g")

SQL="INSERT INTO tokens (token, label, created_at, max_calls_per_day, calls_today, calls_total, day) VALUES ('$TOKEN', '$LABEL_ESC', '$NOW', $MAX_PER_DAY, 0, 0, '$TODAY');"

npx --yes wrangler d1 execute "$DB_NAME" --remote --command="$SQL" > /tmp/issue_token.log 2>&1 || {
    echo "error: d1 execute failed:" >&2
    cat /tmp/issue_token.log >&2
    exit 1
}

# Token is the only thing on stdout — easy to capture in $(…).
echo "$TOKEN"
