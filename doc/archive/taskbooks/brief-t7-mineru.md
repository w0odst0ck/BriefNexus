# T7 MinerU PDF 解析能力 — 简报（context pack）

> work-item: t7-mineru ｜ collab-flow 第四个真实项目
> 2026-08-16 凌晨 ｜ 目标：PDF/文档 → 文本解析能力（标准/政策全文检索前置）

## 背景

- 工具箱调研结论：MinerU（opendatalab，⭐77.7k）是 PDF/文档解析最佳工具（版式还原/公式/表格），能力矩阵 C1「文档/PDF 解析」规划中
- 场景：国标/政策 PDF（如 GB 标准全文、SASO/SFDA 法规文件）→ 文本 → 可检索/可入 NewsItem（standards 域重建前置；合规速览的 PDF 支撑）
- 环境：MinerU 需 Python <3.14 → 独立 venv `/home/l/venvs/mineru/.venv`（py3.12，torch 安装中，后台）
- 架构借鉴：T6 render 模式——重依赖子进程隔离（BriefNexus 进程不 import mineru），独立 venv 执行

## 目标

1. 新增文档解析执行器（MinerU 封装）：PDF 文件路径/URL → 解析 → 返回文本（markdown/纯文本）
2. 独立子进程 worker（mineru venv 执行，输出 JSON），宿主进程不 import mineru
3. 集成点：research-collectors 或 BriefNexus 侧提供 CLI/采集器接口（PDF URL → NewsItem 或落盘文本）
4. 验证：解析 1 个真实 PDF（国标或政策文件）端到端出文本
5. 测试：stub/mock（不依赖 mineru 可跑单测）+ live 冒烟（真实 PDF，隔离标记）

## 约束

- mineru 独立 venv（/home/l/venvs/mineru/.venv），不污染 BriefNexus/research-collectors 依赖
- 子进程 worker 模式（宿主不 import mineru/torch）
- 解析失败/超时 → 返回错误 JSON，调用方降级，不中断整体
- CPU 推理可接受（PDF 解析非实时）；不写 token/key

## 红线

- 不改现有采集器行为；新增能力纯增量
- 不引入常驻服务（按需子进程）
- 文件安全：只解析任务指定的 PDF（URL 或路径），不扫任意目录

## 验收标准

1. 解析执行器：PDF → 文本（stub 单测 + 真实 PDF 冒烟）
2. 子进程 worker 输出 JSON 契约（ok/text/error）
3. 调用方接口：给定 PDF URL/路径 → 文本（或降级错误）
4. 失败/超时降级不中断
5. ruff 干净；现有测试不破坏

## 备注

- 安装状态：/tmp/mineru-install.status（py3.12 venv + torch，预计 5-15 分钟）
- MinerU 首次运行会下载模型权重（~1GB），live 冒烟需代理；模型缓存 ~/.cache
