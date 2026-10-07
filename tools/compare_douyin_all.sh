#!/usr/bin/env bash
# Orchestrates all 4 runs: {sonnet5, gemini38} x {t1_blacklist, t5_dm}, serial.
# Writes a tiny status trail to _runs_compare_status.log so an external watcher
# can tell what's running without tailing each per-run log.
set -euo pipefail
cd "$(dirname "$0")/.."

STATUS=_runs_compare_status.log
: > "$STATUS"
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$STATUS"; }

log "comparison start: 4 runs, serial"

RUNS=(
  "gemini38 1"
  "sonnet5 1"
)

for entry in "${RUNS[@]}"; do
  log "START: $entry"
  if tools/compare_douyin.sh $entry; then
    log "DONE : $entry"
  else
    log "FAIL : $entry (rc=$?) — continuing to next run"
  fi
  # Short cool-down so the UI/Chrome has time to settle before the next run.
  sleep 10
done

log "comparison complete"
