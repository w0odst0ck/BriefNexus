#!/usr/bin/env bash
# BriefNexus 采集平台 healthcheck — 每小时跑，失败飞书告警
# 用法: bash 脚本/briefnexus_healthcheck.sh
#
# 2026-10-14 加固：原版只探平台自身 /healthz，不探依赖与产出 →
# RSSHub(:1200) 挂掉 25 天、快照全 0 条，巡检照样每小时报 ok。
# 现分三层：① 平台自身 ② 依赖可达 ③ 产出非空 + 新鲜度 + 源级健康。
set -uo pipefail

API="http://127.0.0.1:9000/healthz"
RSSHUB="http://127.0.0.1:1200/"
SNAPSHOT_DIR="/home/l/.openclaw/workspace/projects/research-collectors/data"
LOG=/tmp/briefnexus-health.log
FAILS=()

# 整数校验：非整数返回 1（供数值比较前把关，避免 test 语法错误被静默吞掉）
is_int() { case "${1:-}" in ''|*[!0-9]*) return 1 ;; esac; return 0; }

resp=$(curl -s -m 10 "$API" 2>/dev/null)
code=$?
echo "$(date '+%F %T') | exit=$code | $resp" >> "$LOG"

# ── ① 平台自身 ──
if [ $code -ne 0 ] || ! echo "$resp" | grep -q '"status":"ok"'; then
  FAILS+=("平台自身 /healthz 异常: $resp (exit=$code)")
fi

# ── ② 依赖可达（RSSHub 被 research-collectors 采集器依赖）──
# 探 :1200 根路径本身；不跟重定向（-L 会把「跳转到 200 的错误页」误判为健康）
# 状态码语义（仅文案区分，均计为一次失败并告警）：
#   200        = 健康
#   empty/000  = 不可用（连接失败/超时/curl 缺失）
#   429/403    = 受限（限流/WAF，根因可能在代理）
#   其他非 200 = 可达但异常
rh_code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$RSSHUB" 2>/dev/null)
if [ -z "$rh_code" ] || [ "$rh_code" = "000" ]; then
  FAILS+=("依赖 RSSHub :1200 不可达 (连接失败/超时/curl 异常)")
elif [ "$rh_code" = "429" ] || [ "$rh_code" = "403" ]; then
  FAILS+=("依赖 RSSHub :1200 受限（HTTP $rh_code，疑似限流/WAF；检查代理 :7890）")
elif [ "$rh_code" != "200" ]; then
  FAILS+=("依赖 RSSHub :1200 可达但返回 HTTP $rh_code")
fi

# ── ③ 产出非空 + 新鲜度 + 源级健康（最近一次快照）──
# 目录名约定 YYYY-MM-DD（零填充），LC_ALL=C 固定排序（不依赖调用者 locale）
latest=$(LC_ALL=C ls -d "$SNAPSHOT_DIR"/[0-9][0-9][0-9][0-9]-*/ 2>/dev/null | LC_ALL=C sort | tail -1)
if [ -z "$latest" ]; then
  FAILS+=("快照目录不存在或为空（$SNAPSHOT_DIR）— 采集链路从未产出")
elif [ ! -f "${latest}manifest.jsonl" ]; then
  # 目录存在但 manifest 缺失 = 那次运行中途崩溃，不得静默放过
  FAILS+=("最新快照 $(basename "$latest") 缺 manifest.jsonl（运行中途崩溃？）")
