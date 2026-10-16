"""PyInstaller 打包入口（仅用于产出 Windows 单文件 exe）。

为什么需要单独一个入口
----------------------
仓库根的 `mcd-coupon-desk.py` 用 `__file__` 定位 `src/` 与 `examples/`。
PyInstaller 单文件模式下这些路径会变（数据被解到 `sys._MEIPASS`），
所以这里显式处理两种形态，而**不改动**正常运行时的源码。

行为
----
    双击 exe（无参数）            → 打开图形界面
    exe demo / exe me / exe plan  → 照常走命令行

构建命令见 `.github/workflows/release.yml` 与 README。
"""

from __future__ import annotations

import pathlib
import sys

# ---- 路径装配：源码运行时把 src/ 加进 sys.path；冻结运行时模块已在 PYZ 里 ----
_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))

from mcd_coupon_desk import cli  # noqa: E402


def _force_utf8_stdio() -> None:
    """Windows 控制台默认回落到 GBK，输出里的 ¥ 会触发 UnicodeEncodeError。

    `--noconsole` 打包时 `sys.stdout` 为 None，这里整体跳过 ——
    注意用 print 输出到 None 是安全的（CPython 的 print 会直接返回）。
    """
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:  # noqa: BLE001 - 纯兼容项，失败不影响功能
            pass
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def _point_to_bundled_sample() -> None:
    """冻结运行时，样例数据在 `_MEIPASS/examples/`，而不是仓库根。"""
    if not getattr(sys, "frozen", False):
        return
    base = pathlib.Path(getattr(sys, "_MEIPASS", _ROOT))
    sample = base / "examples" / "sample_catalog.json"
    if sample.is_file():
        cli.DEFAULT_SAMPLE = sample


if __name__ == "__main__":
    _force_utf8_stdio()
    _point_to_bundled_sample()
    # 双击（无参数）时直接开图形界面；带参数则照常走命令行
    raise SystemExit(cli.main(sys.argv[1:] or ["gui"]))
