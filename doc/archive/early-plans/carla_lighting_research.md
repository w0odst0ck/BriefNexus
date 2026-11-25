# Carla 光照能力调研

> 生成: 2026-07-14 | 目的: 评估 Carla 能否支持"GB 照明标准 → 感知退化"实验

---

## 一、Carla WeatherParameters API 能力边界

### 可控参数（Python API）

| 参数 | 范围 | 对应自然条件 |
|---|---|---|
| `cloudiness` | 0–100 | 云量，0=晴空，100=全阴 |
| `precipitation` | 0–100 | 降雨强度 |
| `precipitation_deposits` | 0–100 | 路面积水（puddles） |
| `wind_intensity` | 0–100 | 风速（影响雨的方向和树叶） |
| `sun_azimuth_angle` | 0–360° | 太阳方位角 |
| `sun_altitude_angle` | -90–90° | 太阳高度角（-90=午夜，90=正午） |
| `fog_density` | 0–100 | 雾浓度（仅影响 RGB 相机） |
| `scattering_intensity` | 0–1 | 散射强度（0.9.12+） |
| `mie_scattering_scale` | 0–1 | Mie 散射（0.9.12+） |
| `rayleigh_scattering_scale` | 0–1 | Rayleigh 散射（0.9.12+） |

### 预设天气条件

Carla 提供了 14 个预设 `WeatherParameters`。相关场景：

| 预设 | 对应 | 备注 |
|---|---|---|
| `ClearNoon` | 晴天正午 | 基线 |
| `ClearSunset` | 晴天黄昏 | 低角度光照 |
| `Night` | 夜间 | 仅环境光 + 车灯 |
| `HardRainNoon` | 大雨正午 | 雨+低可见度 |
| `HardRainNight` | 大雨夜间 | 最强退化组合 |
| `SoftRainNoon` | 小雨正午 | |
| `WetCloudyNoon` | 湿+多云 | |
| `WetNight` | 湿+夜间 | |
| `MidRainSunset` | 中雨黄昏 | |

### 光照相关 API

- **车辆灯光**: Python API 只提供 `VehicleLightState` — 枚举 `LowBeam` / `HighBeam` / `FogLights` / `Position`，仅能开关，不能调强度/配光
- **街灯**: Python API 可调强度（`set_light_intensity()`），但不能改颜色/方向
- **UE 编辑**: 如果需要自定义车辆前照灯的配光曲线，必须进入 Unreal Editor 修改蓝图

---

## 二、核心限制

### ❌ 不能做的事

1. **调整前照灯强度/配光曲线** — GitHub issue [#7311](https://github.com/carla-simulator/carla/issues/7311) 已确认，官方回复："you can only change the streetlights, if you want to change the vehicle intensity, you should modify the vehicle blueprint"。需要修改 C++ 源码或 UE 蓝图。
2. **模拟对向车辆眩光** — Carla 的车灯是静态 emissive mesh + 固定 spotlight，不模拟真实的眩光散射效果。对的来车灯光只是一个光晕，不是真实的配光分布。
3. **精确的色温控制** — WeatherParameters 没有色温参数，只有散射参数 (mie/rayleigh)。车灯色温不可控。
4. **IES 光型文件导入** — Carla 的 streetlight 支持 IES，但 vehicle headlight 不走这个路径，需要改蓝图。

### ✅ 能做的事

1. **日/夜/黄昏切换** — `sun_altitude_angle` 控制，粗细粒度足够
2. **雾/雨/云** — 自然天气条件模拟成熟
3. **车灯开关** — LowBeam / HighBeam / Fog 的 on/off 切换
4. **街灯强度** — 可调数量级环境光
5. **相机传感器受光照影响** — Carla 的 RGB 相机的 autoexposure 会响应环境光照变化，这是 Sim2Real 关键

---

## 三、已存在的 benchmark 项目

### AV-perception-benchmark-Carla ⭐

> https://github.com/Michael-ola/AV-perception-benchmark-Carla

- Carla 0.9.15, Python 3.8
- 模型: YOLOv8 / DETR / Faster R-CNN
- 测试条件: ClearNoon / HeavyRain / DenseFog / Night / RainyNight
- 指标: mAP, precision, recall per condition
- **CPU-only 可跑**（PyTorch 2.4.1+cpu）
- **直接可用** — 改 weather 参数就能扩展实验

### DarkDriving-ICRA-2026 ⭐

> https://github.com/DriveMindLab/DarkDriving-ICRA-2026

- 9538 对日/夜真实场景（ICRA 2026）
- 不是仿真数据，是真实数据集
- **和我们的 Phase B 方向直接对齐** — "夜间感知退化"核心问题

### HG-Lane (CVPR 2026)

- 30,000 张恶劣天气车道线图片（仿真）
- 展示了 Carla 仿真数据可以提升 real-world 模型 mF1 达 20.87%
- **强证据**: Carla 仿真的天气/光照数据可以 bridge Sim2Real gap

---

## 四、结论：Phase 0 可行性评估

### 能做的实验矩阵

| 实验 | Carla 能力 | 难度 |
|---|---|---|
| 晴天 → 夜间 mAP 退化 | ✅ `sun_altitude=-90`, `Night` preset | 低 |
| 雾天影响感知精度 | ✅ `fog_density=50/100` | 低 |
| 雨天 + 夜间组合 | ✅ `HardRainNight` preset | 低 |
| 近光灯 vs 远光灯比较 | ✅ `VehicleLightState.LowBeam/HighBeam` | 中 |
| 雾灯开 vs 关 | ✅ `VehicleLightState.FogLights` | 中 |
| 对向车灯眩光 | ❌ 无物理眩光模型 | 高（需改 UE） |
| 配光曲线级精度 | ❌ 需改 UE 蓝图 | 极高 |
| 色温对比 | ❌ 无 API | 极高 |

### 可行的最小实验

```
Phase 0 可以直接跑:
  Carla Night preset → 记录 YOLOv8 mAP
  Carla Night + HighBeam → 对比 mAP 变化
  Carla HardRainNight → 对比 mAP 变化
  Carla DenseFog → 对比 mAP 变化

→ 输出: 4 个光照条件下 YOLOv8 的感知退化曲线
→ 成本: 无需改 Carla 源码，直接用 AV-perception-benchmark-Carla
```

### 不可行的实验（当前）

```
× 对向远光灯眩光 → 需要 UE 编辑器自定义灯光
× GB 4599 配光曲线级仿真 → 需要 C++ 源码修改
× 自适应前照灯 AFS → Carla 不支持动态光型
```

### 整体评估

**Carla 足够启动 Phase 0。** 虽然无法做配光曲线级精度，但日/夜/雾/雨 + 车灯开关的交叉实验矩阵已经有了足够的差异化光照条件，可以产出一篇有意义的感知退化曲线分析。

**建议路线**:
1. Phase 0: 用 AV-perception-benchmark-Carla 跑 ClearNoon/Night 基线
2. Phase 1: 扩展实验到车灯状态（LowBeam vs HighBeam vs Fog）
3. Phase 2: 评估结果 → 判断是否需要进入 UE Editor 做更高精度

**不需要买 GPU**：AV-perception-benchmark 支持 CPU-only PyTorch。Carla UE4 本身需要 GPU（或软件渲染），但 WSL2 下可以通过 `-RenderOffScreen` 或 Windows 原生运行。
