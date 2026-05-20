#!/bin/bash
# Mint a new prepaid wallet token in the Worker's D1 database.
#
# Usage:
#   ./worker/issue-token.sh <tier> [label]
#
#   <tier>   paid  -> starts with $5.00 of quota
#            test  -> starts with $0.10 of quota (runs out within a test session)
#   [label]  optional human note (defaults to the tier name)
#
# Example:
#   ./worker/issue-token.sh paid alice
#   ./worker/issue-token.sh test
#
# Prints the token to stdout — hand it to the buyer to paste into the app.
#
# Margin: balance is the FACE VALUE the user buys (e.g. $5). The Worker deducts
# real Gemini cost x MARKUP (2.0 by default), so a $5 token covers ~$2.50 of
# real Gemini and you keep ~50%. Sell at your price (e.g. AUD$5/$10).
#
# Token format: `pc_` + 32 url-safe random chars.

set -euo pipefail

cd "$(dirname "$0")"

# Tier -> starting balance in micro-USD (integer). Tune here.
PAID_UUSD=5000000   # $5.00
TEST_UUSD=100000    # $0.10

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <paid|test> [label]" >&2
    exit 2
fi

TIER="$1"
case "$TIER" in
    paid) BALANCE_UUSD=$PAID_UUSD ;;
    test) BALANCE_UUSD=$TEST_UUSD ;;
    *) echo "error: tier must be 'paid' or 'test' (got '$TIER')" >&2; exit 2 ;;
esac
LABEL="${2:-$TIER}"
DB_NAME="phantom-click-tokens"

# Generate random token. openssl is available on every macOS by default.
TOKEN="pc_$(openssl rand -base64 32 | tr -d '=+/' | head -c 32)"
NOW=$(date -u +"%Y-%m-%dT%H:%M:%S+00:00")

# D1 CLI doesn't expose bind params, so strings are inlined. Token is
# alphanumeric (guarded below); label single-quotes are SQL-escaped.
if [[ ! "$TOKEN" =~ ^pc_[A-Za-z0-9]+$ ]]; then
    echo "error: generated token has unexpected chars" >&2
    exit 1
fi
LABEL_ESC=$(printf '%s' "$LABEL" | sed "s/'/''/g")

SQL="INSERT INTO tokens (token, label, created_at, balance_uusd, spent_uusd, tier, calls_total) VALUES ('$TOKEN', '$LABEL_ESC', '$NOW', $BALANCE_UUSD, 0, '$TIER', 0);"

npx --yes wrangler d1 execute "$DB_NAME" --remote --command="$SQL" > /tmp/issue_token.log 2>&1 || {
    echo "error: d1 execute failed:" >&2
    cat /tmp/issue_token.log >&2
    exit 1
}

# Token is the only thing on stdout — easy to capture in $(…).
echo "$TOKEN"
