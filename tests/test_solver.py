"""核心算法单元测试。

全部使用标准库 unittest，不引入 pytest —— 保持「clone 下来就能跑」。
运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcd_coupon_desk.catalog import OfflineSource, to_cents  # noqa: E402
from mcd_coupon_desk.models import (  # noqa: E402
    REASON_EXPIRED,
    REASON_ITEM_MISMATCH,
    REASON_THRESHOLD,
    REASON_WRONG_STORE,
    Catalog,
    Coupon,
    Item,
    Line,
    SCOPE_STORE,
)
from mcd_coupon_desk.coupons import evaluate  # noqa: E402
from mcd_coupon_desk.solver import (  # noqa: E402
    _collapse,
    _group_of,
    minimal_subset_reaching,
    plan_split,
    plan_stacked,
    solve,
)

SAMPLE = ROOT / "examples" / "sample_catalog.json"


def _item(code: str, name: str, yuan: float, category: str = "", tags: tuple[str, ...] = ()) -> Item:
    return Item(
        code=code,
        name=name,
        price_cents=int(round(yuan * 100)),
        category=category,
        tags=tags,
    )


class TestMoneyUnits(unittest.TestCase):
    """金额单位换算必须无损 —— 比价算错一分钱都是 bug。"""

    def test_float_yuan_to_cents(self):
        self.assertEqual(to_cents(24.5), 2450)
        self.assertEqual(to_cents(9.9), 990)

    def test_int_heuristic(self):
        # 整数：≥100 视为「分」，<100 视为「元」
        self.assertEqual(to_cents(2450), 2450)
        self.assertEqual(to_cents(25), 2500)

    def test_string_with_symbol(self):
        self.assertEqual(to_cents("¥18.00"), 1800)

    def test_float_accumulation_has_no_error(self):
        # 0.1 + 0.2 问题在整数分下不存在
        total = sum(to_cents(x) for x in (0.1, 0.2))
        self.assertEqual(total, 30)


class TestMinimalSubset(unittest.TestCase):
    """凑门槛必须是精确解，不能是贪心近似。"""

    def setUp(self):
        self.units = [
            _item("B001", "巨无霸", 24.5),
            _item("B003", "麦辣鸡腿堡", 22.5),
            _item("S001", "中薯条", 14.0),
            _item("D001", "可口可乐中杯", 10.0),
        ]

    def test_picks_smallest_sum_above_threshold(self):
        subset = minimal_subset_reaching(self.units, 3500)
        self.assertIsNotNone(subset)
        # 2250 + 1400 = 3650 是 ≥3500 的最小可行和（不是贪心的 4700）
        self.assertEqual(sum(u.price_cents for u in subset), 3650)

    def test_returns_empty_for_no_threshold(self):
        self.assertEqual(minimal_subset_reaching(self.units, 0), [])

    def test_returns_none_when_unreachable(self):
        self.assertIsNone(minimal_subset_reaching(self.units, 99999))

    def test_exact_boundary(self):
        subset = minimal_subset_reaching(self.units, 3450)
        self.assertEqual(sum(u.price_cents for u in subset), 3450)


class TestCouponAvailability(unittest.TestCase):
    """不可用必须给出具体原因，不能只说「不能用」。"""

    def setUp(self):
        self.cart = [
            Line(_item("B001", "巨无霸", 24.5)),
            Line(_item("D001", "可口可乐中杯", 10.0), qty=2),
        ]  # 合计 4450 分

    def test_expired(self):
        coupon = Coupon("X1", "过期券", 800, 2000, valid_to="2020-01-01")
        verdict = evaluate(coupon, self.cart, "SH0001")
        self.assertFalse(verdict.usable)
        self.assertEqual(verdict.reason_code, REASON_EXPIRED)

    def test_threshold_shortfall_reported(self):
        coupon = Coupon("X2", "满100减50", 5000, 10000)
        verdict = evaluate(coupon, self.cart, "SH0001")
        self.assertEqual(verdict.reason_code, REASON_THRESHOLD)
        self.assertEqual(verdict.shortfall_cents, 10000 - 4450)

    def test_item_tag_mismatch(self):
        coupon = Coupon("X3", "早餐券", 600, 0, item_tags=("早餐",))
        verdict = evaluate(coupon, self.cart, "SH0001")
        self.assertEqual(verdict.reason_code, REASON_ITEM_MISMATCH)

    def test_store_coupon_rejected_at_other_store(self):
        coupon = Coupon("X4", "他店专享", 1000, 0, store_code="BJ9999")
        verdict = evaluate(coupon, self.cart, "SH0001")
        self.assertEqual(verdict.reason_code, REASON_WRONG_STORE)

    def test_saving_never_exceeds_cart(self):
        coupon = Coupon("X5", "超额券", 99999, 0)
        verdict = evaluate(coupon, self.cart, "SH0001")
        self.assertTrue(verdict.usable)
        self.assertEqual(verdict.saving_cents, 4450)


class TestSolverAgainstFixture(unittest.TestCase):
    """端到端：用离线样例跑出四套方案并验证最优解。"""

    @classmethod
    def setUpClass(cls):
        source = OfflineSource(SAMPLE)
        catalog: Catalog = source.menu()
        cls.store_code = source.store_code
        cls.coupons = source.coupons()
        cls.cart = [
            Line(catalog.find("B001")),   # 巨无霸 2450
            Line(catalog.find("B003")),   # 麦辣鸡腿堡 2250
            Line(catalog.find("S001")),   # 中薯条 1400
            Line(catalog.find("D001")),   # 可口可乐中杯 1000
        ]
        cls.solution = solve(cls.cart, cls.coupons, cls.store_code)

    def test_cart_subtotal(self):
        self.assertEqual(self.solution.baseline.total_cents, 7100)

    def test_stacked_plan(self):
        plan = next(p for p in self.solution.plans if p.label == "多券叠加")
        # 满减组最优是「满50减25」，品类组是「饮品立减5」
        self.assertEqual(plan.saving_cents, 3000)
        self.assertEqual(plan.total_cents, 4100)

    def test_split_beats_stacked(self):
        stacked = next(p for p in self.solution.plans if p.label == "多券叠加")
        split = next(p for p in self.solution.plans if p.label == "拆单最优")
        self.assertLess(split.total_cents, stacked.total_cents)

    def test_split_uses_two_orders(self):
        plan = plan_split(self.cart, self.solution.verdicts)
        self.assertIsNotNone(plan)
        with_coupon = [o for o in plan.orders if o.coupons]
        self.assertEqual(len(with_coupon), 2)

    def test_best_plan_is_split(self):
        self.assertEqual(self.solution.best.label, "拆单最优")
        self.assertEqual(self.solution.best.total_cents, 3600)
        self.assertEqual(self.solution.saving_vs_baseline_cents, 3500)

    def test_all_four_plans_present(self):
        labels = {p.label for p in self.solution.plans}
        self.assertEqual(labels, {"原价直选", "单券最优", "多券叠加", "拆单最优"})

    def test_verdicts_explain_every_blocked_coupon(self):
        blocked = [v for v in self.solution.verdicts if not v.usable]
        self.assertTrue(blocked)
        for verdict in blocked:
            self.assertTrue(verdict.reason, f"{verdict.coupon.coupon_id} 缺少不可用原因")


class TestStackingConservatism(unittest.TestCase):
    """未标组的券默认视为互斥 —— 宁可少算，不可虚报。"""

    def setUp(self):
        self.cart = [Line(_item("B001", "巨无霸", 24.5)), Line(_item("B003", "麦辣", 22.5))]
        self.coupons = [
            Coupon("A", "券A", 1000, 0),
            Coupon("B", "券B", 900, 0),
        ]

    def _verdicts(self):
        return [evaluate(c, self.cart, "SH0001") for c in self.coupons]

    def test_default_treats_ungrouped_as_exclusive(self):
        self.assertIsNone(plan_stacked(self.cart, self._verdicts()))

    def test_assume_stackable_allows_stacking(self):
        plan = plan_stacked(self.cart, self._verdicts(), assume_stackable=True)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.saving_cents, 1900)


# ---------------------------------------------------------------- 暴力枚举 oracle

def _all_set_partitions(n: int) -> list[tuple[int, ...]]:
    """枚举 n 个元素的全部集合划分（受限增长串表示）。

    长度为 n 的元组里，第 i 个位置的值是「元素 i 归属的订单编号」，
    编号从 0 连续递增，因此每种划分只出现一次。
    """
    result: list[tuple[int, ...]] = []
    blocks = [0] * n

    def rec(i: int, k: int) -> None:
        if i == n:
            result.append(tuple(blocks))
            return
        for b in range(k):
            blocks[i] = b
            rec(i + 1, k)
        blocks[i] = k
        rec(i + 1, k + 1)

    rec(0, 0)
    return result


def _exhaustive_best_total(
    lines: list[Line],
    coupons: list[Coupon],
    store_code: str,
    now: datetime | None = None,
) -> int:
    """暴力枚举真实最低到手价（分）。

    枚举「购物车的所有拆单划分 × 每张券的指派（用在哪笔订单 / 不用）」，
    逐张券用 `coupons.evaluate` 在目标订单上校验（有效期 / 门店 / 门槛 / 适用商品），
    同一笔订单内再按互斥组去重（与求解器同一套 `_group_of` 规则）。
    结果只用于对拍，作为「真实可达最低价」的独立下界。
    """
    units = [ln.item for ln in lines for _ in range(ln.qty)]
    n = len(units)
    best: int | None = None

    for blocks in _all_set_partitions(n):
        order_count = (max(blocks) + 1) if blocks else 0
        order_units: list[list[Item]] = [[] for _ in range(order_count)]
        for unit, b in zip(units, blocks):
            order_units[b].append(unit)
        order_lines = [_collapse(u) for u in order_units]
        order_subtotals = [sum(it.price_cents for it in u) for u in order_units]

        # 每张券：-1 = 不用；否则放进第 oi 笔订单
        for assign in product(range(-1, order_count), repeat=len(coupons)):
            per_order: list[list[Coupon]] = [[] for _ in range(order_count)]
            for ci, oi in enumerate(assign):
                if oi >= 0:
                    per_order[oi].append(coupons[ci])

            valid = True
            total_saving = 0
            for oi in range(order_count):
                groups: set[str] = set()
                raw_saving = 0
                for coupon in per_order[oi]:
                    group = _group_of(coupon, False)
                    if group in groups:          # 同组互斥
                        valid = False
                        break
                    groups.add(group)
                    verdict = evaluate(coupon, order_lines[oi], store_code, now)
                    if not verdict.usable:       # 门槛 / 适用商品 / 门店 / 时效
                        valid = False
                        break
                    raw_saving += verdict.saving_cents
                if not valid:
                    break
                total_saving += min(raw_saving, order_subtotals[oi])
            if valid:
                total = sum(order_subtotals) - total_saving
                if best is None or total < best:
                    best = total

    assert best is not None, "oracle 至少应找到「全部原价、不用券」这一可行解"
    return best


class TestSplitPlanFeasibility(unittest.TestCase):
    """拆单可行性：每张券必须落在**真正能用它**的订单上，且不得虚报节省额。

    这组测试针对一个已复现的真实缺陷：拆单挂载此前只校验互斥组、不校验门槛与
    适用商品，导致「限饮品」的券被挂到没有饮品的订单上，报出比真实最优更低的
    到手价（高估节省）。测试 #1 是根治回归的主锁，测试 #4 用暴力枚举对拍兜底。
    """

    @classmethod
    def setUpClass(cls):
        source = OfflineSource(SAMPLE)
        catalog: Catalog = source.menu()
        cls.store_code = source.store_code
        cls.coupons = source.coupons()
        cls.cart = [
            Line(catalog.find("B001")),   # 巨无霸
            Line(catalog.find("B003")),   # 麦辣鸡腿堡
            Line(catalog.find("S001")),   # 中薯条
            Line(catalog.find("D001")),   # 可口可乐中杯（带「饮品」标签）
        ]
        cls.now = datetime(2026, 10, 16, 12, 0, 0)
        cls.solution = solve(cls.cart, cls.coupons, cls.store_code, cls.now)

    def test_every_applied_coupon_is_valid_on_its_order(self):
        # 主锁：任意方案里被挂上的每一张券，都必须在**它所在订单**上重新判定为
        # 可用（门槛 / 适用商品 / 门店 / 时效），且省额与现算结果一致
        # （固定价券的省额随该单商品原价变化，不能用全车工况下的旧结论）。
        checked = 0
        for plan in self.solution.plans:
            for order in plan.orders:
                names = [ln.item.name for ln in order.lines]
                for applied in order.applied:
                    verdict = evaluate(applied.coupon, order.lines, self.store_code, self.now)
                    self.assertTrue(
                        verdict.usable,
                        f"「{applied.coupon.name}」被挂在 {names}，"
                        f"但该单不满足其门槛/商品约束：{verdict.reason}",
                    )
                    self.assertEqual(
                        applied.saving_cents, verdict.saving_cents,
                        f"「{applied.coupon.name}」在 {names} 上的省额与现算不一致",
                    )
                    checked += 1
        self.assertGreater(checked, 0, "样例下应至少有一张券被使用")

    def test_drink_coupon_lands_on_the_order_with_the_drink(self):
        split = next(p for p in self.solution.plans if p.label == "拆单最优")
        drink_name = "可口可乐中杯"

        carriers = [
            o for o in split.orders
            if any(a.coupon.coupon_id == "C003" for a in o.applied)
        ]
        self.assertEqual(len(carriers), 1, "「饮品立减5元」应恰好挂在一笔订单上")
        carrier_names = [ln.item.name for ln in carriers[0].lines]
        self.assertIn(drink_name, carrier_names)

        for order in split.orders:
            if order is carriers[0]:
                continue
            self.assertNotIn(
                "C003", [a.coupon.coupon_id for a in order.applied],
                f"「饮品立减5元」不应挂在没有饮品的订单 {[ln.item.name for ln in order.lines]} 上",
            )

    def test_item_restricted_threshold_coupon_gets_applicable_items(self):
        # 「满 15 减 15，限主食」只能被分到含主食的订单上凑门槛，
        # 不能因为两杯饮品合计更高就被塞到饮品订单里。
        staple = _item("B900", "测试主食", 20.0, "主食", ("主食",))
        drink = _item("D900", "测试饮品", 8.0, "饮品", ("饮品",))
        cart = [Line(staple), Line(drink), Line(drink)]           # 主食 2000 + 饮品 800×2

        coupons = [
            Coupon("RT", "满15减15 限主食", 1500, 1500, item_tags=("主食",)),
            Coupon("RA", "满15减15 不限", 1500, 1500),
        ]
        now = datetime(2026, 10, 16, 12, 0, 0)
        verdicts = [evaluate(c, cart, "SH0001", now) for c in coupons]

        plan = plan_split(cart, verdicts, store_code="SH0001", now=now)
        self.assertIsNotNone(plan)

        carriers = [
            o for o in plan.orders
            if any(a.coupon.coupon_id == "RT" for a in o.applied)
        ]
        self.assertEqual(len(carriers), 1, "受限券应恰好落在一笔订单上")
        order = carriers[0]

        applicable = [ln for ln in order.lines if "主食" in ln.item.tags]
        self.assertTrue(
            applicable,
            f"受限券所在订单必须含适用商品（主食），实际为 {[ln.item.name for ln in order.lines]}",
        )
        applicable_subtotal = sum(ln.amount_cents for ln in applicable)
        self.assertGreaterEqual(applicable_subtotal, 1500)

    def test_total_never_beats_exhaustive_optimum(self):
        # 构造用例（与复核报告一致）：固定价券若被挂到错误订单上，
        # 求解器会报出比真实可达最低价更低的到手价。
        side = _item("S950", "测试配餐", 5.0, "配餐", ("配餐",))
        drink = _item("D950", "测试饮品", 12.0, "饮品", ("饮品",))
        staple = _item("B950", "测试主食", 20.0, "主食", ("主食",))
        cart = [Line(staple), Line(drink), Line(side)]

        coupons = [
            Coupon("G1", "满20减15", 1500, 2000, group="g1"),
            Coupon("G2", "满15减15", 1500, 1500),                                   # 未标组
            Coupon("DRINK", "饮品固定价3元", 0, 0, item_tags=("饮品",), fixed_price_cents=300),
            Coupon("SIDE", "配餐固定价1元", 0, 0, item_tags=("配餐",), fixed_price_cents=100),
        ]
        store = "SH0001"
        now = datetime(2026, 10, 16, 12, 0, 0)

        oracle = _exhaustive_best_total(cart, coupons, store, now)
        # oracle 自证：该构造用例的真实最优到手价 = 主食单(满20减15 → ¥5.00 券后)…
        # 见下方断言 —— 若 oracle 本身算错，这一步会先失败。
        self.assertEqual(oracle, 300, "oracle 结果与人工核算的 ¥3.00 不一致")

        solution = solve(cart, coupons, store, now)
        self.assertGreaterEqual(
            solution.best.total_cents,
            oracle,
            f"求解器报出到手价 {solution.best.total_cents} 分，"
            f"低于真实可达最低价 {oracle} 分 —— 高估了节省额（方案不可行）",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
