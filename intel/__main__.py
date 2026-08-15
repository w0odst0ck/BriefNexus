"""intel 包入口 — 使 `python -m intel <子命令>` 可用"""
import sys

from intel.cli import main

if __name__ == "__main__":
    sys.exit(main())
