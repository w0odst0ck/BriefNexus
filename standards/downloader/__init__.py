"""
standards/downloader/ — 浏览器自动化标准下载

依赖: playwright (python3 -m playwright install chromium)

使用流程:
  1. 首次使用: 先运行 `python -m standards.downloader.auth` 登录夸克网盘
  2. 查找标准: 运行 `python -m standards.downloader.sync --discover` 扫描 bzxz 列表页
  3. 下载 PDF: 运行 `python -m standards.downloader.download --all` 或 `--std GB/T 39394-2020`
"""
