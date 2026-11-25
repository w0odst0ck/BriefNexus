# V2 方案 — 采集能力升级

> 2026-11-17 立，2026-11-20 精简 ｜ **状态：待启动（用户拍板「V1 先用，够用了」）**
> V2 = 采集能力（本文档）；V3 = 平台层（认证/多实例/SDK，未规划）

---

## 边界

| 做 | 不做（留 V3） |
|---|---|
| `intel/core/*` 采集内核 | `platform/*` API/调度/存储 |
| `intel/collectors/*` 内置源 | 认证、多实例、水平扩展 |
| 外挂采集器能力契约 | 路由管理界面、SDK |

**判断**：只影响「采集器怎么写、能采到什么」，不改「消费方怎么调」。

---

## 五个能力包

### A1 渲染引擎升级 ★最高优先

**问题**（实测）：每请求 launch+close 浏览器，example.com 纯启动 2.1s、巨潮 17.1s；无 XHR 捕获，WAF 站只能靠解析 HTML。

**方案**：浏览器/context 复用池（常驻 worker + JSON 行协议）· 资源拦截（默认 block image/font/media）· **XHR 响应捕获**（`capture: {url_pattern}`，WAF/SPA 主力）· 等待策略 DSL（`wait: {selector, until}`）· 基础 stealth · storage_state 复用（登录态，TTL + 权限 600 + 不入库）

**验收**：连续两次渲染第二次 ≤ 首次 60% · 巨潮端到端 ≤ 8s · 新增 1 个 XHR 捕获实装源 · 141 测试不破坏 + 新增 ≥10 · 渲染失败仍降级静态

---

### A2 HTML_SPEC 声明式抽取

**问题**：无 `HTML_SPEC`，加 HTML 源 = 手写 100 行；`startswith("//")` URL 拼接复制 4 处。

**方案**：`intel/core/html.py` — `HTMLCollector` + `HTML_SPEC`（`list: {container, rows}` + `fields: {title, url, date → {selector, attr, transform}}`）· 复用 `api.py` 的 7 个 transform · **`join_url()` 收口** · 容错契约同 `APICollector`（永不抛异常）· 简单分页声明

**验收**：用 HTML_SPEC 重写 1 个现有源且条目一致 · URL 拼接收敛为 1 处（grep 断言）· 加新源只需声明 · ≥12 单测

---

### A3 网络层统一

**问题**：mihomo :7890 在跑但代码零使用；UA 列表两份重复（`cli.py:41` / `scheduler.py:39`）；无 ETag；无 host 限速。

**方案**：`intel/core/http.py` 唯一入口 `new_session(cfg)` — 声明式代理（默认读 `BN_PROXY`，失败降级直连）· 内存态 ETag/LM 缓存（LRU，符合零持久化）· host 令牌桶（默认 1 req/s，可 per-source）· UA 单源（`intel/core/ua.py`）· 指数退避 + `Retry-After`

**验收**：境外源开代理实测 200 · 二次请求 304 → 空 + 标记 · 同 host 连发 5 次间隔 ≥1s · UA 全局仅 1 处定义 · 141 测试不破坏

> **建议**：A3 的「代理 + UA 单源」提前到 A1 之前（1 小时落地，立刻解锁境外源）

---

### A4 正文抽取

**问题**：`summary` 大量为空；trafilatura/lxml 全未装。

**方案**：`intel/core/extract.py` — `extract_main_text()`（trafilatura 主 + readability 备 + 正则兜底），声明式开关 `extract: {summary: true, max_chars: 300}`，默认关防成本；依赖进 `requirements.txt`

**验收**：5 个真实 URL 非空率 ≥4/5 · 开启前后 summary 有效率提升 · 依赖声明完整

---

### A5 协议扩展（按需，不排期）

- **RSS 内置**：`RSSCollector`（feedparser 已有，仅 2 源用）
- **Sitemap 发现**：`sitemap: true` → 无列表页的站
- **GraphQL**：`APICollector` 已够用，只补文档
- **doc 契约**：`type=doc` 定义（MinerU 仍走外挂）
- **社媒**：**不内置**（登录态 + 法律风险），保持外挂

---

## 排期

| 顺序 | 包 | 理由 |
|---|---|---|
| 0 | A3 前半（代理+UA） | 1 小时，立刻解锁境外源 |
| 1 | **A1** | 数据最贵一环，17s→8s 决定可用性 |
| 2 | A2 | 加源成本从「写代码」降到「写配置」 |
| 3 | A3 后半 | 稳定性底座 |
| 4 | A4 | 独立无依赖 |
| 5 | A5 | 需求驱动 |

**流程**：`rx` + 独立复验 + ocr review + commit（AGENTS.md 路由）。A1 属「有方案权衡」，先出 design 再 execute。

---

## 风险

| 风险 | 处置 |
|---|---|
| 常驻 worker 与「零持久化」冲突 | 池只放浏览器进程（无数据落盘），HTML 结果仍只回内存 |
| storage_state 落盘 = 唯一新增磁盘状态 | 用户目录 + 权限 600 + 不入库 + TTL |
| 资源拦截误伤（懒加载触发数据） | per-source 可关；失败降级全资源 |
| 声明式层抽象过度 | 只抽象「3+ 源重复出现」的模式；单源特例允许直接覆盖 hook |
| 平台侧不受影响是硬约束 | 每次跑 141 测试；`platform/*` 零改动（除 UA import） |

---

## 待拍板

1. A1 先出设计稿再动手？（建议是）
2. A5 哪些现在要？
3. 代理默认策略：境外源开、国内源关？