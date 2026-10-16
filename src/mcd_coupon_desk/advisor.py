"""基于真实账号数据的推荐引擎。

设计原则：**推荐由确定性规则算出，不交给大模型。**
理由和求解器一致 —— 用户要能顺着数字自己复核「为什么推荐这个」。
每条建议都能追到具体的数据（哪张券、还剩几天、差多少积分）。

覆盖三类真实痛点
----------------
1. **积分别烂在账户里** —— 麦享会积分到期不补发，先用「将过期」提醒，
   再按余额算「能兑换什么」；不够时明确算出「还差多少分」。
2. **券的时机** —— 券有两层时间约束（有效区间 + 可用时段）。
   「现在不能用、什么时候能用」往往比「省多少钱」更决定这一顿怎么点，
   所以这里会把「下次可用时间」直接算出来。
3. **不虚报** —— 账号券不带「适用商品」信息，就无法精确算省额。
   这种情况如实说明，而不是拿券面价硬凑一个节省额。
"""

from __future__ import annotations

from datetime import datetime, date

from .models import (
    ADVICE_INFO,
    ADVICE_TIP,
    ADVICE_URGENT,
    Advice,
    Coupon,
    PointsAccount,
    RewardProduct,
)
from .schedule import describe_moment, parse_datetime

# 时间比较的兜底值：没有截止时间的券视为「很久以后才到期」
_FAR_FUTURE_DAYS = 9999
# 「差一点点就能换」的阈值：差额在此以内就值得专门提示去攒
_NEAR_GAP_POINTS = 200


# ---------------------------------------------------------------- 券的时间工具


def _ends_at(coupon: Coupon) -> datetime | None:
    return parse_datetime(coupon.valid_to)


def _starts_at(coupon: Coupon) -> datetime | None:
    return parse_datetime(coupon.valid_from)


def _expired(coupon: Coupon, now: datetime) -> bool:
    end = _ends_at(coupon)
    return end is not None and now > end


def _days_left(coupon: Coupon, now: datetime) -> int:
    end = _ends_at(coupon)
    return (end.date() - now.date()).days if end else _FAR_FUTURE_DAYS


def _expires_on(coupon: Coupon, when: date) -> bool:
    end = _ends_at(coupon)
    if end is not None and end.date() == when:
        return True
    # 服务端有时只给「今日到期」这个标记，不给具体到点的时间
    return "今日到期" in coupon.badges


def _within_validity(coupon: Coupon, now: datetime) -> bool:
    start, end = _starts_at(coupon), _ends_at(coupon)
    if start is not None and now < start:
        return False
    return not (end is not None and now > end)


# ---------------------------------------------------------------- 积分建议


def advise_points(
    account: PointsAccount | None,
    products: list[RewardProduct] | None = None,
    now: datetime | None = None,
) -> list[Advice]:
    """围绕积分余额与积分商城给出的建议。"""
    now = now or datetime.now()
    if account is None or not account.has_data:
        return []

    out: list[Advice] = []

    # 1) 将过期 —— 会真实损失权益，最高优先级
    if account.expiring_this_month > 0:
        out.append(Advice(
            ADVICE_URGENT,
            f"本月有 {account.expiring_this_month} 积分即将过期",
            "麦享会积分到期不补发，建议先去积分商城兑换成商品。",
        ))
    if account.expiring_next_month > 0:
        out.append(Advice(
            ADVICE_TIP,
            f"下月有 {account.expiring_next_month} 积分将过期",
            "可以提前安排兑换，避免到期作废。",
        ))

    items = [p for p in (products or []) if p.redeemable_at(now)]

    if account.available > 0:
        affordable = sorted(
            (p for p in items if p.affordable(account.available)),
            key=lambda p: p.points,
        )
        if affordable:
            # 取「积分用得最满」的那件 —— 既用掉余额，又不浪费积分
            best = affordable[-1]
            leftover = account.available - best.points
            out.append(Advice(
                ADVICE_TIP,
                f"可用 {account.available} 积分兑换「{best.name}」",
                f"需 {best.points} 分，兑换后剩 {leftover} 分；"
                f"当前预算内共 {len(affordable)} 件可选，"
                f"该商品兑换期至 {best.down_time or '不限'}。",
            ))
        else:
            costs = sorted(
                (p for p in items if p.costs_points),
                key=lambda p: p.points,
            )
            if costs:
                nearest = costs[0]
                gap = nearest.points - account.available
                out.append(Advice(
                    ADVICE_TIP if gap <= _NEAR_GAP_POINTS else ADVICE_INFO,
                    f"再攒 {gap} 分可兑换「{nearest.name}」",
                    f"该商品需 {nearest.points} 分，当前可用 {account.available} 分；"
                    "点餐消费可累积麦享会积分。",
                ))
            else:
                out.append(Advice(
                    ADVICE_INFO,
                    f"当前 {account.available} 积分暂无可兑换商品",
                    "积分商城此刻没有在售的积分商品，可以稍后再看看。",
                ))

    if account.accumulative > 0:
        out.append(Advice(
            ADVICE_INFO,
            f"累计 {account.accumulative} 分 · 已用 {account.used} 分",
            f"账号 {account.masked_id}，当前可用 {account.available} 分"
            f"（{account.currency or '积分'}）。",
        ))
    return out


