"""核心算法单元测试。

全部使用标准库 unittest，不引入 pytest —— 保持「clone 下来就能跑」。
运行：python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
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
    minimal_subset_reaching,
    plan_split,
    plan_stacked,
    solve,
)

SAMPLE = ROOT / "examples" / "sample_catalog.json"


def _item(code: str, name: str, yuan: float) -> Item:
    return Item(code=code, name=name, price_cents=int(round(yuan * 100)))


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
