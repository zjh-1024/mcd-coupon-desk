#!/usr/bin/env python3
"""零安装启动器。

直接 `python mcd-coupon-desk.py gui` 打开图形界面，或
`python mcd-coupon-desk.py demo` 在终端里跑一遍 ——
不需要 pip install、不需要配置环境，也不需要申请 Token。仅依赖 Python 3.10+ 标准库。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

from mcd_coupon_desk.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
