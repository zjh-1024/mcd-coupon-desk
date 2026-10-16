"""推荐引擎测试。

建议全部由确定性规则算出，因此可以逐条断言 ——
这也是本项目把「推荐」放在代码里而不是交给大模型的原因。
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from mcd_coupon_desk.advisor import advise, advise_coupons, advise_points  # noqa: E402
from mcd_coupon_desk.catalog import account_from_raw, reward_from_raw       # noqa: E402
from mcd_coupon_desk.models import (                                        # noqa: E402
    ADVICE_INFO,
    ADVICE_TIP,
    ADVICE_URGENT,
    Coupon,
    PointsAccount,
    RewardProduct,
)
from mcd_coupon_desk.schedule import parse_period                            # noqa: E402

NOW = datetime(2026, 10, 16, 22, 17)     # 周五 22:17（真实抓取时刻）


def _coupon(**kwargs) -> Coupon:
    base = dict(coupon_id="C", name="券", discount_cents=500, threshold_cents=0)
    base.update(kwargs)
    return Coupon(**base)


class TestAccountFromRaw(unittest.TestCase):
    def test_real_payload_shape(self):
        """上游把 month 拼成了 Mouth，必须原样接住。"""
        account = account_from_raw({
            "availablePoint": "804", "accountId": "02004865038383544520911991219",
            "accumulativePoint": "2096", "currency": "麦享会积分",
            "currentMouthExpirePoint": "0", "nextMouthExpirePoint": "30",
            "expiredPoint": "0", "frozenPoint": "0", "usedPoint": "1292",
        })
        self.assertEqual(account.available, 804)
        self.assertEqual(account.accumulative, 2096)
        self.assertEqual(account.used, 1292)
        self.assertEqual(account.expiring_next_month, 30)
        self.assertEqual(account.masked_id, "****1219")

    def test_correct_month_spelling_also_accepted(self):
        account = account_from_raw({"currentMonthExpirePoint": "12"})
        self.assertEqual(account.expiring_this_month, 12)

    def test_masked_id_when_missing(self):
        self.assertEqual(account_from_raw({}).masked_id, "—")


class TestAdvisePoints(unittest.TestCase):
    def test_expiring_this_month_is_urgent(self):
        account = PointsAccount(account_id="X", available=100, expiring_this_month=50)
        advices = advise_points(account, [], NOW)
        self.assertEqual(advices[0].level, ADVICE_URGENT)
        self.assertIn("即将过期", advices[0].title)

    def test_recommends_the_item_that_uses_balance_fully(self):
        """804 分 → 选 800 分那件（用得最满），而不是随便一件便宜的。"""
        account = PointsAccount(account_id="X", available=804, accumulative=2096)
        products = [
            reward_from_raw({"spuName": "6.9元可乐麦炫酷", "point": "50", "status": 2,
                             "downTime": "2026-12-30 23:59:59"}),
            reward_from_raw({"spuName": "16.9元麦辣可乐组合", "point": "500", "status": 2,
                             "downTime": "2026-12-30 23:59:59"}),
            reward_from_raw({"spuName": "21.9元巨无霸可乐组合", "point": "800", "status": 2,
                             "downTime": "2026-12-30 23:59:59"}),
            reward_from_raw({"spuName": "买不起的", "point": "5000", "status": 2,
                             "downTime": "2026-12-30 23:59:59"}),
        ]
        advices = advise_points(account, products, NOW)
        titles = [a.title for a in advices]
        self.assertIn("可用 804 积分兑换「21.9元巨无霸可乐组合」", titles)
        detail = next(a.detail for a in advices if "巨无霸可乐组合" in a.title)
        self.assertIn("剩 4 分", detail)

    def test_reports_gap_when_balance_not_enough(self):
        account = PointsAccount(account_id="X", available=700)
        products = [reward_from_raw({"spuName": "21.9元巨无霸可乐组合", "point": "800",
                                     "status": 2, "downTime": "2026-12-30 23:59:59"})]
        advices = advise_points(account, products, NOW)
        self.assertTrue(any("再攒 100 分" in a.title for a in advices))

    def test_expired_rewards_are_excluded(self):
        """实测上游会把兑换期已结束的商品一起返回，不能推荐它们。"""
        account = PointsAccount(account_id="X", available=804)
        products = [
            reward_from_raw({"spuName": "已下架的旧券", "point": "100", "status": 2,
                             "upTime": "2024-12-31 11:45:00",
                             "downTime": "2026-03-21 23:59:59"}),
            reward_from_raw({"spuName": "在售的券", "point": "200", "status": 2,
                             "upTime": "2026-06-25 00:00:00",
                             "downTime": "2026-12-30 23:59:59"}),
        ]
        advices = advise_points(account, products, NOW)
        joined = " ".join(a.title for a in advices)
        self.assertIn("在售的券", joined)
        self.assertNotIn("已下架的旧券", joined)

    def test_zero_point_rewards_are_not_treated_as_free_food(self):
        """0 积分商品（生日派对）是付钱商品，不能说成「积分可兑换」。"""
        account = PointsAccount(account_id="X", available=10)
        products = [reward_from_raw({"spuName": "麦当劳亲子读书会", "point": "0",
                                     "price": "45", "status": 2,
                                     "downTime": "2026-12-30 23:59:59"})]
        advices = advise_points(account, products, NOW)
        self.assertFalse(any("可用 10 积分兑换" in a.title for a in advices))

    def test_no_data_returns_empty(self):
        self.assertEqual(advise_points(None, [], NOW), [])
        self.assertEqual(advise_points(PointsAccount(), [], NOW), [])


class TestAdviseCoupons(unittest.TestCase):
    def test_expiring_today_is_urgent(self):
        coupons = [_coupon(name="中薯条买一送一", valid_to="2026-10-16 23:59")]
        advices = advise_coupons(coupons, NOW)
        self.assertEqual(advices[0].level, ADVICE_URGENT)
        self.assertIn("今天有 1 张券到期", advices[0].title)

    def test_expired_coupons_are_ignored(self):
        coupons = [_coupon(name="老券", valid_to="2026-09-30")]
        self.assertEqual(advise_coupons(coupons, NOW), [])

    def test_out_of_period_coupon_tells_when_it_opens(self):
        """真实场景：麦旋风 10:00-22:00，此刻 22:17 已过 —— 要算出下次可用时间。"""
        coupons = [_coupon(name="人气麦旋风买一送一", valid_to="2026-10-18 23:59",
                           period=parse_period("10:00-22:00"))]
        advices = advise_coupons(coupons, NOW)
        titles = " ".join(a.title for a in advices)
        self.assertIn("不在可用时段", titles)
        self.assertIn("明天 10:00", titles)

    def test_not_yet_effective_coupon_reports_start_day(self):
        coupons = [_coupon(name="火腿扒堡早餐两件套", valid_from="2026-10-19 05:00",
                           valid_to="2026-10-19 10:29",
                           period=parse_period("周一 05:00-10:29"))]
        advices = advise_coupons(coupons, NOW)
        self.assertTrue(any("尚未生效" in a.title for a in advices))
        detail = next(a.detail for a in advices if "尚未生效" in a.title)
        self.assertIn("10月19日（周一）05:00", detail)

    def test_buy_one_get_one_is_flagged(self):
        coupons = [_coupon(name="中薯条买一送一", valid_to="2026-11-30")]
        advices = advise_coupons(coupons, NOW)
        self.assertTrue(any("买一送一" in a.title for a in advices))

    def test_missing_item_info_is_stated_not_hidden(self):
        """缺适用商品的券必须如实说明「不参与精确算省额」，不能装作能算。"""
        coupons = [_coupon(name="某券", valid_to="2026-11-30")]
        advices = advise_coupons(coupons, NOW)
        target = next(a for a in advices if "未提供适用商品" in a.title)
        self.assertEqual(target.level, ADVICE_INFO)
        self.assertIn("不参与精确算省额", target.detail)

    def test_coupon_with_item_codes_is_not_flagged_as_blind(self):
        coupons = [_coupon(name="某券", valid_to="2026-11-30", item_codes=("B001",))]
        advices = advise_coupons(coupons, NOW)
        self.assertFalse(any("未提供适用商品" in a.title for a in advices))


class TestAdviseOrdering(unittest.TestCase):
    def test_urgent_first_then_tip_then_info(self):
        account = account_from_raw({"availablePoint": "804", "accountId": "X",
                                    "accumulativePoint": "2096", "usedPoint": "1292",
                                    "currentMouthExpirePoint": "10"})
        coupons = [
            _coupon(coupon_id="A", name="今天到期券", valid_to="2026-10-16 23:59"),
            _coupon(coupon_id="B", name="买一送一券", valid_to="2026-11-30"),
        ]
        products = [reward_from_raw({"spuName": "10.9元指定小食任选", "point": "200",
                                     "status": 2, "downTime": "2026-12-30 23:59:59"})]
        advices = advise(account, coupons, products, NOW)
        levels = [a.level for a in advices]
        rank = {ADVICE_URGENT: 0, ADVICE_TIP: 1, ADVICE_INFO: 2}
        self.assertEqual(levels, sorted(levels, key=lambda lv: rank[lv]))
        self.assertEqual(advices[0].level, ADVICE_URGENT)
        self.assertGreater(len(advices), 3)


class TestRewardProductState(unittest.TestCase):
    def test_state_text_marks_ended_and_upcoming(self):
        ended = reward_from_raw({"spuName": "旧", "point": "100", "status": 2,
                                 "downTime": "2026-03-21 23:59:59"})
        live = reward_from_raw({"spuName": "现货", "point": "100", "status": 2,
                                "upTime": "2026-06-25 00:00:00",
                                "downTime": "2026-12-30 23:59:59"})
        later = reward_from_raw({"spuName": "预热", "point": "100", "status": 2,
                                 "upTime": "2027-01-01 00:00:00"})
        self.assertEqual(ended.state_text(NOW), "已结束")
        self.assertEqual(ended.state_text(NOW), "已结束")
        self.assertEqual(live.state_text(NOW), "可兑换")
        self.assertEqual(later.state_text(NOW), "未开始")
        self.assertTrue(live.redeemable_at(NOW))
        self.assertFalse(ended.redeemable_at(NOW))

    def test_status_wins_when_not_on_shelf(self):
        product = RewardProduct(spu_id=1, name="售罄品", points=100, status=3)
        self.assertEqual(product.state_text(NOW), "售罄")


if __name__ == "__main__":
    unittest.main()
