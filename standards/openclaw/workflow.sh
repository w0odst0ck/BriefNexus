#!/usr/bin/env bash
# ── 行业标准采集 —— OpenClaw Shell 包装器 ────────────────
# 供 OpenClaw cron / systemEvent 调用
# 用法: bash workflow.sh [--verbose] [--format json|csv|md]
# ─────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OUTPUT_DIR="$PROJECT_ROOT/standards/output"
LOG_FILE="$OUTPUT_DIR/crawl.log"

mkdir -p "$OUTPUT_DIR"

cd "$PROJECT_ROOT"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始采集行业标准..." | tee -a "$LOG_FILE"

python3 -m standards.crawler.main "$@" 2>&1 | tee -a "$LOG_FILE"

EXIT_CODE=${PIPESTATUS[0]}

if [ $EXIT_CODE -eq 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 采集完成" | tee -a "$LOG_FILE"
    # 输出最新文件路径（供 OpenClaw 捕获）
    LATEST_JSON=$(ls -t "$OUTPUT_DIR"/standards_*.json 2>/dev/null | head -1)
    echo "LATEST_OUTPUT=$LATEST_JSON"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 采集失败 (exit=$EXIT_CODE)" | tee -a "$LOG_FILE"
fi

exit $EXIT_CODE
