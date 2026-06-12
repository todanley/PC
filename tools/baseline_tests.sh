#!/usr/bin/env bash
# Baseline test suite for LuLuBot. Three end-to-end tasks that exercise
# the production paths the user cares about most:
#
#   1. Douyin social-graph: follow N + filter-by-attribute on a list
#   2. Gmail compose: drive a real auth'd web app
#   3. Flight search: navigate a complex external site (Trip / Ctrip / Skyscanner)
#
# Each task is run via tools/run_and_review.py with the project bridge
# token and a 1-hour wall clock + 250-step ceiling. Recordings, marks,
# and turn dumps land in _runs/<ts>_<label>/ as usual.
#
# Usage:
#   tools/baseline_tests.sh all          # run all three sequentially
#   tools/baseline_tests.sh 1            # run douyin follow+blacklist only
#   tools/baseline_tests.sh 2            # run gmail self-send only
#   tools/baseline_tests.sh 3            # run flight-price search only
set -euo pipefail
TARGET="${1:-all}"

PHANTOM_BRIDGE_URL="${PHANTOM_BRIDGE_URL:-https://bridge.z1nexusn1.org}"
PHANTOM_BRIDGE_TOKEN="${PHANTOM_BRIDGE_TOKEN:-pc_GjlEQtZ51WCQC3X2q0FsRpU2MieKY5Nm}"
export PHANTOM_BRIDGE_URL PHANTOM_BRIDGE_TOKEN

TASK1='打开chrome, douyin.com，关注10个随机账号，然后拉黑所有我的关注列表里的男性'
TASK2='打开chrome, 使用我的gmail发送三封随机内容测试邮件到这个gmail邮箱自己'
TASK3='打开chrome，查询悉尼到广州一个月内最低的机票票价'

run_one() {
  local label="$1"; local task="$2"
  echo ""
  echo "=========================================================="
  echo "baseline: $label"
  echo "task: $task"
  echo "=========================================================="
  .venv/Scripts/python.exe tools/run_and_review.py \
    --task "$task" \
    --label "$label" \
    --max-steps 250 \
    --timeout 3600 2>&1 \
  | tee "_runs_baseline_${label}_console.log"
}

case "$TARGET" in
  1)    run_one baseline-1-douyin "$TASK1" ;;
  2)    run_one baseline-2-gmail  "$TASK2" ;;
  3)    run_one baseline-3-flight "$TASK3" ;;
  all)  run_one baseline-1-douyin "$TASK1"
        run_one baseline-2-gmail  "$TASK2"
        run_one baseline-3-flight "$TASK3" ;;
  *)    echo "usage: $0 {all|1|2|3}" >&2; exit 2 ;;
esac
