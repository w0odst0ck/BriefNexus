#!/usr/bin/env python3
"""平台启动入口 — `python platform/run.py`(或 `python -m platform.run`)

为什么需要专门入口:
  项目根下的 platform/ 包与 Python 标准库 platform 同名。直接用
  `uvicorn platform.app:app` / `PYTHONPATH=. uvicorn ...` 启动都会失败:
  - 不加 PYTHONPATH: import platform 解析到标准库 → 找不到 platform.app
  - 加 PYTHONPATH:     import platform 解析到本项目包 → uvicorn 依赖的
                       uuid/click 调用 platform.system() 崩溃
  本入口先加载依赖标准库 platform 的模块(uuid 等),再弹出标准库缓存、
  挂载项目包,最后以编程方式启动 uvicorn(不通过模块字符串重新 import)。
"""
import os

# 1. 先加载依赖标准库 platform 的模块(此时项目根尚未进入 sys.path)
#    uuid 为副作用导入: 预加载后其内部 platform.system() 已用标准库解析,
#    后续 uvicorn/click 复用缓存,不会因项目包遮蔽而崩溃。
import platform as _stdlib_platform  # 标准库 platform
import sys
import uuid  # noqa: F401

# 2. 项目根进入 sys.path,弹出标准库 platform 缓存,使 import platform 解析到本项目包
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if sys.modules.get("platform") is _stdlib_platform:
    sys.modules.pop("platform", None)

# 3. 挂载项目包后再启动 uvicorn
from platform import config
from platform.app import app

import uvicorn

if __name__ == "__main__":
    server = config.get_server_config()
    uvicorn.run(app, host=server["host"], port=server["port"])
