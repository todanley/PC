#!/usr/bin/env bash
# One-off: run the new TASK 5 (Douyin search + comment top 10 videos) with
# Gemini 3.8 Flash via direct Google AI Studio API (bridge wallet drained).
# Pre-launches Chrome Profile 3 (stanley) at douyin.com so the agent inherits
# the logged-in session instead of launching Chrome itself.
set -euo pipefail
cd "$(dirname "$0")/.."

source _env.sh
unset PHANTOM_BRIDGE_URL
unset PHANTOM_BRIDGE_TOKEN

export PHANTOM_PROVIDER=google
export PHANTOM_MODEL=gemini-3.8-flash

TASK="打开chrome, douyin.com然后搜索爹系男友，再在前十个视频评论区评论'181 A8见面爆金币'"
LABEL=cmp_gemini38_t5_dycomment
LOG="_runs_${LABEL}_console.log"

echo ""
echo "=========================================================="
echo "cmp run: $LABEL"
echo "model:   $PHANTOM_PROVIDER / $PHANTOM_MODEL"
echo "task:    $TASK"
echo "log:     $LOG"
echo "budget:  150 steps / 3600 s (1h)"
echo "=========================================================="

# Cross-platform venv python: Mac/Linux at .venv/bin/python, Windows at .venv/Scripts/python.exe
PY=.venv/bin/python; [ -x "$PY" ] || PY=.venv/Scripts/python.exe
"$PY" tools/run_and_review.py \
  --task "$TASK" \
  --label "$LABEL" \
  --chrome-profile "Profile 3" \
  --url https://www.douyin.com \
  --max-steps 150 \
  --timeout 3600 2>&1 | tee "$LOG"
