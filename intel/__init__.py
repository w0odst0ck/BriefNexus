"""BriefNexus 情报采集框架

用法:
  python -m intel.cli run        # 采集 → 分类 → 报告
  python -m intel.cli list       # 列出采集器
"""

# 导入所有采集器触发 @register
from intel.collectors import *
