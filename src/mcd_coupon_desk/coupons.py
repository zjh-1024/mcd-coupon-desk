"""优惠券可用性判定。

这是本项目的第一个立足点：**不只回答「哪张券最省」，还要回答「其余券为什么用不了」**。

判定顺序刻意按「用户改不动的因素」优先排列 ——
有效期、门店归属是先决条件，改不动；门槛和商品匹配则可以通过加购来满足。
先报不可改变的原因，用户才不会白折腾。
"""

from __future__ import annotations

from datetime import datetime

from .models import (
    REASON_EXPIRED,
    REASON_ITEM_MISMATCH,
    REASON_NOT_BENEFICIAL,
    REASON_NOT_STARTED,
    REASON_OK,
    REASON_OUT_OF_PERIOD,
    REASON_TEXT,
    REASON_THRESHOLD,
    REASON_WRONG_STORE,
    Catalog,
    Coupon,
    Line,
    Verdict,
)
from .schedule import describe_moment, parse_datetime


def subtotal_cents(lines: list[Line]) -> int:
    """购物车小计（分）。"""
    return sum(ln.amount_cents for ln in lines)


# 保留旧名字，避免外部（含历史测试）因改名而失效
_parse_time = parse_datetime


def _match_items(coupon: Coupon, lines: list[Line]) -> list[Line]:
    """找出这张券能作用的行。空 item_codes 且空 item_tags 表示不限商品。"""
    if not coupon.item_codes and not coupon.item_tags:
        return list(lines)

    hit: list[Line] = []
    codes = set(coupon.item_codes)
    tags = set(coupon.item_tags)
    for ln in lines:
        if codes and ln.item.code in codes:
            hit.append(ln)
            continue
        if tags and tags.intersection(ln.item.tags):
            hit.append(ln)
    return hit


def evaluate(
    coupon: Coupon,
    lines: list[Line],
    store_code: str,
    now: datetime | None = None,
) -> Verdict:
    """判定一张券在「当前门店 + 当前购物车 + 当前时间」下是否可用。

    返回的 Verdict 一定带有可读原因，调用方不需要再猜。
    """
    now = now or datetime.now()

    # 1) 有效期 —— 用户改不动
    start = _parse_time(coupon.valid_from)
    end = _parse_time(coupon.valid_to)
    if start and now < start:
        return Verdict(coupon, False, REASON_NOT_STARTED, f"{REASON_TEXT[REASON_NOT_STARTED]}（{coupon.valid_from} 起）")
    if end and now > end:
        return Verdict(coupon, False, REASON_EXPIRED, f"{REASON_TEXT[REASON_EXPIRED]}（{coupon.valid_to} 截止）")

    # 2) 门店归属 —— 用户改不动。
    #    只认显式的 store_code 字段。不要用备注文本去猜门店，
    #    备注里可能装着商品名之类的任意内容。
    if coupon.store_code and store_code and coupon.store_code != store_code:
        return Verdict(coupon, False, REASON_WRONG_STORE, REASON_TEXT[REASON_WRONG_STORE])

    # 2b) 可用时段 —— 有效期内还限「哪几天 + 几点到几点」（如早餐券仅周六 05:00-10:29）。
    #     放在「适用商品」之前是有意的：时段不通过时，**加购也救不了这张券**，
    #     直说「什么时候再来」才是有用的信息；先报「无适用商品」会把用户引向白折腾。
    period = getattr(coupon, "period", None)
    if period is not None and not period.unrestricted and not period.is_open_at(now):
        upcoming = period.next_open(now)
        hint = f"，下次可用：{describe_moment(upcoming, now)}" if upcoming else ""
        return Verdict(
            coupon, False, REASON_OUT_OF_PERIOD,
            f"{REASON_TEXT[REASON_OUT_OF_PERIOD]}（{period.describe()}）{hint}",
        )

    # 3) 适用商品 —— 可以通过加购满足
    hit_lines = _match_items(coupon, lines)
    if not hit_lines:
        return Verdict(coupon, False, REASON_ITEM_MISMATCH, REASON_TEXT[REASON_ITEM_MISMATCH])

    # 3b) 「指定商品按固定价购买」型券（实测麦当劳的主流券型）
    #     省额 = 适用商品原价 − 券面价。取最贵的那件来用，省得最多。
    if coupon.is_fixed_price:
        best = max(hit_lines, key=lambda ln: ln.item.price_cents)
        saving = best.item.price_cents - (coupon.fixed_price_cents or 0)
        if saving <= 0:
            # 券面价已经不低于原价，用了反而更贵 —— 明确说出来，而不是默默当成 0
            return Verdict(
                coupon, False, REASON_NOT_BENEFICIAL,
                f"{REASON_TEXT[REASON_NOT_BENEFICIAL]}（券面 {coupon.fixed_price_yuan}，"
                f"商品 {best.item.price_yuan}）",
            )
        return Verdict(coupon, True, REASON_OK, REASON_TEXT[REASON_OK], saving)

    # 4) 门槛 —— 可以通过加购满足
    #    注意：门槛通常按整单小计判定，部分券按适用商品小计判定。
    #    这里保守取「适用商品小计」与「整单小计」的较小值，宁可判不可用也不虚报节省。
    base = min(subtotal_cents(hit_lines), subtotal_cents(lines))
    if base < coupon.threshold_cents:
        short = coupon.threshold_cents - base
        return Verdict(coupon, False, REASON_THRESHOLD, REASON_TEXT[REASON_THRESHOLD], 0, short)

    # 5) 通过全部检查：节省额不能超过购物车金额本身
    saving = min(coupon.discount_cents, subtotal_cents(lines))
    return Verdict(coupon, True, REASON_OK, REASON_TEXT[REASON_OK], saving)


def diagnose(
    coupons: list[Coupon],
    lines: list[Line],
    store_code: str,
    now: datetime | None = None,
) -> list[Verdict]:
    """对一整个券包做体检，按「可用在前、节省额降序」排序。"""
    verdicts = [evaluate(c, lines, store_code, now) for c in coupons]
    verdicts.sort(key=lambda v: (not v.usable, -v.saving_cents, v.coupon.coupon_id))
    return verdicts


def applicable_for_catalog(coupons: list[Coupon], catalog: Catalog, store_code: str) -> list[Coupon]:
    """筛出与门店/时间相关、但还需购物车才能最终判定的券（用于「先看有哪些券」）。"""
    now = datetime.now()
    keep: list[Coupon] = []
    for c in coupons:
        start = _parse_time(c.valid_from)
        end = _parse_time(c.valid_to)
        if start and now < start:
            continue
        if end and now > end:
            continue
        if c.store_code and store_code and c.store_code != store_code:
            continue
        keep.append(c)
    return keep


__all__ = [
    "applicable_for_catalog",
    "diagnose",
    "evaluate",
    "subtotal_cents",
]