# ---------------------------------------------------------------- 券建议


def advise_coupons(coupons: list[Coupon], now: datetime | None = None) -> list[Advice]:
    """围绕券的到期时间与可用时段给出的建议。"""
    now = now or datetime.now()
    live = [c for c in (coupons or []) if not _expired(c, now)]
    if not live:
        return []

    out: list[Advice] = []

    # 1) 今日到期 —— 过期即作废
    today = [c for c in live if _expires_on(c, now.date())]
    if today:
        names = "、".join(c.name for c in today[:4])
        suffix = f" 等 {len(today)} 张" if len(today) > 4 else ""
        out.append(Advice(
            ADVICE_URGENT,
            f"今天有 {len(today)} 张券到期",
            f"{names}{suffix}。今天用得上的话就用掉，过期作废。",
        ))

    # 2) 三天内到期（不含今天）
    soon = [c for c in live if 0 < _days_left(c, now) <= 3]
    if soon:
        soon.sort(key=lambda c: _days_left(c, now))
        out.append(Advice(
            ADVICE_TIP,
            f"{len(soon)} 张券将在 3 天内到期",
            "、".join(f"{c.name}（剩 {_days_left(c, now)} 天）" for c in soon[:5]),
        ))

    # 3) 现在不在可用时段 —— 直接说出「什么时候再来」
    waiting: list[tuple[datetime, Coupon]] = []
    for coupon in live:
        if coupon.period.unrestricted or coupon.period.is_open_at(now):
            continue
        if not _within_validity(coupon, now):
            continue                      # 还没开始生效的，归到第 4 条
        upcoming = coupon.period.next_open(now)
        if upcoming is not None:
            waiting.append((upcoming, coupon))
    if waiting:
        waiting.sort(key=lambda pair: pair[0])
        first = waiting[0][0]
        out.append(Advice(
            ADVICE_TIP,
            f"现在有 {len(waiting)} 张券不在可用时段，"
            f"{describe_moment(first, now)} 起可用",
            "、".join(f"{c.name}（{c.period.describe()}）" for _t, c in waiting[:5])
            + " —— 想吃这些，等到时段内再下单。",
        ))

    # 4) 还没开始生效的券（如实告知哪天开始）
    upcoming_coupons: list[tuple[datetime, Coupon]] = []
    for coupon in live:
        start = _starts_at(coupon)
        if start is not None and start > now:
            upcoming_coupons.append((start, coupon))
    if upcoming_coupons:
        upcoming_coupons.sort(key=lambda pair: pair[0])
        first = upcoming_coupons[0][0]
        detail = "、".join(
            f"{c.name}（{describe_moment(s, now)}"
            + (f"，{c.period.describe()}" if not c.period.unrestricted else "")
            + "）"
            for s, c in upcoming_coupons[:5]
        )
        out.append(Advice(
            ADVICE_TIP,
            f"{len(upcoming_coupons)} 张券尚未生效，"
            f"最早 {describe_moment(first, now)} 起可用",
            detail + " —— 想用这几张，可以安排在那个时间点点餐。",
        ))

    # 5) 买一送一：份量翻倍，值得单独点出来
    bogo = [c for c in live if "买一送一" in c.name]
    if bogo:
        out.append(Advice(
            ADVICE_INFO,
            f"{len(bogo)} 张券是「买一送一」",
            "、".join(c.name for c in bogo[:4]) + " —— 一次下单可得两份。",
        ))

    # 6) 不虚报：缺「适用商品」的券只能提示时机
    blind = [c for c in live if not c.item_codes and not c.item_tags]
    if blind:
        out.append(Advice(
            ADVICE_INFO,
            f"{len(blind)} 张券未提供适用商品信息",
            "这类券只用于提示时机，不参与精确算省额（宁可少算，不虚报）；"
            "下单前请在点单页确认它对哪件商品生效。",
        ))
    return out


# ---------------------------------------------------------------- 汇总


def advise(
    account: PointsAccount | None = None,
    coupons: list[Coupon] | None = None,
    products: list[RewardProduct] | None = None,
    now: datetime | None = None,
) -> list[Advice]:
    """汇总所有建议，按「优先 → 机会 → 提示」排序。"""
    now = now or datetime.now()
    items = advise_points(account, products, now) + advise_coupons(coupons or [], now)
    rank = {ADVICE_URGENT: 0, ADVICE_TIP: 1, ADVICE_INFO: 2}
    # Python 的 sort 是稳定的：同级别内保持上面的规则顺序
    items.sort(key=lambda a: rank.get(a.level, 3))
    return items


__all__ = ["advise", "advise_coupons", "advise_points"]
