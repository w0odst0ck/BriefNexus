#!/usr/bin/env bash
# BriefNexus 采集平台 healthcheck — 每小时跑，失败飞书告警
# 用法: bash 脚本/briefnexus_healthcheck.sh
set -uo pipefail

API="http://127.0.0.1:9000/healthz"
LOG=/tmp/briefnexus-health.log

resp=$(curl -s -m 10 "$API" 2>/dev/null)
code=$?
echo "$(date '+%F %T') | exit=$code | $resp" >> "$LOG"

if [ $code -ne 0 ] || ! echo "$resp" | grep -q '"status":"ok"'; then
  echo "{\"text\":\"⚠️ BriefNexus 采集平台 healthcheck 失败: $resp (exit=$code)\"}"
  exit 1
fi
exit 0
