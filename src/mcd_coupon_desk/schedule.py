"""券的「可用时段」解析与判定。

为什么单独一个模块
------------------
麦当劳的券有两层**独立**的时间约束：

1. **有效区间**（validFrom / validTo）—— 这张券本身能用多久；
2. **可用时段** —— 有效期内还限「哪几天 + 几点到几点」。

实测样例（2026-10-16，`query-my-coupons` 真实返回）::

    巧克力味厚松饼猪柳蛋套餐
    - 优惠: ¥29.9 (用券价格)
    - 有效期: 2026-10-17 05:00-2026-10-17 10:29 周六 05:00-10:29

第 2 行的「周六 05:00-10:29」就是可用时段：这张券只在周六早餐时段能用。
**只判有效区间会得出「这张券今天就能用」的错误结论** —— 而这恰恰是
本项目「现在买还是等」这一问最需要的信息。

因此这里把时段解析成结构化数据（星期集合 + 时间窗），
既用于「此刻能不能用」，也用于算出「下次什么时候能用」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta

# 周几的汉字 → Python 的 weekday()（周一 = 0）
_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_WEEKDAY_LABEL = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 「周一」+ 后续「、二、三、四、五」这种连写
# 注意：字符类里**刻意不含数字** —— 否则「周五 10:30-23:59」会把后面的 "10" 吃进来
_WEEK_BLOCK = re.compile(
    r"(?:周|星期|礼拜)\s*([一二三四五六日天])"
    r"((?:\s*[、,，和及]\s*[一二三四五六日天])*)"
)
# 「周一至周五」这类区间
_WEEK_RANGE = re.compile(
    r"(?:周|星期|礼拜)\s*([一二三四五六日天])\s*[-~至—]\s*(?:周|星期|礼拜)?\s*([一二三四五六日天])"
)
# 「10:30-23:59」这类时间窗
_TIME_WINDOW = re.compile(r"(\d{1,2}):(\d{2})\s*[-~至—]\s*(\d{1,2}):(\d{2})")

_MINUTES_PER_DAY = 24 * 60


def parse_datetime(value: str) -> datetime | None:
    """宽松解析日期 / 日期时间字符串；解析不了返回 None（视为不限）。"""
    if not value:
        return None
    text = str(value).strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_weekdays(text: str) -> tuple[int, ...]:
    """从文本里解析出涉及的星期（0=周一）。解析不到返回空元组。"""
    found: list[int] = []
    for match in _WEEK_RANGE.finditer(text):
        start, end = _WEEKDAY_INDEX[match.group(1)], _WEEKDAY_INDEX[match.group(2)]
        if start <= end:
            found.extend(range(start, end + 1))
        else:                       # 跨周，如「周五至周一」
            found.extend(list(range(start, 7)) + list(range(0, end + 1)))
    for match in _WEEK_BLOCK.finditer(text):
        found.append(_WEEKDAY_INDEX[match.group(1)])
        for ch in match.group(2):
            if ch in _WEEKDAY_INDEX:
                found.append(_WEEKDAY_INDEX[ch])
    return tuple(sorted(set(found)))


def parse_time_windows(text: str) -> tuple[tuple[int, int], ...]:
    """从文本里解析出时间窗，单位为「当天第几分钟」。解析不到返回空元组。"""
    windows: set[tuple[int, int]] = set()
    for match in _TIME_WINDOW.finditer(text):
        h1, m1, h2, m2 = (int(g) for g in match.groups())
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if 0 <= start < _MINUTES_PER_DAY and 0 <= end < _MINUTES_PER_DAY:
            windows.add((start, end))
    return tuple(sorted(windows))


def _format_minutes(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def _weekday_text(weekdays: tuple[int, ...]) -> str:
    """把星期集合压缩成「周一至周五」这种人类可读写法。"""
    if not weekdays:
        return ""
    if len(weekdays) == 7:
        return "每天"
    spans: list[tuple[int, int]] = []
    start = prev = weekdays[0]
    for day in weekdays[1:]:
        if day == prev + 1:
            prev = day
            continue
        spans.append((start, prev))
        start = prev = day
    spans.append((start, prev))
    chunks = [
        _WEEKDAY_LABEL[a] if a == b else f"{_WEEKDAY_LABEL[a]}至{_WEEKDAY_LABEL[b]}"
        for a, b in spans
    ]
    return "、".join(chunks)


@dataclass(frozen=True)
class Period:
    """券的可用时段。三个字段全空表示**不限时段**。"""

    weekdays: tuple[int, ...] = ()
    windows: tuple[tuple[int, int], ...] = ()
    text: str = ""       # 原始描述，用于展示与追溯

    @property
    def unrestricted(self) -> bool:
        return not self.weekdays and not self.windows

    def is_open_at(self, moment: datetime) -> bool:
        """给定时刻是否落在可用时段内。不限时段时恒为 True。"""
        if self.weekdays and moment.weekday() not in self.weekdays:
            return False
        if not self.windows:
            return True
        current = moment.hour * 60 + moment.minute
        for start, end in self.windows:
            if start <= end:
                if start <= current <= end:
                    return True
            elif current >= start or current <= end:      # 跨零点，如 22:00-02:00
                return True
        return False

    def describe(self) -> str:
        """渲染成「周六 05:00-10:29」这样的可读文本。"""
        if self.unrestricted:
            return "不限时段"
        days = _weekday_text(self.weekdays)
        times = "、".join(f"{_format_minutes(s)}-{_format_minutes(e)}" for s, e in self.windows)
        return " ".join(part for part in (days, times) if part)

    def next_open(self, moment: datetime) -> datetime | None:
        """下一次可用时段的起点；不限时段或七天内无可用时段时返回 None。"""
        if self.unrestricted:
            return None
        for offset in range(0, 8):
            day = (moment + timedelta(days=offset)).date()
            if self.weekdays and day.weekday() not in self.weekdays:
                continue
            if not self.windows:
                candidate = datetime.combine(day, time(0, 0))
                if candidate > moment:
                    return candidate
                continue
            for start, _end in self.windows:
                candidate = datetime.combine(day, time(start // 60, start % 60))
                if candidate > moment:
                    return candidate
        return None


def parse_period(text: str) -> Period:
    """把「周六 05:00-10:29」这类描述解析成 Period。"""
    raw = str(text or "").strip()
    if not raw:
        return Period()
    return Period(
        weekdays=parse_weekdays(raw),
        windows=parse_time_windows(raw),
        text=raw,
    )


def describe_moment(moment: datetime, now: datetime) -> str:
    """把某个时刻说成「今天 10:00」/「明天 05:00」/「10月19日 05:00」。

    相对日期比绝对日期更好决策 —— 用户想知道的是「还要等多久」，
    而不是「日历上第几天」。
    """
    days = (moment.date() - now.date()).days
    clock = f"{moment.hour:02d}:{moment.minute:02d}"
    if days == 0:
        return f"今天 {clock}"
    if days == 1:
        return f"明天 {clock}"
    if days == 2:
        return f"后天 {clock}"
    weekday = _WEEKDAY_LABEL[moment.weekday()]
    return f"{moment.month}月{moment.day}日（{weekday}）{clock}"


__all__ = [
    "Period",
    "describe_moment",
    "parse_datetime",
    "parse_period",
    "parse_time_windows",
    "parse_weekdays",
]
