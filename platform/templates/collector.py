# BriefNexus 采集器骨架模板(D21) — 供 POST /v1/collect 的 collector.code 内联使用
#
# ⚠️ 实验性功能: 代码内联(collector.code)仅限受信本机调用方,默认关闭
#   (ALLOW_INLINE_CODE=true 才启用)。生产/对外请使用 collector.module 引用
#   (调用方自管代码,部署到 collectors_extra_dirs)。
#
# 契约: 本文件内容可直接作为 collector.code 传入(需 ALLOW_INLINE_CODE=true)。
# 二选一,平台按顺序检测:
#   方案 A: 定义 class Collector(BaseCollector)          —— 类式(推荐,可声明 PARAM_SCHEMA)
#   方案 B: 定义 def crawl(sess) -> list[dict]          —— 函数式(title/url 必填,其余可选)
# 两者都未定义 → 422。
#
# 内联代码可用命名空间(已注入):
#   - 常用模块: requests / bs4 / json / re / time / datetime / logging
#   - intel 基类: BaseCollector / NewsItem(intel.core.base)
#   - stderr_write(msg): 向 stderr 写日志(平台捕获进任务的 collector_log 字段)
#   - Python 内置 __builtins__(open/eval/exec/compile/__import__ 被 AST 安全检查拦截)
# 禁止导入 os/subprocess/socket/shutil/sys/pathlib/ctypes/pickle/multiprocessing/threading。

from intel.core.base import BaseCollector, NewsItem  # 命名空间已注入,也可显式导入


# ============ 方案 A: 类式 ============
class Collector(BaseCollector):
    """示例采集器: 继承 BaseCollector,实现 crawl(sess) 返回 NewsItem 列表"""

    source_name = "my_inline_source"   # 任务/去重标识;留空则取 code hash 前 8 位
    display_name = "我的内联源"
    domains = ["example.com"]
    # 可选: 声明参数 schema,提交时校验(宽松模式不声明即可)
    # PARAM_SCHEMA = {"max_age": {"type": "int", "min": 1, "max": 90}}

    def crawl(self, sess):
        """采集入口: sess 为平台构造的 requests.Session(UA 已轮换)"""
        # TODO: 在此实现抓取逻辑,返回 [NewsItem, ...](可空列表)
        return [
            NewsItem(
                title="示例标题",
                url="https://example.com/item/1",
                summary="示例摘要",
                source=self.source_name,
                domain="example.com",
            )
        ]


# ============ 方案 B: 函数式(与方案 A 二选一,同时定义时优先用类)============
def crawl(sess) -> list:
    """采集入口: 返回 list[dict],title/url 必填,其余(summary/source/domain/
    sector/type/date_str/date 及任意自定义字段)可选,自定义字段进 raw_data。"""
    # TODO: 在此实现抓取逻辑
    return [
        {"title": "示例标题", "url": "https://example.com/item/1", "summary": "示例摘要"},
    ]
