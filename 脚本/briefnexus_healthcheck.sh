#!/usr/bin/env bash
# BriefNexus 采集平台 healthcheck — 每小时跑，失败飞书告警
# 用法: bash 脚本/briefnexus_healthcheck.sh
#
# 2026-10-14 加固：原版只探平台自身 /healthz，不探依赖与产出 →
# RSSHub(:1200) 挂掉 25 天、快照全 0 条，巡检照样每小时报 ok。
# 现分三层：① 平台自身 ② 依赖可达 ③ 产出非空 + 新鲜度 + 源级健康。
#
# 2026-11 改造（配合 pull.py --mode / manifest 新语义「只写本次实跑源」+ mode 字段）：
#   1) 空 manifest（0 行）= 正常态：scheduled 白名单为空，记 INFO 不告警；
#      manifest 缺失仍告警（真崩溃）。
#   2) 源级统计只算 mode=scheduled，manual/archive 跳过（手动源没跑是正常态）；
#      mode 字段缺失视为 scheduled（兼容旧快照）。
#   3) 零产出源（status=ok 但 count=0）可见化 —— 不判失败，但日志 + 告警文案必须列出，
#      治 nvidia_blog 这类「接口静默返回空」的假绿灯。
#
# 2026-11-17 补白名单空期误报（唯一改动）：scheduled 白名单为空时 pull.py 不建日期目录、
#   不写 manifest，旧逻辑把「快照目录不存在或为空」判为致命 → 白名单空期每小时误报。
#   现先探测 sources.yaml 是否有 enabled 且 mode=scheduled 的源：
#     · 有 → 维持原判（真故障，不许放宽）
#     · 无（白名单空）→ 降为 NOTE（快照空 / 快照不新鲜均属预期），不进 FAILS、不改退出码
#     · sources.yaml 缺失/不可读/解析失败 → 探测输出非整数，按「有 scheduled 源」处理
#       （fail-closed：不许因配置不可读而静默放过真故障）
set -uo pipefail

API="http://127.0.0.1:9000/healthz"
RSSHUB="http://127.0.0.1:1200/"
SNAPSHOT_DIR="/home/l/.openclaw/workspace/projects/research-collectors/data"
SOURCES_YAML="/home/l/.openclaw/workspace/projects/research-collectors/sources.yaml"
LOG=/tmp/briefnexus-health.log
FAILS=()      # 致命项 → 飞书告警 + exit 1
NOTES=()      # 可见但非致命（零产出源 / 白名单空 / 部分源失败）→ 日志，告警时附在文案里

# 整数校验：非整数返回 1（供数值比较前把关，避免 test 语法错误被静默吞掉）
is_int() { case "${1:-}" in ''|*[!0-9]*) return 1 ;; esac; return 0; }

