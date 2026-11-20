# 模型接入架构 — 接口现状与扩展方案

## 当前接口盘点

### 可直接被模型调用的

| 模块 | 入口类/函数 | 用途 |
|------|-----------|------|
| `standards.engine.storage.StandardDB` | `search()`, `filter()`, `get_by_standard_no()` | 标准数据查询 |
| `standards.engine.exporter` | `export_json()`, `export_csv()`, `export_markdown()` | 数据导出为模型输入 |
| `intel.core.base.NewsItem` | dataclass | 统一情报数据格式 |
| `intel.pipeline.classifier` | `classify()` | 规则分类（当前是词典规则） |

### 可直接 import 调用的示例

```python
# 查询标准数据
from standards.engine.storage import StandardDB
db = StandardDB()
results = db.search("LED 灯具")  # FTS5 全文搜索
db.filter(sector="automotive_lighting")  # 按行业过滤
db.close()

# 获取情报数据
from intel.core.registry import instantiate_collectors, get_collector_classes
collectors = instantiate_collectors()  # 实例化所有采集器
for c in collectors:
    items = c.crawl(session)  # 采集
```

### 缺少的

| 缺口 | 影响 |
|------|------|
| 无 `models/` 目录 | 模型代码没地方放 |
| 无模型接口规范 | 每个模型自己写 IO，不统一 |
| 无训练/推理流水线 | 模型训练需手动拉数据 |
| 无特征工程层 | 文本→向量等转换逻辑分散 |
| 无仿真平台输出适配器 | 汽车仿真需自定义导出格式 |

---

## 推荐架构

### 新增目录

```
BriefNexus/
├── models/                    ← NEW: 所有模型代码
│   ├── __init__.py
│   ├── base.py                ← 模型基类（定义接口）
│   ├── registry.py            ← 模型注册器
│   ├── features/              ← 特征工程
│   │   ├── text.py            ← 文本向量化
│   │   ├── timeseries.py      ← 时序特征
│   │   └── standards.py       ← 标准结构化特征
│   ├── nlp/                   ← NLP 模型
│   │   ├── sentiment.py       ← 政策情感打分
│   │   └── clustering.py      ← 标准聚类
│   ├── financial/             ← 金融分析模型
│   │   ├── impact_score.py    ← 事件影响力评分
│   │   └── predictor.py       ← 趋势预测
│   └── simulation/            ← 仿真对接模型
│       ├── standard_param.py  ← 标准→参数提取
│       └── compliance.py      ← 合规检查引擎
│
├── adapters/                  ← NEW: 外部系统对接
│   ├── __init__.py
│   ├── simulation/            ← 汽车仿真平台适配器
│   │   ├── base.py
│   │   ├── carsim.py          ← CarSim 格式
│   │   └── opendrive.py       ← OpenDRIVE 格式
│   └── output/                ← 不同平台的输出格式
│       ├── zhihu.py
│       ├── xhs.py
│       └── wechat.py
```

### 模型接口规范

```python
# models/base.py

from abc import ABC, abstractmethod
from typing import Any, Dict, List

class BaseModel(ABC):
    """所有模型的统一接口"""
    
    @abstractmethod
    def predict(self, input: Any) -> Any:
        """推理入口"""
        ...
    
    @abstractmethod
    def train(self, data: List[Dict], **kwargs):
        """训练入口（不需要训练的模型可跳过）"""
        ...
    
    @property
    def metadata(self) -> Dict:
        """模型元数据：名称、版本、输入/输出 schema"""
        ...
```

### 数据流

```
  采集层 (intel / standards)
       ↓
  特征层 (models/features/)
       ↓
  ┌────┴────┐
  │         │
模型推理    数据导出
(情感/聚类  (JSON/Parquet/
 /合规)      CSV)
  │         │
  └────┬────┘
       ↓
  输出适配 (adapters/)
       ↓
  平台发布 / 仿真引擎 / 论文图表
```

---

## 各模型接入方案

### 1. 金融分析模型

```python
# 输入: intel 采集的 NewsItem 列表
# 特征: title 向量化 + 来源权重 + 时间衰减
# 模型: 情感分类 (LLM 零样本 / fine-tune DistilBERT)
# 输出: {-1.0 ~ 1.0} 情感分数 + 关键词
# 对接: 直接 Python import，无需额外服务
```

### 2. 标准聚类模型

```python
# 输入: DB 查询的 standard_no + title + sector
# 特征: sentence-transformers 嵌入
# 模型: UMAP + HDBSCAN
# 输出: 聚类标签 + 可视化散点图
# 对接: 同上，Python 内联调用
```

### 3. 汽车仿真对接

```
当前: SQLite → JSON/CSV → 人工导入仿真
未来: SQLite → models/simulation/ → 结构化参数 → adapter/ → 仿真平台原生格式

需要的中间层:
  - standards → 参数提取（LLM + 规则）
  - 参数 → 仿真约束格式（JSON Schema）
  - 约束 → 平台适配（CarSim / IPG CarMaker 等专有格式）
```

---

## 优先建设顺序

| 优先级 | 模块 | 工作量 | 依赖 |
|--------|------|--------|------|
| P0 | `models/base.py` + registry | 1 天 | 无 |
| P0 | `models/features/text.py` | 0.5 天 | sentence-transformers |
| P1 | `models/nlp/sentiment.py` | 1 天 | 特征层就绪 |
| P1 | `models/nlp/clustering.py` | 1 天 | 特征层就绪 |
| P2 | 金融适配器反爬升级 | 2 天 | 无 |
| P3 | `models/simulation/` | 待定 | 需确定仿真平台 |

---

## 当前可用的快捷路径

即使 `models/` 还没建，你现在就可以这样调：

```python
# 直接 Python import 现有接口，零成本接入

# 1. 拉标准数据
from standards.engine.storage import StandardDB
db = StandardDB()
auto_lighting = list(db.filter(sector="automotive_lighting"))

# 2. 拉情报数据（今天采集的 75 条）
import json
with open("intel/output/report_2026-07-10.json") as f:
    items = json.load(f)

# 3. 喂给任何 Python 模型
from your_model import analyze
result = analyze(auto_lighting + items)
```

已有接口都是纯 Python 对象，不挑模型框架（PyTorch / sklearn / 纯规则都行）。
