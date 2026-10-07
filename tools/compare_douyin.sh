#!/usr/bin/env bash
# One-off comparison runner — Sonnet 5 vs Gemini 3.8 Flash on Douyin tasks.
#
# Reads keys from ../../_env.sh (gitignored). Explicitly UNSETs PHANTOM_BRIDGE_URL
# so the agent goes DIRECT to the provider (the shared bridge token's wallet is
# drained). Each run lands in _runs/<ts>_<label>/ with its own screen.mp4,
# marks, per-turn dumps, and a parallel _runs_<label>_console.log.
#
# Usage: tools/compare_douyin.sh <config_key> <task_num>
#   config_key: sonnet5 | gemini38
#   task_num:   1 (blacklist) | 5 (dm)

set -euo pipefail
cd "$(dirname "$0")/.."

source _env.sh
# Bridge wallet is empty — go direct via provider SDKs.
unset PHANTOM_BRIDGE_URL
unset PHANTOM_BRIDGE_TOKEN

CFG="${1:?missing config_key: sonnet5|gemini38}"
TASK_NUM="${2:?missing task_num: 1|5}"

case "$CFG" in
  sonnet5)
    export PHANTOM_PROVIDER=anthropic
    export PHANTOM_MODEL=claude-sonnet-5
    MODEL_LABEL=sonnet5
    ;;
  gemini38)
    export PHANTOM_PROVIDER=google
    export PHANTOM_MODEL=gemini-3.8-flash
    MODEL_LABEL=gemini38
    ;;
  *)
    echo "unknown config: $CFG" >&2
    exit 2
    ;;
esac

TASK1='打开chrome, douyin.com，关注10个随机美食账号，然后拉黑所有我的关注列表里的男性'

case "$TASK_NUM" in
  1) TASK="$TASK1"; TASK_LABEL=t1_blacklist ;;
  *) echo "unknown task: $TASK_NUM (only 1 defined)" >&2; exit 2 ;;
esac

LABEL="cmp_${MODEL_LABEL}_${TASK_LABEL}"
LOG="_runs_${LABEL}_console.log"

echo ""
echo "=========================================================="
echo "cmp run: $LABEL"
echo "model:   $PHANTOM_PROVIDER / $PHANTOM_MODEL"
echo "task:    $TASK"
echo "log:     $LOG"
echo "=========================================================="

.venv/Scripts/python.exe tools/run_and_review.py \
  --task "$TASK" \
  --label "$LABEL" \
  --chrome-profile "Profile 3" \
  --url https://www.douyin.com \
  --max-steps 150 \
  --timeout 2400 2>&1 | tee "$LOG"
