"""结果渲染。

设计原则：**每一个数字都要能追溯到来源**。
方案里的金额全部由本地确定性算法算出，并标注可被官方 calculate-price 复核；
券的不可用原因必须写明，不允许出现「无法使用」这种无信息量的结论。
"""

from __future__ import annotations

import unicodedata

from .models import Line, Verdict
from .solver import Plan, Solution


def yuan(cents: int) -> str:
    """分 → 带符号的元，保留两位小数。"""
    sign = "-" if cents < 0 else ""
    return f"{sign}¥{abs(cents) / 100:.2f}"


def display_width(text: str) -> int:
    """终端显示宽度：中文、全角标点占 2 列，其余占 1 列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def pad(text: str, width: int, align: str = "left") -> str:
    """按显示宽度补齐，保证中文列对得齐。"""
    gap = max(0, width - display_width(text))
    filler = " " * gap
    return filler + text if align == "right" else text + filler


def _rule(char: str = "─", width: int = 58) -> str:
    return char * width


def render_cart(lines: list[Line]) -> str:
    if not lines:
        return "（空购物车）"
    parts = [f"{ln.item.name} ×{ln.qty}" for ln in lines]
    return " + ".join(parts)


def render_verdicts(verdicts: list[Verdict]) -> str:
    usable = [v for v in verdicts if v.usable]
    blocked = [v for v in verdicts if not v.usable]

    out = ["券包体检", _rule()]
    out.append(f"共 {len(verdicts)} 张 · 可用 {len(usable)} 张 · 不可用 {len(blocked)} 张")

    if usable:
        out.append("")
        out.append("可用（按节省额降序）")
        for v in usable:
            out.append(f"  · {v.coupon.name}　省 {yuan(v.saving_cents)}")
            if v.coupon.threshold_cents:
                out.append(f"      门槛 {yuan(v.coupon.threshold_cents)}　渠道 {'门店券' if v.coupon.scope == 'store' else '账号券'}")

    if blocked:
        out.append("")
        out.append("不可用（附具体原因）")
        for v in blocked:
            out.append(f"  · {v.coupon.name}　{v.reason}")

    return "\n".join(out)


def render_plan_table(solution: Solution) -> str:
    out = ["方案对比", _rule()]
    out.append(
        pad("方案", 12) + pad("订单数", 8, "right") + pad("小计", 12, "right")
        + pad("节省", 12, "right") + pad("到手价", 13, "right")
    )
    for p in solution.plans:
        flag = "  ← 最优" if p is solution.best else ""
        out.append(
            pad(p.label, 12)
            + pad(str(len(p.orders)), 8, "right")
            + pad(yuan(p.subtotal_cents), 12, "right")
            + pad(yuan(p.saving_cents), 12, "right")
            + pad(yuan(p.total_cents), 13, "right")
            + flag
        )
    return "\n".join(out)


def render_plan_detail(plan: Plan) -> str:
    if not plan.orders:
        return "（无订单）"

    out = [f"最优方案明细 · {plan.label}", _rule()]
    for idx, order in enumerate(plan.orders, start=1):
        title = f"第 {idx} 单" if plan.is_split else "本单"
        out.append(f"{title}　{render_cart(order.lines)}")
        if order.applied:
            for item in order.applied:
                out.append(f"    用券　{item.coupon.name}（−{yuan(item.saving_cents)}）")
        else:
            out.append("    用券　（无）")
        out.append(
            f"    小计 {yuan(order.subtotal_cents)}　"
            f"节省 {yuan(order.saving_cents)}　实付 {yuan(order.total_cents)}"
        )
        if idx < len(plan.orders):
            out.append("")

    for note in plan.notes:
        out.append(f"※ {note}")
    return "\n".join(out)


def render_summary(solution: Solution) -> str:
    baseline = solution.baseline
    out = ["结论", _rule()]
    if solution.saving_vs_baseline_cents <= 0:
        out.append(f"当前购物车暂无可用优惠，到手价 {yuan(solution.best.total_cents)}。")
        return "\n".join(out)
    out.append(
        f"按「{solution.best.label}」下单，到手价 {yuan(solution.best.total_cents)}，"
        f"比原价省 {yuan(solution.saving_vs_baseline_cents)}"
        f"（{solution.saving_vs_baseline_pct:.1f}%）。"
    )
    return "\n".join(out)


def render_solution(solution: Solution, store_label: str = "", lines: list[Line] | None = None) -> str:
    """把一次求解渲染成完整报告。"""
    blocks: list[str] = []
    blocks.append(render_summary(solution))
    blocks.append("")
    blocks.append("购物车")
    blocks.append(_rule())
    blocks.append(render_cart(lines or []))
    if store_label:
        blocks.append(f"门店：{store_label}")
    blocks.append("")
    blocks.append(render_plan_table(solution))
    blocks.append("")
    blocks.append(render_plan_detail(solution.best))
    blocks.append("")
    blocks.append(render_verdicts(solution.verdicts))
    blocks.append("")
    blocks.append(_rule())
    blocks.append("金额口径：全部为「分」的整数运算；外卖配送费不由本地估算，")
    blocks.append("最终价格请以官方 calculate-price 与订单页为准。")
    return "\n".join(blocks)


__all__ = [
    "render_cart",
    "render_plan_detail",
    "render_plan_table",
    "render_solution",
    "render_summary",
    "render_verdicts",
    "yuan",
]
