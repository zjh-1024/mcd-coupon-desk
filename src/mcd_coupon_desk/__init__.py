"""麦麦券务台 —— 麦当劳优惠券与到手价的确定性计算。

模块划分
--------
models   领域模型（金额一律用「分」的整数）
coupons  券可用性判定（给出不可用的具体原因）
solver   方案求解（直选 / 单券 / 叠加 / 拆单）
catalog  数据源适配（离线样例 与 麦当劳 MCP）
report   结果渲染
cli      命令行入口

一句话定位：**券能不能用 · 怎么摆最省 · 现在买还是等。**
"""

from .models import (
    Catalog,
    Coupon,
    Item,
    Line,
    SCOPE_ACCOUNT,
    SCOPE_STORE,
    Verdict,
)
from .coupons import diagnose, evaluate, subtotal_cents
from .solver import OrderPlan, Plan, Solution, minimal_subset_reaching, solve

__version__ = "0.1.0"

__all__ = [
    "Catalog",
    "Coupon",
    "Item",
    "Line",
    "OrderPlan",
    "Plan",
    "SCOPE_ACCOUNT",
    "SCOPE_STORE",
    "Solution",
    "Verdict",
    "__version__",
    "diagnose",
    "evaluate",
    "minimal_subset_reaching",
    "solve",
    "subtotal_cents",
]
