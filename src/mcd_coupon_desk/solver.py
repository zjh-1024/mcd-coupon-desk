"""点餐方案求解器。

问题定义
--------
给定：一个购物车（若干商品 × 数量）、一个券包、一个门店、一个时间点。
求：在合规前提下「到手价最低」的点餐方案。

三类方案分开算，再横向比：
  A 原价直选     —— 一把梭，不用券
  B 单券最优     —— 只挑一张券，但挑最狠的那张
  C 多券叠加     —— 按互斥组各取一张，叠加使用
  D 拆单最优     —— 把购物车拆成多笔订单，每笔各用一张券

关于拆单
--------
拆单能以小吃大（例如两张「满 30 减 15」用在同一桌），但选哪几件去凑门槛是
「最小子集和 ≥ 门槛」问题。本项目用一次 0/1 背包 DP 求**精确解**，
不是贪心近似 —— 差别在于能否证明「这就是最省的凑单方式」。

关于叠加的保守假设
------------------
真实叠加规则以麦当劳官方为准，本项目不臆测。券的 group 字段若为空，
默认视为**互斥**（共享同一个默认组），因为高估节省额比低估更糟：
用户会按错的方案去下单。需要放宽时用 assume_stackable=True。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .coupons import _match_items, diagnose, evaluate, subtotal_cents
from .models import Coupon, Item, Line, Verdict

# 空 group 的券归入这个默认互斥组
DEFAULT_GROUP = "__default__"


# ---------------------------------------------------------------- 方案数据结构

@dataclass
class Applied:
    """「某张券用在这笔订单上，省下多少钱」。

    为什么不能直接用 `Coupon.discount_cents`：麦当劳的券大多是「指定商品按固定价购买」，
    券本身不带折扣金额，省额必须拿商品原价现算。
    所以省额是**评估的产物**，不是券的固有属性。
    """

    coupon: Coupon
    saving_cents: int


@dataclass
class OrderPlan:
    """一笔订单。"""

    lines: list[Line] = field(default_factory=list)
    applied: list[Applied] = field(default_factory=list)

    @property
    def subtotal_cents(self) -> int:
        return subtotal_cents(self.lines)

    @property
    def coupons(self) -> list[Coupon]:
        return [a.coupon for a in self.applied]

    @property
    def saving_cents(self) -> int:
        raw = sum(a.saving_cents for a in self.applied)
        # 减免不得超过订单本身金额
        return min(raw, self.subtotal_cents)

    @property
    def total_cents(self) -> int:
        return self.subtotal_cents - self.saving_cents

    @property
    def coupon_names(self) -> list[str]:
        return [a.coupon.name for a in self.applied]


@dataclass
class Plan:
    """由一或多笔订单构成的完整方案（拆单时 orders 长度 > 1）。"""

    label: str
    orders: list[OrderPlan] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def subtotal_cents(self) -> int:
        return sum(o.subtotal_cents for o in self.orders)

    @property
    def saving_cents(self) -> int:
        return sum(o.saving_cents for o in self.orders)

    @property
    def total_cents(self) -> int:
        return self.subtotal_cents - self.saving_cents

    @property
    def saving_pct(self) -> float:
        if self.subtotal_cents <= 0:
            return 0.0
        return self.saving_cents / self.subtotal_cents * 100

    @property
    def is_split(self) -> bool:
        return len(self.orders) > 1


# ---------------------------------------------------------------- 基础工具

def _expand(lines: list[Line]) -> list[Item]:
    """把带数量的行摊平成单品列表（拆单与凑单都在单品粒度上做）。"""
    units: list[Item] = []
    for ln in lines:
        units.extend([ln.item] * ln.qty)
    return units


def _collapse(units: list[Item]) -> list[Line]:
    """把单品列表合并回带数量的行，保持首次出现顺序。"""
    order: list[Item] = []
    counts: dict[str, int] = {}
    by_code: dict[str, Item] = {}
    for it in units:
        if it.code not in counts:
            counts[it.code] = 0
            order.append(it)
            by_code[it.code] = it
        counts[it.code] += 1
    return [Line(by_code[it.code], counts[it.code]) for it in order]


def minimal_subset_reaching(units: list[Item], target_cents: int) -> list[Item] | None:
    """求总价 ≥ target_cents 的**金额最小**子集；无解返回 None。

    target_cents <= 0 时返回空列表（无门槛券不需要凑单）。

    实现为 0/1 背包 + 可达和追踪：dp[s] 记录「首个凑出金额 s 的商品下标」，
    只写一次不覆盖，因此可安全回溯出唯一路径。时间复杂度 O(n × 总金额)。
    这在真实点餐规模（单品 ≤ 数百件、总价 ≤ 四位数）下是毫秒级。
    """
    if target_cents <= 0:
        return []
    total = sum(u.price_cents for u in units)
    if total < target_cents:
        return None

    dp: list[int] = [-2] * (total + 1)   # -2 不可达，-1 表示金额 0
    dp[0] = -1
    for idx, unit in enumerate(units):
        price = unit.price_cents
        if price <= 0:
            continue
        for s in range(total - price, -1, -1):
            if dp[s] != -2 and dp[s + price] == -2:
                dp[s + price] = idx

    for s in range(target_cents, total + 1):
        if dp[s] == -2:
            continue
        chosen: list[Item] = []
        cursor = s
        while cursor > 0:
            idx = dp[cursor]
            chosen.append(units[idx])
            cursor -= units[idx].price_cents
        return chosen
    return None


def _group_of(coupon: Coupon, assume_stackable: bool) -> str:
    if coupon.group:
        return coupon.group
    return coupon.coupon_id if assume_stackable else DEFAULT_GROUP


# ---------------------------------------------------------------- 各方案求解

def plan_baseline(lines: list[Line]) -> Plan:
    """方案 A：不使用任何优惠券。作为一切对比的基准线。"""
    return Plan(label="原价直选", orders=[OrderPlan(lines=list(lines))])


def plan_single_best(
    lines: list[Line],
    verdicts: list[Verdict],
) -> Plan | None:
    """方案 B：只使用一张券，取节省最多的一张。"""
    usable = [v for v in verdicts if v.usable]
    if not usable:
        return None
    best = max(usable, key=lambda v: v.saving_cents)
    return Plan(
        label="单券最优",
        orders=[OrderPlan(lines=list(lines), applied=[Applied(best.coupon, best.saving_cents)])],
        notes=[f"选用「{best.coupon.name}」，单券节省 ¥{best.saving_cents / 100:.2f}"],
    )


def plan_stacked(
    lines: list[Line],
    verdicts: list[Verdict],
    assume_stackable: bool = False,
) -> Plan | None:
    """方案 C：按互斥组各取一张，叠加使用。"""
    usable = [v for v in verdicts if v.usable]
    if not usable:
        return None

    best_per_group: dict[str, Verdict] = {}
    for v in usable:
        key = _group_of(v.coupon, assume_stackable)
        cur = best_per_group.get(key)
        if cur is None or v.saving_cents > cur.saving_cents:
            best_per_group[key] = v

    picked = list(best_per_group.values())
    if len(picked) <= 1:
        # 只有一张能上，等价于方案 B
        return None

    warnings = [] if assume_stackable else [
        "同组券按互斥处理；若官方允许叠加，可用 --assume-stackable 放宽"
    ]
    notes = [f"叠加 {len(picked)} 张券"] + warnings
    return Plan(
        label="多券叠加",
        orders=[OrderPlan(
            lines=list(lines),
            applied=[Applied(v.coupon, v.saving_cents) for v in picked],
        )],
        notes=notes,
    )


def _can_attach(order: OrderPlan, coupon: Coupon, assume_stackable: bool) -> bool:
    """判断一张券能否再加进已有订单（不违反互斥组规则）。"""
    target = _group_of(coupon, assume_stackable)
    for existing in order.coupons:
        if _group_of(existing, assume_stackable) == target:
            return False
    return True


def _split_with_ordering(
    lines: list[Line],
    usable: list[Verdict],
    assume_stackable: bool,
    priority,
    store_code: str = "",
    now: datetime | None = None,
) -> Plan | None:
    """按给定优先级做一次拆单，返回方案或 None。

    关键点：每张券最终落在哪笔订单上，都由 `coupons.evaluate` 在**那笔订单**的
    商品组合上重新判定（门槛 / 适用商品 / 门店 / 时效），不复用「全车工况」下的
    旧结论 —— 否则会把「限饮品」的券挂到没有饮品的订单上，算出虚假节省额。
    重新判定的省额也必须用该订单现算（固定价券的省额取决于该单里的商品原价）。
    """
    remaining = _expand(lines)
    orders: list[OrderPlan] = []
    used_names: list[str] = []
    floating: list[Verdict] = []

    for verdict in sorted(usable, key=priority):
        coupon = verdict.coupon
        if not remaining:
            floating.append(verdict)
            continue

        # 受限券（限商品编码 / 限品类）只能在**适用商品**里凑单，
        # 否则会生成一笔根本不满足该券商品约束的订单。
        pool = remaining
        if coupon.item_codes or coupon.item_tags:
            pool = [unit for unit in remaining if _match_items(coupon, [Line(unit, 1)])]
            if not pool:
                continue          # 剩余商品里没有适用商品，这张券本轮放弃

        subset = minimal_subset_reaching(pool, coupon.threshold_cents)
        if subset is None:
            continue          # 余量不够凑门槛，这张券在本轮放弃
        if not subset:
            floating.append(verdict)   # 无门槛券，不必独占一笔订单
            continue

        order_lines = _collapse(subset)
        fresh = evaluate(coupon, order_lines, store_code, now)
        if not fresh.usable:
            # 理论上不会走到（池子已按约束过滤）；保守起见放弃，不虚报。
            continue
        for unit in subset:
            remaining.remove(unit)
        orders.append(OrderPlan(lines=order_lines, applied=[Applied(coupon, fresh.saving_cents)]))
        used_names.append(coupon.name)

    if not orders:
        return None

    # 剩余商品的订单必须**先**纳入候选，floating 券才能看到
    # 「唯一含适用商品」的那笔订单，否则只能挂到错误的订单上。
    if remaining:
        orders.append(OrderPlan(lines=_collapse(remaining)))

    # 无门槛券（含「受限但无门槛」型）挂到**第一笔能真正使用它**的订单上：
    # 候选订单按小计降序逐个尝试，同时满足「互斥组允许」与「按该单重新判定可用」
    # 才落点；省额用该订单现算。全都通不过就本轮放弃（宁可少算，不虚报节省额）。
    for verdict in floating:
        for host in sorted(orders, key=lambda o: o.subtotal_cents, reverse=True):
            if not _can_attach(host, verdict.coupon, assume_stackable):
                continue
            fresh = evaluate(verdict.coupon, host.lines, store_code, now)
            if not fresh.usable:
                continue
            host.applied.append(Applied(verdict.coupon, fresh.saving_cents))
            used_names.append(verdict.coupon.name)
            break

    if sum(1 for o in orders if o.coupons) < 2:
        return None           # 只落到一笔订单上，谈不上拆单

    return Plan(
        label="拆单最优",
        orders=orders,
        notes=[f"拆成 {len(orders)} 笔订单，分别使用：{'、'.join(used_names)}"],
    )


def plan_split(
    lines: list[Line],
    verdicts: list[Verdict],
    assume_stackable: bool = False,
    store_code: str = "",
    now: datetime | None = None,
) -> Plan | None:
    """方案 D：拆单。把购物车拆成多笔订单，各自核销一张券。

    为什么拆单能更省：券的门槛是**按单**判定的，同一张券在一笔订单上只能用一次。
    把商品分成两笔，就能让两张「满减券」都生效 —— 这是合并下单做不到的。

    「选哪几件去凑门槛」是「最小子集和 ≥ 门槛」问题，由
    `minimal_subset_reaching` 用背包 DP 求精确解。

    至于「先给哪张券分配商品」属于集合划分问题（指数级），
    这里不做完整枚举，而是换几种排序各跑一遍取最优：
      · 按减免额降序 —— 优先吃大券
      · 按减免率降序（减免 ÷ 门槛）—— 优先留出余量给后续小券
      · 按门槛升序 —— 优先满足容易达成的券
    实测中「减免率」在多数场景最优，多跑两种只是兜底，代价可忽略。

    每条券的落点都会用 `coupons.evaluate` 在**目标订单**上重新判定，
    因此 `store_code` 与 `now` 必须一路传下来。
    """
    usable = [v for v in verdicts if v.usable]
    if len(usable) < 2:
        return None

    orderings = (
        lambda v: -v.coupon.discount_cents,
        lambda v: -(v.coupon.discount_cents / max(v.coupon.threshold_cents, 1)),
        lambda v: v.coupon.threshold_cents,
    )

    candidates: list[Plan] = []
    for priority in orderings:
        plan = _split_with_ordering(lines, usable, assume_stackable, priority, store_code, now)
        if plan is not None:
            candidates.append(plan)

    if not candidates:
        return None
    return min(candidates, key=lambda p: p.total_cents)


# ---------------------------------------------------------------- 汇总

@dataclass
class Solution:
    """一次完整求解的产出。"""

    verdicts: list[Verdict]
    plans: list[Plan]
    best: Plan

    @property
    def baseline(self) -> Plan:
        return next(p for p in self.plans if p.label == "原价直选")

    @property
    def saving_vs_baseline_cents(self) -> int:
        return self.baseline.total_cents - self.best.total_cents

    @property
    def saving_vs_baseline_pct(self) -> float:
        if self.baseline.total_cents <= 0:
            return 0.0
        return self.saving_vs_baseline_cents / self.baseline.total_cents * 100


def solve(
    lines: list[Line],
    coupons: list[Coupon],
    store_code: str = "",
    now: datetime | None = None,
    assume_stackable: bool = False,
) -> Solution:
    """求解完整方案集合，返回按到手价升序排列的结果与最优方案。"""
    verdicts = diagnose(coupons, lines, store_code, now)

    candidates: list[Plan] = [plan_baseline(lines)]
    makers = (
        lambda: plan_single_best(lines, verdicts),
        lambda: plan_stacked(lines, verdicts, assume_stackable),
        lambda: plan_split(lines, verdicts, assume_stackable, store_code, now),
    )
    for maker in makers:
        plan = maker()
        if plan is not None:
            candidates.append(plan)

    # 去重：到手价、订单笔数、所用券三者都相同才算重复方案
    seen: set[tuple] = set()
    unique: list[Plan] = []
    for p in candidates:
        signature = (
            p.total_cents,
            len(p.orders),
            tuple(sorted(c.coupon_id for o in p.orders for c in o.coupons)),
        )
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(p)

    unique.sort(key=lambda p: (p.total_cents, not p.is_split))
    return Solution(verdicts=verdicts, plans=unique, best=unique[0])


__all__ = [
    "DEFAULT_GROUP",
    "Applied",
    "OrderPlan",
    "Plan",
    "Solution",
    "minimal_subset_reaching",
    "plan_baseline",
    "plan_single_best",
    "plan_split",
    "plan_stacked",
    "solve",
]
