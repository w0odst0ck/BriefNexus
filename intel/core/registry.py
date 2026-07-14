"""
采集器注册器 — @register 装饰器自动发现

用法:
    from intel.core.registry import register, get_collectors

    @register("white_house")
    class WhiteHouseCollector(BaseCollector):
        ...

配置驱动筛选:
    collectors = get_collectors(config={"sources": {"white_house": {"enabled": True}}})
"""

import logging

_COLLECTORS = {}

logger = logging.getLogger("intel.registry")


def register(name: str = None):
    """装饰器：注册采集器到全局注册表"""
    def decorator(cls):
        key = name or cls.__name__
        if key in _COLLECTORS:
            logger.warning("采集器 %s 已注册，将被覆盖", key)
        _COLLECTORS[key] = cls
        logger.debug("注册采集器: %s → %s", key, cls.__name__)
        return cls
    return decorator


def get_collector_classes(config: dict = None, domains: str = None) -> dict:
    """获取采集器类字典

    Args:
        config: 可选配置字典，格式:
            {"sources": {"white_house": {"enabled": True, "max_age": 7}, ...}}
            不传则返回全部注册的采集器
        domains: 逗号分隔的领域字符串，如 "finance,self_driving"
                 筛选拥有任一匹配领域的采集器

    Returns:
        {name: collector_class, ...}
    """
    if config is None:
        result = dict(_COLLECTORS)
    else:
        sources_cfg = config.get("sources", {})
        if not sources_cfg:
            result = dict(_COLLECTORS)
        else:
            result = {}
            for name, cls in _COLLECTORS.items():
                if name in sources_cfg:
                    source_cfg = sources_cfg[name]
                    if source_cfg.get("enabled", True):
                        result[name] = cls
                else:
                    result[name] = cls

    if domains:
        domain_set = set(d.strip() for d in domains.split(","))
        result = {
            n: c for n, c in result.items()
            if hasattr(c, "domains") and domain_set & set(c.domains)
        }

    return result


def instantiate_collectors(config: dict = None, domains: str = None) -> list:
    """实例化启用的采集器

    Args:
        config: 可选配置，支持每源参数
        domains: 逗号分隔的领域字符串

    Returns:
        [BaseCollector_instance, ...]
    """
    classes = get_collector_classes(config, domains=domains)
    sources_cfg = (config or {}).get("sources", {})

    instances = []
    for name, cls in classes.items():
        cfg = sources_cfg.get(name, {})
        max_age = cfg.get("max_age", 7)
        try:
            instance = cls(max_age=max_age)
            instances.append(instance)
        except Exception as e:
            logger.warning("实例化采集器失败 [%s]: %s", name, e)

    return instances
