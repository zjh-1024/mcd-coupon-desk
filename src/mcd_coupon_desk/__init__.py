"""麦麦券务台 —— 麦当劳优惠券与到手价的确定性计算。

模块划分
--------
models   领域模型（金额一律用「分」的整数；积分账户、积分商品、建议）
schedule 券的「可用时段」解析与判定（哪几天 + 几点到几点）
coupons  券可用性判定（给出不可用的具体原因）
solver   方案求解（直选 / 单券 / 叠加 / 拆单）
advisor  基于真实账号数据的推荐（积分兑换、到期、时机）
catalog  数据源适配（离线样例 与 麦当劳 MCP）
report   结果渲染
gui      图形界面（tkinter，标准库自带）
cli      命令行入口

一句话定位：**券能不能用 · 怎么摆最省 · 现在买还是等。**
"""

from .advisor import advise, advise_coupons, advise_points
from .coupons import diagnose, evaluate, subtotal_cents
from .models import (
    Advice,
    Catalog,
    Coupon,
    Item,
    Line,
    PointsAccount,
    RewardProduct,
    SCOPE_ACCOUNT,
    SCOPE_STORE,
    Verdict,
)
from .schedule import Period, parse_period
from .solver import OrderPlan, Plan, Solution, minimal_subset_reaching, solve

__version__ = "0.2.0"

__all__ = [
    "Advice",
    "Catalog",
    "Coupon",
    "Item",
    "Line",
    "OrderPlan",
    "Period",
    "Plan",
    "PointsAccount",
    "RewardProduct",
    "SCOPE_ACCOUNT",
    "SCOPE_STORE",
    "Solution",
    "Verdict",
    "__version__",
    "advise",
    "advise_coupons",
    "advise_points",
    "diagnose",
    "evaluate",
    "minimal_subset_reaching",
    "parse_period",
    "solve",
    "subtotal_cents",
]