# scheduled 白名单探测：stdout 只输出整数（enabled 且 mode=scheduled 的源数）。
# 任何读取/解析异常 → 输出非整数 "err"，由调用方按「有 scheduled 源」保守处理。
# 判定口径对齐 pull.py：enabled 缺省为 true，mode 严格等于 scheduled（缺省 manual 不算）。
sched_count=$(python3 -c '
import sys

try:
    import yaml
    with open(sys.argv[1], encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    sources = doc.get("sources") if isinstance(doc, dict) else None
    if not isinstance(sources, dict):
        raise ValueError("sources 节点缺失或非 dict")
    n = 0
    for cfg in sources.values():
        if not isinstance(cfg, dict):
            continue  # 脏条目本身由采集侧报错，这里不计数
        if cfg.get("enabled", True) and cfg.get("mode") == "scheduled":
            n += 1
    print(n)
except Exception:
    print("err")
' "$SOURCES_YAML" 2>/dev/null)

# 白名单为空的唯一口径：探测成功且为 0。
# 非整数（python3/yaml 缺失、文件不存在或不可读、YAML 语法错）= 不知道 → 不当成空（fail-closed）。
whitelist_empty() { is_int "${sched_count:-}" && [ "$sched_count" -eq 0 ]; }

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
  if whitelist_empty; then
    # 白名单空：pull.py --mode scheduled 本次无采集，不建日期目录、不写 manifest —— 预期态，不告警
    NOTES+=("scheduled 白名单为空，快照目录暂无产出（属预期）")
  else
    FAILS+=("快照目录不存在或为空（$SNAPSHOT_DIR）— 采集链路从未产出")
  fi
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
    if whitelist_empty; then
      # 白名单空期不会有新快照，陈旧是预期态（历史快照已被清空时同属此情形）
      NOTES+=("快照不新鲜：最新为 $snap_day，但 scheduled 白名单为空（无定时产出属预期）")
    else
      FAILS+=("快照不新鲜：最新为 $snap_day（预期至少到昨天）— 定时任务或依赖可能已停")
    fi
  fi

  # schema 对齐 producer（research-collectors/pull.py write_manifest）：{source, date, count, status, mode}
  # 用临时文件承接 python 输出，以捕获其真实退出码
  # （process substitution 的 `$?` 只反映 read，拿不到 python 状态）
  # 输出 key<TAB>value：t/bad/nbad/sched/skip 为计数，failed/empty 每源一行
  stats_file=$(mktemp) || stats_file=""
  py_rc=1
  if [ -n "$stats_file" ]; then
    python3 -c '
import json, sys

tot = bad = nbad = sched = skip = 0
failed, empty = [], []


def name_of(obj):
    s = obj.get("source")
    return s if isinstance(s, str) and s else "<unnamed>"


for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        bad += 1
        continue
    if not isinstance(obj, dict) or "count" not in obj:
        bad += 1
        continue
    # mode 字段缺失/空 → 视为 scheduled（向后兼容旧 manifest 行）
    mode = obj.get("mode")
    if mode is None or mode == "":
        mode = "scheduled"
    if mode in ("manual", "archive"):
        skip += 1  # 没跑是正常态，不参与健康统计
        continue
    if mode != "scheduled":
        # 未知/写错的 mode 不能静默跳过（否则 sched=0 会走「白名单为空」的假绿灯）
        bad += 1
        continue
    c = obj.get("count")
    # 非整数/负数 = 数据损坏，不得静默计入或拉低 total，也不计入实跑源数
    if isinstance(c, bool) or not isinstance(c, int) or c < 0:
        bad += 1
        continue
    sched += 1
    tot += c
    if obj.get("status") != "ok":
        nbad += 1
        failed.append(name_of(obj))
    elif c == 0:
        empty.append(name_of(obj))  # status=ok 但 0 条 → 可能的静默失败，必须可见

for k, v in (("t", tot), ("bad", bad), ("nbad", nbad), ("sched", sched), ("skip", skip)):
    print("%s\t%d" % (k, v))
for s in failed:
    print("failed\t%s" % s.replace("\t", " ").replace("\n", " "))
for s in empty:
    print("empty\t%s" % s.replace("\t", " ").replace("\n", " "))
' "${latest}manifest.jsonl" > "$stats_file" 2>/dev/null
    py_rc=$?
  fi
  total=""; bad=""; nbad=""; sched=""; skip=""; failed_list=""; empty_list=""
  if [ -n "$stats_file" ] && [ -s "$stats_file" ]; then
    while IFS=$'\t' read -r k v; do
      case "$k" in
        t)       total=$v ;;
        bad)     bad=$v ;;
        nbad)    nbad=$v ;;
        sched)   sched=$v ;;
        skip)    skip=$v ;;
        failed)  failed_list="${failed_list:+$failed_list, }$v" ;;
        empty)   empty_list="${empty_list:+$empty_list, }$v" ;;
      esac
    done < "$stats_file"
  fi
  [ -n "$stats_file" ] && rm -f "$stats_file"
  if [ -z "$stats_file" ]; then
    FAILS+=("最近快照 $snap_day 无法创建临时统计文件（mktemp 失败），源级统计未执行")
  elif [ "$py_rc" -ne 0 ] || ! is_int "$total" || ! is_int "$bad" || ! is_int "$nbad" \
     || ! is_int "$sched" || ! is_int "$skip"; then
    FAILS+=("最近快照 $snap_day manifest 读取/汇总失败 (python exit=$py_rc)")
  else
    if [ "$bad" -gt 0 ]; then
      FAILS+=("最近快照 $snap_day manifest 结构损坏 $bad 行（解析失败/非 dict 行/count 或 mode 非法）")
    fi
    if [ "$sched" -eq 0 ]; then
      # 只有白名单确为空时，0 个 scheduled 行才是正常态；
      # 白名单有 scheduled 源却没有实跑行 = 采集未执行 / manifest 语义异常（真故障，不许假绿灯）
      if ! whitelist_empty; then
        FAILS+=("最近快照 $snap_day 无 scheduled 实跑行，但白名单存在 scheduled 源（采集未执行或 manifest 异常）")
      elif [ "$bad" -eq 0 ] && [ "$skip" -eq 0 ]; then
        NOTES+=("最近快照 $snap_day manifest 为 0 行（无 scheduled 实跑源，白名单为空属正常态）")
      elif [ "$bad" -eq 0 ]; then
        NOTES+=("最近快照 $snap_day 无 scheduled 实跑源（跳过 $skip 行 manual/archive，白名单为空属正常态）")
      fi
    else
      # 失败率阈值按实跑源数（sources = mode=scheduled 行数），manual/archive 不参与
      if [ "$nbad" -gt 0 ]; then
        if [ $((nbad * 2)) -gt "$sched" ]; then
          FAILS+=("最近快照 $snap_day 超过半数实跑源失败：$nbad/$sched（$failed_list）")
        else
          NOTES+=("最近快照 $snap_day 部分源失败 $nbad/$sched（未超半数，不告警；$failed_list）")
        fi
      fi
      # 保留原有「合计 0 条 = 断链」保护（全部实跑源都没产出，比单源零产出严重）；
      # 单源零产出本身不进 FAILS，只在 NOTES 里可见（见下）
      if [ "$total" -eq 0 ]; then
        FAILS+=("最近快照 $snap_day 实跑 $sched 个源产出 0 条（采集链路疑似断）")
      fi
      # 零产出源：status=ok 但 count=0 —— 不判失败（当天可能确实无新内容），但必须可见
      if [ -n "$empty_list" ]; then
        NOTES+=("零产出源：$empty_list（status=ok 但 0 条，可能被反爬/接口变更静默）")
      fi
    fi
  fi
fi

nfails=${#FAILS[@]}
if [ "$nfails" -gt 0 ]; then
  msg="⚠️ BriefNexus 采集链路 healthcheck 失败"
  for f in "${FAILS[@]}"; do msg="$msg
- $f"; done
  # 非致命的可见项（零产出源等）附在告警文案里，但不影响退出码判定
  # 用 ℹ️ 前缀与致命项区分，避免读的人误以为都同等严重
  if [ ${#NOTES[@]} -gt 0 ]; then
    for n in "${NOTES[@]}"; do msg="$msg
ℹ️ $n"; done
  fi
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
  if [ ${#NOTES[@]} -gt 0 ]; then
    printf '%s\n' "$(date '+%F %T') | NOTE: ${NOTES[*]}" >> "$LOG" 2>/dev/null || true
  fi
  exit 1
fi

# 无致命项：NOTES（零产出源 / 白名单空 / 部分源失败）仍要落日志，否则等于静默
if [ ${#NOTES[@]} -gt 0 ]; then
  printf '%s\n' "$(date '+%F %T') | INFO: ${NOTES[*]}" >> "$LOG" 2>/dev/null || true
fi

exit 0
