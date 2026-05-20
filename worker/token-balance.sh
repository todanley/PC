#!/bin/bash
# Show a token's prepaid wallet state (read-only).
#
# Usage:
#   ./worker/token-balance.sh <pc_token>
#
# Prints balance / spent (in dollars) and lifetime call count. Needs wrangler
# auth (same as deploy.sh / issue-token.sh).

set -euo pipefail

cd "$(dirname "$0")"

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <pc_token>" >&2
    exit 2
fi

TOKEN="$1"
DB_NAME="phantom-click-tokens"

if [[ ! "$TOKEN" =~ ^pc_[A-Za-z0-9]+$ ]]; then
    echo "error: token has unexpected chars" >&2
    exit 1
fi

SQL="SELECT token, tier, label, balance_uusd, spent_uusd, calls_total, last_call_at FROM tokens WHERE token = '$TOKEN';"

# --json gives structured output we can reformat micro-USD -> dollars.
npx --yes wrangler d1 execute "$DB_NAME" --remote --json --command="$SQL" \
    | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print("could not parse wrangler output", file=sys.stderr); sys.exit(1)
rows = []
for blk in (data if isinstance(data, list) else [data]):
    rows += (blk.get("results") or [])
if not rows:
    print("no such token"); sys.exit(0)
r = rows[0]
print(f"token       : {r[\"token\"]}")
print(f"tier        : {r.get(\"tier\")}")
print(f"label       : {r.get(\"label\")}")
print(f"balance     : ${(r.get(\"balance_uusd\") or 0)/1e6:.4f}")
print(f"spent       : ${(r.get(\"spent_uusd\") or 0)/1e6:.4f}")
print(f"calls_total : {r.get(\"calls_total\")}")
print(f"last_call_at: {r.get(\"last_call_at\")}")
'
