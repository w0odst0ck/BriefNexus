"""
平台配置 — platform_config.yaml 加载 + 采集器动态 import

- 统一配置入口: server(host/port/max_workers/task_timeout_s)
                + sources(enabled/module/params)
                + storage(db_path/dedup_ttl_days)
- 采集器动态 import: module 字段形如 "intel.collectors.white_house:WhiteHouseCollector",
  通过 importlib.import_module + getattr 解析为类,不依赖 intel 注册表(保留注册表兼容)。
"""
import importlib
import logging
import os

logger = logging.getLogger("platform.config")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "platform_config.yaml")
# 环境变量可覆盖配置路径(测试/多环境部署用)
CONFIG_ENV = "BRIEFNEXUS_PLATFORM_CONFIG"

_config: dict = None  # 模块级缓存


def load_config(path: str | None = None) -> dict:
    """加载 platform_config.yaml;path 缺省时依次取环境变量、默认路径"""
    global _config
    path = path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(f"平台配置文件不存在: {path}")
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            _config = yaml.safe_load(f) or {}
    except ImportError:
        raise RuntimeError("缺少依赖 pyyaml,请先安装") from None
    return _config


def get_config() -> dict:
    """获取配置(懒加载)"""
    if _config is None:
        load_config()
    return _config


def set_config(cfg: dict):
    """注入配置(测试用,避免依赖真实 yaml 与磁盘)"""
    global _config
    _config = cfg


def get_server_config() -> dict:
    """server 段: host/port/max_workers/task_timeout_s(带默认值)"""
    server = get_config().get("server", {})
    return {
        "host": server.get("host", "127.0.0.1"),
        "port": int(server.get("port", 9000)),
        "max_workers": int(server.get("max_workers", 2)),
        "task_timeout_s": int(server.get("task_timeout_s", 300)),
    }


def get_storage_config() -> dict:
    """storage 段: db_path(相对路径基于项目根解析)/dedup_ttl_days"""
    storage = get_config().get("storage", {})
    db_path = storage.get("db_path", "data/briefnexus.db")
    if not os.path.isabs(db_path):
        db_path = os.path.join(PROJECT_ROOT, db_path)
    return {
        "db_path": os.path.abspath(db_path),
        "dedup_ttl_days": int(storage.get("dedup_ttl_days", 90)),
    }


def get_sources_config() -> dict:
    """全部 sources 配置: {name: {enabled/module/params/...}}"""
    return get_config().get("sources", {}) or {}


def get_enabled_sources() -> dict:
    """enabled=true 的源: {name: cfg}(默认视为启用)"""
    return {n: c for n, c in get_sources_config().items() if c.get("enabled", True)}


def resolve_collector_class(module_path: str):
    """动态 import 采集器类

    Args:
        module_path: "intel.collectors.white_house:WhiteHouseCollector"

    Returns:
        采集器类(继承 intel.core.base.BaseCollector)
    """
    if not module_path or ":" not in module_path:
        raise ValueError(f"module 字段格式应为 'module:ClassName', 实际为: {module_path!r}")
    module_name, class_name = module_path.rsplit(":", 1)
    try:
        module = importlib.import_module(module_name)
    except Exception as e:
        raise ImportError(f"采集器模块导入失败 {module_name}: {e}") from e
    cls = getattr(module, class_name, None)
    if cls is None:
        raise AttributeError(f"模块 {module_name} 中不存在类 {class_name}")
    return cls


def get_source_info(name: str) -> dict:
    """单个启用源的元信息: {name, module, params, ...};未启用或不存在返回 None"""
    cfg = get_sources_config().get(name)
    if not cfg or not cfg.get("enabled", True):
        return None
    return {"name": name, **cfg}
