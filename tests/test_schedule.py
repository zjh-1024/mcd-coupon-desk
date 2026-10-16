"""券的「可用时段」解析与判定测试。

数据形状全部取自 2026-10-16 真实 `query-my-coupons` 返回，
不做臆造 —— 这些用例的价值就在于锁住真实文本里的坑。
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from mcd_coupon_desk.coupons import evaluate          # noqa: E402
from mcd_coupon_desk.models import REASON_NOT_STARTED, REASON_OK, REASON_OUT_OF_PERIOD, Coupon  # noqa: E402
from mcd_coupon_desk.models import Item, Line          # noqa: E402
from mcd_coupon_desk.schedule import (                 # noqa: E402
    describe_moment,
    parse_period,
    parse_time_windows,
    parse_weekdays,
)


class TestParseWeekdays(unittest.TestCase):
    def test_chinese_list_form(self):
        """真实文本「周一、二、三、四、五」——后续星期是裸汉字，没有「周」前缀。"""
        self.assertEqual(parse_weekdays("周一、二、三、四、五 10:30-23:59"), (0, 1, 2, 3, 4))

    def test_single_day(self):
        self.assertEqual(parse_weekdays("周六 05:00-10:29"), (5,))

    def test_range_form(self):
        self.assertEqual(parse_weekdays("周一至周五 10:30-23:59"), (0, 1, 2, 3, 4))

    def test_no_weekday(self):
        self.assertEqual(parse_weekdays("10:00-22:00"), ())

    def test_does_not_swallow_trailing_digits(self):
        """回归：星期字符类里不能含数字。

        完整有效期文本是
        「2026-10-12 10:30-2026-10-16 23:59 周一、二、三、四、五 10:30-23:59」。
        早期实现把 `\\d` 放进字符类，结果把末尾的 "10:30" 里的 1、0 当成
        「周一」「周日」，凭空多出两个星期并污染判定。
        """
        text = "2026-10-12 10:30-2026-10-16 23:59 周一、二、三、四、五 10:30-23:59"
        self.assertEqual(parse_weekdays(text), (0, 1, 2, 3, 4))
        # 时段窗口同样只能有一个（不能把日期区间那截也当成时间窗）
        self.assertEqual(parse_time_windows(text), ((630, 1439),))


class TestParseTimeWindows(unittest.TestCase):
    def test_single_window(self):
        self.assertEqual(parse_time_windows("周六 05:00-10:29"), ((300, 629),))

    def test_ignores_date_range(self):
        """日期区间「2026-10-10 00:00-2026-10-16 23:59」不应被误判成时间窗。

        「10:30-2026」这种形态不满足 hh:mm-hh:mm，因此不会匹配。
        """
        self.assertEqual(parse_time_windows("2026-10-10 00:00-2026-10-16 23:59"), ())


class TestPeriodIsOpenAt(unittest.TestCase):
    def test_unrestricted_always_open(self):
        period = parse_period("")
        self.assertTrue(period.unrestricted)
        self.assertTrue(period.is_open_at(datetime(2026, 10, 16, 3, 0)))

    def test_weekday_and_time_must_both_match(self):
        # 真实：中薯条买一送一，周一至周五 10:30-23:59
        period = parse_period("周一、二、三、四、五 10:30-23:59")
        self.assertTrue(period.is_open_at(datetime(2026, 10, 16, 22, 17)))   # 周五 22:17
        self.assertFalse(period.is_open_at(datetime(2026, 10, 16, 9, 0)))    # 周五 09:00 早于时段
        self.assertFalse(period.is_open_at(datetime(2026, 10, 17, 22, 0)))   # 周六不在星期集合

    def test_window_only(self):
        # 真实：人气麦旋风买一送一，10:00-22:00（不限制星期）
        period = parse_period("10:00-22:00")
        self.assertTrue(period.is_open_at(datetime(2026, 10, 16, 12, 0)))
        self.assertFalse(period.is_open_at(datetime(2026, 10, 16, 22, 17)))

    def test_window_crossing_midnight(self):
        period = parse_period("22:00-02:00")
        self.assertTrue(period.is_open_at(datetime(2026, 10, 16, 23, 30)))
        self.assertTrue(period.is_open_at(datetime(2026, 10, 16, 1, 0)))
        self.assertFalse(period.is_open_at(datetime(2026, 10, 16, 12, 0)))


class TestPeriodDescribeAndNextOpen(unittest.TestCase):
    def test_describe_compresses_weekdays(self):
        self.assertEqual(parse_period("周一、二、三、四、五 10:30-23:59").describe(),
                         "周一至周五 10:30-23:59")
        self.assertEqual(parse_period("周六 05:00-10:29").describe(), "周六 05:00-10:29")
        self.assertEqual(parse_period("10:00-22:00").describe(), "10:00-22:00")
        self.assertEqual(parse_period("").describe(), "不限时段")

    def test_next_open_same_day_later(self):
        """22:17 已过 22:00 → 下次可用是明天 10:00。这是「现在买还是等」的关键结论。"""
        period = parse_period("10:00-22:00")
        now = datetime(2026, 10, 16, 22, 17)
        self.assertEqual(period.next_open(now), datetime(2026, 10, 17, 10, 0))

    def test_next_open_skips_to_matching_weekday(self):
        # 周五 22:17 看「周一 05:00-10:29」→ 下次是 10-19（周一）05:00
        period = parse_period("周一 05:00-10:29")
        self.assertEqual(period.next_open(datetime(2026, 10, 16, 22, 17)),
                         datetime(2026, 10, 19, 5, 0))

    def test_next_open_none_when_unrestricted(self):
        self.assertIsNone(parse_period("").next_open(datetime(2026, 10, 16, 12, 0)))


class TestDescribeMoment(unittest.TestCase):
    def test_relative_wording(self):
        now = datetime(2026, 10, 16, 22, 17)
        self.assertEqual(describe_moment(datetime(2026, 10, 16, 23, 30), now), "今天 23:30")
        self.assertEqual(describe_moment(datetime(2026, 10, 17, 10, 0), now), "明天 10:00")
        self.assertEqual(describe_moment(datetime(2026, 10, 18, 5, 0), now), "后天 05:00")
        self.assertEqual(describe_moment(datetime(2026, 10, 19, 5, 0), now),
                         "10月19日（周一）05:00")


class TestVerdictWithPeriod(unittest.TestCase):
    """时段必须参与券的可用性判定。

    真实场景：人气麦旋风买一送一 有效至 2026-10-18 23:59，但只在 10:00-22:00 可用。
    在 22:17 只判有效期会得出「可用」的错误结论。
    """

    def setUp(self):
        self.item = Item("D001", "可口可乐中杯", 1000, "饮品", ("饮品",))

    def _coupon(self, **kwargs):
        base = dict(coupon_id="C1", name="测试券", discount_cents=500, threshold_cents=0)
        base.update(kwargs)
        return Coupon(**base)

    def test_out_of_period_is_reported_with_next_window(self):
        coupon = self._coupon(
            valid_from="2026-10-12", valid_to="2026-10-18 23:59",
            period=parse_period("10:00-22:00"),
        )
        verdict = evaluate(coupon, [Line(self.item, 1)], "SH0001",
                           datetime(2026, 10, 16, 22, 17))
        self.assertFalse(verdict.usable)
        self.assertEqual(verdict.reason_code, REASON_OUT_OF_PERIOD)
        self.assertIn("明天 10:00", verdict.reason)

    def test_open_period_stays_usable(self):
        coupon = self._coupon(
            valid_from="2026-10-12", valid_to="2026-10-18 23:59",
            period=parse_period("10:00-22:00"),
        )
        verdict = evaluate(coupon, [Line(self.item, 1)], "SH0001",
                           datetime(2026, 10, 16, 12, 0))
        self.assertTrue(verdict.usable)
        self.assertEqual(verdict.reason_code, REASON_OK)

    def test_validity_takes_precedence_over_period(self):
        """还没到生效日期的券应报「尚未开始」，而不是「不在可用时段」。"""
        coupon = self._coupon(
            valid_from="2026-10-19 05:00", valid_to="2026-10-19 10:29",
            period=parse_period("周一 05:00-10:29"),
        )
        verdict = evaluate(coupon, [Line(self.item, 1)], "SH0001",
                           datetime(2026, 10, 16, 22, 17))
        self.assertEqual(verdict.reason_code, REASON_NOT_STARTED)

    def test_coupon_without_period_is_unaffected(self):
        """回归：不带时段的券行为必须与改动前完全一致。"""
        coupon = self._coupon(valid_from="2026-10-01", valid_to="2026-12-31")
        verdict = evaluate(coupon, [Line(self.item, 1)], "SH0001",
                          datetime(2026, 10, 16, 3, 0))
        self.assertTrue(verdict.usable)


if __name__ == "__main__":
    unittest.main()
