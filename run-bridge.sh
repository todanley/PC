#!/bin/bash
# Start the phantom-click Gemini bridge + Cloudflare tunnel.
#
# Two modes, auto-detected:
#   • named tunnel (stable URL): if bridge/cloudflared.yml exists. Set it up
#     once with `./bridge/setup-tunnel.sh <name> <hostname>` — after that
#     every restart reuses the same public hostname.
#   • quick tunnel (ephemeral URL): fallback. Gives a fresh *.trycloudflare.com
#     URL each run. Fine for testing; no DNS/CF account needed.
#
# Prereqs:
#   - GEMINI_API_KEY exported
#   - cloudflared installed: `brew install cloudflared`
#   - bridge deps installed: `pip install -r requirements-bridge.txt`
#
# Stop with Ctrl-C — both processes are killed on exit.

set -euo pipefail

cd "$(dirname "$0")"

if [[ -z "${GEMINI_API_KEY:-}" ]]; then
    echo "error: GEMINI_API_KEY not set" >&2
    exit 1
fi

if ! command -v cloudflared > /dev/null; then
    echo "error: cloudflared not installed. run: brew install cloudflared" >&2
    exit 1
fi

PORT="${BRIDGE_PORT:-8787}"
LOGDIR="bridge"
UVICORN_LOG="$LOGDIR/uvicorn.log"
TUNNEL_LOG="$LOGDIR/cloudflared.log"
NAMED_CFG="$LOGDIR/cloudflared.yml"

mkdir -p "$LOGDIR"
: > "$UVICORN_LOG"
: > "$TUNNEL_LOG"

cleanup() {
    echo
    echo "shutting down…"
    [[ -n "${UVICORN_PID:-}" ]] && kill "$UVICORN_PID" 2>/dev/null || true
    [[ -n "${TUNNEL_PID:-}" ]] && kill "$TUNNEL_PID" 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# 1. uvicorn bound to loopback. Cloudflared pulls from here; no LAN exposure.
echo "starting uvicorn on 127.0.0.1:$PORT …"
python3 -m uvicorn bridge.server:app \
    --host 127.0.0.1 --port "$PORT" --log-level info \
    > "$UVICORN_LOG" 2>&1 &
UVICORN_PID=$!

# Wait for /healthz so cloudflared doesn't open a tunnel to a dead port.
for i in {1..30}; do
    if curl -fsS "http://127.0.0.1:$PORT/healthz" > /dev/null 2>&1; then
        echo "uvicorn ready (pid $UVICORN_PID)"
        break
    fi
    sleep 0.5
    if ! kill -0 "$UVICORN_PID" 2>/dev/null; then
        echo "error: uvicorn died before becoming ready. see $UVICORN_LOG" >&2
        tail -n 20 "$UVICORN_LOG" >&2
        exit 1
    fi
done

# 2. Tunnel — named mode if config present, otherwise quick.
PUBLIC_URL=""
if [[ -f "$NAMED_CFG" ]]; then
    HOSTNAME=$(awk '/^[[:space:]]*-?[[:space:]]*hostname:/{print $NF; exit}' "$NAMED_CFG")
    if [[ -z "$HOSTNAME" ]]; then
        echo "error: couldn't parse hostname from $NAMED_CFG" >&2
        exit 1
    fi
    PUBLIC_URL="https://$HOSTNAME"
    echo "starting named cloudflared tunnel ($HOSTNAME) …"
    cloudflared tunnel --no-autoupdate --config "$NAMED_CFG" run \
        > "$TUNNEL_LOG" 2>&1 &
    TUNNEL_PID=$!
    # Named tunnels don't print a URL; we already know it. Wait for the
    # "Registered tunnel connection" line as a readiness signal.
    for i in {1..60}; do
        if grep -q "Registered tunnel connection" "$TUNNEL_LOG" 2>/dev/null; then
            break
        fi
        sleep 0.5
        if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
            echo "error: cloudflared died. see $TUNNEL_LOG" >&2
            tail -n 20 "$TUNNEL_LOG" >&2
            exit 1
        fi
    done
else
    echo "starting cloudflared quick tunnel …"
    cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" \
        > "$TUNNEL_LOG" 2>&1 &
    TUNNEL_PID=$!
    # cloudflared prints the public URL a few seconds in. Poll for it.
    for i in {1..60}; do
        PUBLIC_URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -n 1 || true)
        if [[ -n "$PUBLIC_URL" ]]; then
            break
        fi
        sleep 0.5
        if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
            echo "error: cloudflared died. see $TUNNEL_LOG" >&2
            tail -n 20 "$TUNNEL_LOG" >&2
            exit 1
        fi
    done
fi

if [[ -z "$PUBLIC_URL" ]]; then
    echo "error: timed out waiting for tunnel. see $TUNNEL_LOG" >&2
    exit 1
fi

MODE="quick (ephemeral)"
[[ -f "$NAMED_CFG" ]] && MODE="named (stable)"

cat <<EOF

=============================================================
  phantom-click Gemini bridge is LIVE  [$MODE]
=============================================================
  public URL : $PUBLIC_URL
  local      : http://127.0.0.1:$PORT
  uvicorn log: $UVICORN_LOG
  tunnel log : $TUNNEL_LOG
  access log : $LOGDIR/access.log

  Mint a token:  python3 -m bridge.issue_token <label> [--max-per-day N]

  CN-client env vars:
    PHANTOM_PROVIDER=google
    PHANTOM_API_BASE=$PUBLIC_URL
    GEMINI_API_KEY=<bridge token>
    PHANTOM_MODEL=gemini-3-pro-preview

  Ctrl-C to stop both processes.
=============================================================
EOF

# Park until either child exits. macOS ships bash 3.2 which lacks `wait -n`,
# so we poll. 2s ticks: light on CPU, fast enough to notice a crash.
while kill -0 "$UVICORN_PID" 2>/dev/null && kill -0 "$TUNNEL_PID" 2>/dev/null; do
    sleep 2
done