else
  snap_day=$(basename "$latest")

  # 新鲜度：快照每日 18:30 产出，最新目录日期不应早于昨天零点
  # （不用 date -d yesterday：那是 now-24h，会把昨天的快照误判为陈旧）
  snap_ts=$(date -d "$snap_day" +%s 2>/dev/null)
  anchor=$(date -d 'yesterday 00:00' +%s 2>/dev/null)
  if [ -z "$snap_ts" ]; then
    # 目录名无法解析为日期 = 命名约定被破坏，属独立故障类，不冒充「不新鲜」
    FAILS+=("最新快照目录名 $snap_day 无法解析为日期（命名约定被破坏？）")
  elif [ -z "$anchor" ]; then
    # 阈值算不出来就不能静默跳过（那正是本次加固要消灭的盲区）
    FAILS+=("无法计算新鲜度阈值（date 异常），新鲜度校验未执行")
  elif [ "$snap_ts" -lt "$anchor" ]; then
    FAILS+=("快照不新鲜：最新为 $snap_day（预期至少到昨天）— 定时任务或依赖可能已停")
  fi

  # schema 对齐 producer（research-collectors/pull.py write_manifest）：{source, date, count, status}
  # 用临时文件承接 python 输出，以捕获其真实退出码
  # （process substitution 的 `$?` 只反映 read，拿不到 python 状态）
  stats_file=$(mktemp) || stats_file=""
  py_rc=1
  if [ -n "$stats_file" ]; then
    python3 -c "
import json,sys
bad=nbad=0
t=0
for line in open(sys.argv[1],encoding='utf-8'):
    line=line.strip()
    if not line: continue
    try:
        obj=json.loads(line)
        if isinstance(obj, dict) and 'count' in obj:
            c=obj.get('count')
            # 非整数/负数 = 数据损坏，不得静默计入或拉低 total
            if isinstance(c,bool) or not isinstance(c,int) or c<0:
                bad+=1
            else:
                t+=c
                # 单源 status 非 ok = 该源采集失败，属断链信号
                if obj.get('status') != 'ok':
                    nbad+=1
        else:
            bad+=1
    except (ValueError, TypeError, AttributeError):
        bad+=1
print(t, bad, nbad)
" "${latest}manifest.jsonl" > "$stats_file" 2>/dev/null
    py_rc=$?
  fi
  total=""; bad=0; nbad=0
  if [ -n "$stats_file" ] && [ -s "$stats_file" ]; then
    read -r total bad nbad < "$stats_file"
  fi
  [ -n "$stats_file" ] && rm -f "$stats_file"
  if [ "$py_rc" -ne 0 ] || ! is_int "$total" || ! is_int "$bad" || ! is_int "$nbad"; then
    FAILS+=("最近快照 $snap_day manifest 读取/汇总失败 (python exit=$py_rc)")
  else
    if [ "$bad" -gt 0 ]; then
      FAILS+=("最近快照 $snap_day manifest 结构损坏 $bad 行（解析失败）")
    fi
    if [ "$nbad" -gt 0 ]; then
      FAILS+=("最近快照 $snap_day 有 $nbad 个源 status 非 ok（单源采集失败）")
    fi
    if [ "$total" -eq 0 ]; then
      FAILS+=("最近快照 $snap_day 产出 0 条（采集链路疑似断）")
    fi
  fi
fi

nfails=${#FAILS[@]}
if [ "$nfails" -gt 0 ]; then
  msg="⚠️ BriefNexus 采集链路 healthcheck 失败"
  for f in "${FAILS[@]}"; do msg="$msg
- $f"; done
  # JSON 安全转义（响应体含引号/反斜杠，裸插值会产出非法 JSON）
  # python3 缺失或损坏都不能丢掉告警本身 → 以"是否真产出"为准，降级为纯文本
  payload=$(python3 -c 'import json,sys; print(json.dumps({"text": sys.stdin.read()}, ensure_ascii=False))' <<< "$msg" 2>/dev/null)
  if [ -n "$payload" ]; then
    printf '%s\n' "$payload"
  else
    printf '%s\n' "$msg"
  fi
  # 告警结论也落入日志（best-effort：LOG 不可写也不能掩盖退出码）
  printf '%s\n' "$(date '+%F %T') | FAIL: ${FAILS[*]}" >> "$LOG" 2>/dev/null || true
  exit 1
fi

exit 0
