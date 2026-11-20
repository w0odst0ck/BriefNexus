# Brief — M2a：BOSS 搜索页采集升级（参数化 + 字段扩展 + 频率控制）

> 依据：M2-BOSS采集-方案报告.md §5 M2a 阶段。目标城市：上海（city code 101020100）。

## 背景

BriefNexus 已有 boss_zhipin 验证源（T6，render:true，写死「自动驾驶/北京」，正则提取职位名+链接）。M2a 把它升级为正式采集器：关键词池驱动搜索 + 字段扩展 + 频率控制 + 去重。消费端（boss_recommend.py）在 M2c 做，本次不做。

## 目标

升级 `BriefNexus/intel/collectors/boss_zhipin.py`：

```
搜索词（关键词池代表词 × 城市=上海）──► CDP 渲染列表页 ──► 岗位卡片
  卡片字段: 职位名/公司/薪资/经验学历/城市/链接
  ──► 分页（第 1 页起步，页数参数化）
  ──► 去重（链接 hash）
  ──► 产物: intel/data/boss/<日期>/joblist.json（幂等落盘）
```

- **关键词池驱动**：搜索词不写死——从 job-search 的 `job_keywords.json` 取 5 方向代表词（每方向 1-2 个，共 ~8-10 词：如 大模型/RAG/物联网/嵌入式/AI产品经理/爬虫/跨境电商/独立站），可配置（config 或参数覆盖）
- **参数化**：query / city（默认上海 101020100）/ pages（默认 1）/ max_items（默认 30）
- **字段扩展**：卡片解析补 公司/薪资/经验学历（BOSS 卡片 DOM 结构需真实验证，正则多级降级：完整字段 → 部分字段 → 职位名兜底）
- **频率控制**：每页间隔 ≥5s + 随机抖动（2s），单页失败容错不中断
- **去重**：job link hash 集合，跨关键词去重（同岗位多关键词命中只采一次）

## 约束

- 在 BriefNexus 内升级（intel/collectors/boss_zhipin.py + tests/），不碰其他采集器/平台核心
- 复用 render 基建（render:true 声明，RenderAwareSession），渲染失败降级静态（既有模式）
- 不新增依赖（requests + re + stdlib）；测试 unittest mock
- 关键词池读取：设计定（直接读 job-search job_keywords.json 绝对路径 or 复制到 intel/data 作配置？倾向**直接读 job-search 真源**，单点真源；路径可配置覆盖）
- 产物：intel/data/boss/<date>/joblist.json（幂等，原子写，gitignore）

## 红线

- 不虚构字段：解析失败返回空字段，不编造薪资/公司
- 频率克制：间隔 ≥5s + 抖动，不并发轰炸
- 不自动投递、不写 key、不公开数据
- 反爬拦截（无卡片/验证码页）→ 返回 [] + warning，不硬刚
- 不碰平台核心（render.py/任务系统）——只在采集器层改

## 验收标准

1. `python3 -m unittest discover -s intel/tests` 全绿（原 79 + 新增 boss_m2a 用例 ≥8：参数化/字段解析降级/频率控制/去重/容错/产物结构）
2. ruff 零错误（BriefNexus .venv/bin/ruff）
3. **真实渲染验证**（宿主执行，INTEL_RENDER_LIVE 或平台 run）：上海+关键词池代表词 → ≥3 张卡片（含职位/公司/薪资至少一项），或如实记录反爬拦截
4. 产物 joblist.json 结构正确（幂等重跑不重复）
5. `git status --short` 只有预期文件（boss_zhipin.py / tests / 简报）

## 环境

- 仓库：/home/l/.openclaw/workspace/projects/BriefNexus（git）
- 测试：BriefNexus .venv/bin/python；ruff：.venv/bin/ruff
- 真实验证：宿主跑（render 需要真实 chromium + 系统库，T6 已装）
- 不要 git commit（由青简完成）
