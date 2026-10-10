"""券解析与「固定价券」判定的单元测试。

样本全部取自 2026-10-10 对真实接口的实测返回，固定住解析行为。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcd_coupon_desk.catalog import (  # noqa: E402
    coupon_from_raw,
    parse_coupon_price,
    parse_threshold_discount,
    parse_validity,
)
from mcd_coupon_desk.coupons import evaluate  # noqa: E402
from mcd_coupon_desk.models import (  # noqa: E402
    REASON_ITEM_MISMATCH,
    REASON_NOT_BENEFICIAL,
    REASON_OK,
    Item,
    Line,
    SCOPE_STORE,
)

# 实测的 query-store-coupons 单条返回
REAL_COUPON = {
    "title": "9.9元中杯冰美式",
    "couponId": "264B97DBE02BE6CF996AA6914151C4A9",
    "couponCode": "MCD6R124JM0L96009B100",
    "tradeDateTime": "2026-10-10 00:00:00-2026-10-16 23:59:59",
    "products": [{"productCode": "507411", "productName": "麦咖啡™冰美式中杯"}],
    "promotionId": "112023002",
}


class TestCouponPriceParsing(unittest.TestCase):
    """券面价只能从券名解析 —— 这是对上游缺字段的补偿，必须固定住。"""

    def test_price_in_title(self):
        self.assertEqual(parse_coupon_price("9.9元中杯冰美式"), 990)
        self.assertEqual(parse_coupon_price("23.9元四件套"), 2390)

    def test_free_coupon_is_zero_not_none(self):
        # 「免费」= 券面价 0，与「无法识别」必须区分开
        self.assertEqual(parse_coupon_price("免费脆薯饼"), 0)
        self.assertEqual(parse_coupon_price("0元甜筒"), 0)

    def test_unrecognized_returns_none(self):
        # 识别不出就不能臆造价格
        self.assertIsNone(parse_coupon_price("买一送一"))
        self.assertIsNone(parse_coupon_price(""))

    def test_threshold_fallback(self):
        self.assertEqual(parse_threshold_discount("满30减12"), (3000, 1200))
        self.assertEqual(parse_threshold_discount("9.9元中杯冰美式"), (0, 0))


class TestValidityParsing(unittest.TestCase):
    def test_real_range(self):
        start, end = parse_validity("2026-10-10 00:00:00-2026-10-16 23:59:59")
        self.assertEqual(start, "2026-10-10 00:00:00")
        self.assertEqual(end, "2026-10-16 23:59:59")

    def test_date_only_range(self):
        start, end = parse_validity("2026-10-10-2026-10-16")
        self.assertEqual(start, "2026-10-10")
        self.assertEqual(end, "2026-10-16")

    def test_single_value_is_end(self):
        start, end = parse_validity("2026-10-16 23:59:59")
        self.assertEqual(start, "")
        self.assertEqual(end, "2026-10-16 23:59:59")

    def test_empty(self):
        self.assertEqual(parse_validity(""), ("", ""))


class TestCouponFromRaw(unittest.TestCase):
    def test_real_sample_maps_correctly(self):
        coupon = coupon_from_raw(REAL_COUPON, scope=SCOPE_STORE)
        self.assertEqual(coupon.name, "9.9元中杯冰美式")
        self.assertEqual(coupon.coupon_id, "264B97DBE02BE6CF996AA6914151C4A9")
        self.assertEqual(coupon.coupon_code, "MCD6R124JM0L96009B100")
        self.assertEqual(coupon.fixed_price_cents, 990)
        self.assertTrue(coupon.is_fixed_price)
        self.assertEqual(coupon.item_codes, ("507411",))
        self.assertEqual(coupon.valid_from, "2026-10-10 00:00:00")
        self.assertEqual(coupon.valid_to, "2026-10-16 23:59:59")
        self.assertEqual(coupon.scope, SCOPE_STORE)


class TestFixedPriceEvaluation(unittest.TestCase):
    """「固定价券」的省额必须由购物车里的商品原价现算。"""

    def setUp(self):
        self.coupon = coupon_from_raw(REAL_COUPON, scope=SCOPE_STORE)

    def test_saving_computed_from_menu_price(self):
        cart = [Line(Item("507411", "麦咖啡™冰美式中杯", 1700))]
        verdict = evaluate(self.coupon, cart, "1410419")
        self.assertTrue(verdict.usable)
        self.assertEqual(verdict.saving_cents, 1700 - 990)

    def test_no_applicable_item(self):
        cart = [Line(Item("9999", "巨无霸", 2650))]
        verdict = evaluate(self.coupon, cart, "1410419")
        self.assertFalse(verdict.usable)
        self.assertEqual(verdict.reason_code, REASON_ITEM_MISMATCH)

    def test_refuses_when_coupon_price_exceeds_item_price(self):
        # 券面 9.9，商品只要 5 元 —— 用了反而更贵，必须明说而不是当成 0
        cart = [Line(Item("507411", "麦咖啡™冰美式中杯", 500))]
        verdict = evaluate(self.coupon, cart, "1410419")
        self.assertFalse(verdict.usable)
        self.assertEqual(verdict.reason_code, REASON_NOT_BENEFICIAL)

    def test_picks_most_expensive_applicable_item(self):
        coupon = coupon_from_raw(
            {**REAL_COUPON, "products": [
                {"productCode": "A", "productName": "小杯"},
                {"productCode": "B", "productName": "大杯"},
            ]},
            scope=SCOPE_STORE,
        )
        cart = [Line(Item("A", "小杯", 1200)), Line(Item("B", "大杯", 2000))]
        verdict = evaluate(coupon, cart, "1410419")
        self.assertEqual(verdict.saving_cents, 2000 - 990)
        self.assertEqual(verdict.reason_code, REASON_OK)

    def test_free_coupon_saves_full_price(self):
        coupon = coupon_from_raw(
            {**REAL_COUPON, "title": "免费脆薯饼",
             "products": [{"productCode": "S9", "productName": "脆薯饼"}]},
            scope=SCOPE_STORE,
        )
        cart = [Line(Item("S9", "脆薯饼", 900))]
        verdict = evaluate(coupon, cart, "1410419")
        self.assertEqual(verdict.saving_cents, 900)


if __name__ == "__main__":
    unittest.main(verbosity=2)
