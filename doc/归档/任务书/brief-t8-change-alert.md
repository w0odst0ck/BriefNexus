# T8 变更预警推送 — 简报（context pack）

> work-item: t8-change-alert ｜ collab-flow 第二个真实项目（验证契约补丁修复）
> 2026-08-15 晚间 ｜ 目标：合规/情报「检测到变更」→「飞书通知」链路打通

## 背景

- BriefNexus 生态已有：compare_snapshots.py（mojin-store/intel/，快照 diff，今晚交付）、changedetection.io（:5000 部署，通用网页变更监控）、feishu_push.py（trade-pulse 工具，HTTP 直推飞书，带重试）
- 缺口：检测到变更后**无通知**——合规速览/竞品盯梢要人肉去看结果文件
- 本任务打通「变更 → 飞书预警」闭环

## 目标

1. `compare_snapshots.py` 支持 `--push` 参数：检测到变更（added/removed/status_changed 任一 > 0）→ 调 feishu_push.py 推送变更摘要（含变更总数 + 各源明细）；无变更不推送（安静）
2. changedetection.io 启用通知：配置 webhook/json 通知 → 转发到飞书（或说明其原生通知接入方式）
3. 推送失败不中断主流程（警告即可，退出码不受影响）

## 约束

- 不动 compare_snapshots.py 现有 diff 逻辑（纯增量：--push 分支）
- feishu_push.py 复用 trade-pulse 现有工具（不复制到新位置；推送凭证走环境变量 FEISHU_APP_SECRET，不落盘）
- 测试用 stub（mock feishu 调用），不真发飞书

## 红线

- 不写 token/key 到文件
- 推送通道仅飞书（现状）；变更检测逻辑零改动
- 无变更时不推送（防噪声）

## 验收标准

1. `compare_snapshots.py --push` 有变更时调用 feishu 推送（stub 验证调用 + 摘要内容正确）；无变更不调用
2. 推送失败 → 警告 + 主流程继续（退出码 0）
3. 单测 ≥4 用例（有变更推送/无变更不推/推送失败容错/摘要格式）
4. changedetection.io 通知配置说明写入 README（webhook → 飞书）
5. ruff 干净；现有 compare_snapshots 8 用例不破坏
