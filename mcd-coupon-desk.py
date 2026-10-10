#!/usr/bin/env python3
"""零安装启动器。

直接 `python mcd-coupon-desk.py demo` 即可运行，不需要 pip install、不需要配置环境。
仅依赖 Python 3.10+ 标准库。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

from mcd_coupon_desk.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
