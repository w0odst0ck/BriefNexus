"""
平台配置 — platform_config.yaml 加载 + config.d 片段合并 + 外部采集器挂载

- 统一配置入口: server(host/port/max_workers/task_timeout_s)
                + sources(enabled/module/params)
                + storage(db_path/dedup_ttl_days)
                + collectors_extra_dirs(外部采集器目录, 可选)
- config.d 片段: 主配置文件同目录下 config.d/*.yaml 启动时与主配置**深合并**
  (sources 合并/覆盖, server/storage 以主配置为准)
- 采集器动态 import: module 字段形如 "intel.collectors.white_house:WhiteHouseCollector",
  通过 importlib.import_module + getattr 解析为类,不依赖 intel 注册表(保留注册表兼容)。
- 外部采集器: 扫描 collectors_extra_dirs 下 *.py,以 "_platform_ext_" 前缀唯一模块名
  动态导入(避免与平台/标准库模块名冲突),外部模块可 import 同目录兄弟文件。
"""
import importlib
import importlib.util
import logging
import os
import sys

logger = logging.getLogger("platform.config")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "platform_config.yaml")
# 环境变量可覆盖配置路径(测试/多环境部署用)
CONFIG_ENV = "BRIEFNEXUS_PLATFORM_CONFIG"

# config.d 片段目录名(位于主配置文件同目录下)
CONFIG_D_DIRNAME = "config.d"
# 外部采集器模块名前缀(防污染: 与平台/标准库模块隔离)
EXT_MODULE_PREFIX = "_platform_ext_"

_config: dict = None  # 模块级缓存


def load_config(path: str | None = None) -> dict:
    """加载 platform_config.yaml + config.d/*.yaml 深合并;path 缺省时依次取环境变量、默认路径"""
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
    _config = _merge_config_dir(_config, path)
    load_extra_collectors(_config)  # 挂载外部采集器(生产路径;测试注入 set_config 不触发)
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


def _deep_merge(base: dict, override: dict) -> dict:
    """深合并两个配置段: dict 递归合并,其余(标量/列表)以 override(片段)为准。

    server/storage 以主配置为准: 片段中的同名段被忽略(仅主配置缺省时才采用片段值)。
    """
    result = dict(base)
    for k, v in override.items():
        if k in ("server", "storage") and k in result:
            continue  # 主配置优先,忽略片段中的 server/storage
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _merge_config_dir(cfg: dict, main_path: str) -> dict:
    """合并主配置同目录 config.d/*.yaml 片段(按文件名排序,后片段覆盖先片段)"""
    frag_dir = os.path.join(os.path.dirname(os.path.abspath(main_path)), CONFIG_D_DIRNAME)
    if not os.path.isdir(frag_dir):
        return cfg
    import yaml
    merged = cfg
    for fn in sorted(os.listdir(frag_dir)):
        if not fn.endswith((".yaml", ".yml")):
            continue
        fpath = os.path.join(frag_dir, fn)
        try:
            with open(fpath, encoding="utf-8") as f:
                frag = yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning("config.d 片段 %s 解析失败,跳过: %s", fn, e)
            continue
        merged = _deep_merge(merged, frag)
        logger.info("已合并 config.d 片段: %s", fn)
    return merged


def load_extra_collectors(cfg: dict | None = None):
    """扫描 collectors_extra_dirs 下所有 *.py,以 "_platform_ext_<文件名>" 模块名动态导入。

    - 模块名加前缀,避免与平台/标准库/业务包模块冲突污染
    - 外部目录会临时加入 sys.path,便于外部模块 import 同目录兄弟文件
    - 无外部目录(或目录不存在)时静默跳过,行为与旧版一致
    """
    cfg = cfg if cfg is not None else get_config()
    for d in (cfg.get("collectors_extra_dirs") or []):
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            logger.warning("外部采集器目录不存在,跳过: %s", d)
            continue
        if d not in sys.path:
            sys.path.insert(0, d)
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py") or fn.startswith("_"):
                continue  # 跳过非 .py 与下划线前缀(含 __init__.py)
            stem = os.path.splitext(fn)[0]
            module_name = f"{EXT_MODULE_PREFIX}{stem}"
            if module_name in sys.modules:
                continue  # 已加载过
            fpath = os.path.join(d, fn)
            try:
                spec = importlib.util.spec_from_file_location(module_name, fpath)
                mod = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = mod
                spec.loader.exec_module(mod)
                logger.info("已挂载外部采集器: %s (%s)", module_name, fpath)
            except Exception as e:
                logger.error("外部采集器模块加载失败 %s: %s", fn, e)
                sys.modules.pop(module_name, None)


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
    """storage 段: db_path(相对路径基于项目根解析)/dedup_ttl_days/清理 TTL"""
    storage = get_config().get("storage", {})
    db_path = storage.get("db_path", "data/briefnexus.db")
    if not os.path.isabs(db_path):
        db_path = os.path.join(PROJECT_ROOT, db_path)
    return {
        "db_path": os.path.abspath(db_path),
        "dedup_ttl_days": int(storage.get("dedup_ttl_days", 90)),
        # 每日清理(D13)参数: 已消费 items 保留天数 / 完成任务归档天数
        "consumed_ttl_days": int(storage.get("consumed_ttl_days", 90)),
        "task_archive_days": int(storage.get("task_archive_days", 30)),
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
