# platform/config.d — 配置片段合并(D12b)

本目录下的 `*.yaml`(或 `*.yml`)片段会在**启动时**与主配置文件
`platform/platform_config.yaml` 做**深合并**后再进入服务。

## 合并规则

- 片段格式与主配置一致(顶层键: `server` / `sources` / `storage` / `collectors_extra_dirs` ...)
- `sources` 为**字典合并**: 片段可以
  - 新增主配置没有的源(如外部采集器目录挂载的适配器)
  - 覆盖已有源的字段(如把某源 `enabled: false` 关闭、改 `params`)
- `server` / `storage` **以主配置为准**,片段中的同名段被忽略(防止片段误改端口/库路径)
- 其余顶层键(如 `collectors_extra_dirs`): 标量/列表直接覆盖,字典递归合并
- 多个片段按**文件名排序**依次合并,后片段覆盖先片段

## 用途

业务项目自维护外部采集器时,平台仓零改动:
1. 在 `platform_config.yaml` 的 `collectors_extra_dirs` 填入外部适配器目录
2. 在本目录放一个片段声明外部源(module 指向挂载后的模块名)

## 示例片段(默认不启用,按需复制)

```yaml
# 文件名示例: 10_external_sources.yaml
# 外部源声明: 采集器放在 collectors_extra_dirs 目录,文件名 my_collector.py,
# 挂载后模块名为 _platform_ext_my_collector,类名 MyCollector。
sources:
  my_external_source:
    enabled: true
    module: _platform_ext_my_collector:MyCollector
    params: {max_age: 7}
```

```yaml
# 文件名示例: 20_disable_cninfo.yaml
# 覆盖主配置: 关闭某内置源(主配置仍保留,片段临时禁用)
sources:
  cninfo:
    enabled: false
```

> 注意: 片段文件名不要以 `_` 开头(YAML 解析不限制,但建议按数字前缀排序);
> 片段解析失败仅记录告警并跳过,不影响主配置加载。
