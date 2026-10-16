"""领域模型。

设计约定
--------
1. 所有金额一律用「分」的整数表示（price_cents / discount_cents / ...）。
   浮点数在比价场景里会产生 0.1+0.2 这类误差，直接导致算错最优解，因此全程整数。
2. 商品与优惠券都是不可变对象（frozen），便于做集合运算与去重。
3. 优惠券的「适用范围」由 scope + item_codes + item_tags 三者共同描述，
   判定逻辑集中在 coupons.py，模型层不做业务判断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .schedule import Period


# ---------------------------------------------------------------- 商品

@dataclass(frozen=True)
class Item:
    """一个可售餐品（对应 MCP 的餐品编码）。

    code    : 餐品编码，来自麦当劳 MCP 的 query-meals
    name    : 餐品名称，用于展示与人工核对
    price   : 单价（分）
    category: 分类，例如「主食 / 配餐 / 饮品」
    tags    : 标签，例如 ('早餐', '含咖啡')，用于匹配「指定品类可用」的券
    """

    code: str
    name: str
    price_cents: int
    category: str = ""
    tags: tuple[str, ...] = ()

    @property
    def price_yuan(self) -> str:
        return f"¥{self.price_cents / 100:.2f}"


@dataclass
class Line:
    """购物车中的一行：商品 + 数量。"""

    item: Item
    qty: int = 1

    @property
    def amount_cents(self) -> int:
        return self.item.price_cents * self.qty

    def split(self, qty: int) -> tuple["Line", "Line"]:
        """拆出 qty 件，返回 (拆出的行, 剩余的行)。拆单策略会用到。"""
        if not 0 <= qty <= self.qty:
            raise ValueError(f"拆分量 {qty} 超出持有量 {self.qty}")
        return Line(self.item, qty), Line(self.item, self.qty - qty)


@dataclass
class Catalog:
    """某个门店的当前可售菜单快照。"""

    store_code: str
    store_name: str
    items: list[Item] = field(default_factory=list)

    def find(self, code: str) -> Optional[Item]:
        for it in self.items:
            if it.code == code:
                return it
        return None

    def search(self, keyword: str) -> list[Item]:
        """按关键词搜索餐品。

        匹配优先级（实测踩过的坑）：
          1. **编码精确匹配** —— 接口之间的名称可能不一致，
             同一 `507411` 在菜单里叫「麦咖啡™美式」、在券里叫「麦咖啡™冰美式中杯」，
             名称不可靠，编码才是稳定主键；
          2. **完全同名** —— 搜「巨无霸」必须命中单品，而不是「巨无霸三件套」；
          3. 名称更短的优先；
          4. 价格更低的优先。
        """
        kw = keyword.strip()
        if not kw:
            return []

        by_code = [it for it in self.items if it.code == kw]
        hits = [it for it in self.items if kw in it.name and it not in by_code]
        hits.sort(key=lambda it: (it.name != kw, len(it.name), it.price_cents))
        return by_code + hits


# ---------------------------------------------------------------- 优惠券

# 券的可用渠道
SCOPE_ACCOUNT = "account"   # 账号券：全国通用
SCOPE_STORE = "store"       # 门店券：仅当前门店可用


@dataclass(frozen=True)
class Coupon:
    """一张优惠券。

    threshold  : 使用门槛（分），0 表示无门槛
    discount   : 满足门槛后减免的金额（分）
    group      : 互斥组名。同组只能选一张，不同组可叠加；空字符串表示独立券
    valid_from / valid_to : 有效期，ISO 日期或日期时间字符串，空表示不限
    item_codes : 限定商品编码，空表示不限商品
    item_tags  : 限定商品标签，空表示不限
    """

    coupon_id: str
    name: str
    discount_cents: int
    threshold_cents: int = 0
    scope: str = SCOPE_ACCOUNT
    group: str = ""
    # 仅限该门店使用（空表示不限门店）。
    # 注意：从 `query-store-coupons` 取回的券已完成门店校验，
    # 可以不填；这个字段是给「已知门店限制但未预先校验」的场景用的。
    store_code: str = ""
    valid_from: str = ""
    valid_to: str = ""
    item_codes: tuple[str, ...] = ()
    item_tags: tuple[str, ...] = ()
    # 「用券价」型券：指定商品按此固定价购买。
    # 实测麦当劳的券**不是满减券**，而是「9.9 元买中杯冰美式」这种形式，
    # 所以与其设 discount/threshold，不如直接记录券面价。
    #   None = 不是固定价券；0 = 免费券（如「免费脆薯饼」）。
    # 千万不能用 0 表示「不适用」—— 那会让免费券被误判成普通券。
    fixed_price_cents: Optional[int] = None
    # 券码：核销凭据。同一 couponId 可对应多个 couponCode。
    coupon_code: str = ""
    note: str = ""
    # 「可用时段」—— 有效期内还限哪几天、几点到几点（如早餐券「周六 05:00-10:29」）。
    # 与有效期是**两层独立约束**：只判有效期会得出「现在就能用」的错误结论。
    period: Period = field(default_factory=Period)
    # 券的展示标签，如 ('今日到期', '到店专用')。
    # **刻意不参与求解** —— 这些是渠道 / 时效标记，不是「适用商品」，
    # 塞进 item_tags 会污染商品匹配，把「不知道适用商品」伪装成「商品不匹配」。
    badges: tuple[str, ...] = ()
    # 领取时间描述（如「今日收到」），仅用于展示。
    received_text: str = ""

    @property
    def is_fixed_price(self) -> bool:
        """是否为「指定商品按固定价购买」型券。"""
        return self.fixed_price_cents is not None

    @property
    def fixed_price_yuan(self) -> str:
        return f"¥{(self.fixed_price_cents or 0) / 100:.2f}"

    @property
    def mode_text(self) -> str:
        if self.is_fixed_price:
            return f"{self.fixed_price_yuan} 任选适用商品"
        if self.threshold_cents:
            return f"满 {self.threshold_yuan} 减 {self.discount_yuan}"
        return f"立减 {self.discount_yuan}"

    @property
    def discount_yuan(self) -> str:
        return f"¥{self.discount_cents / 100:.2f}"

    @property
    def threshold_yuan(self) -> str:
        return f"¥{self.threshold_cents / 100:.2f}"


# ---------------------------------------------------------------- 券可用性判定结果

# 判定原因码。把「不能用」拆成具体原因，是本项目的核心主张之一：
# 用户真正需要的不是「你有 12 张券」，而是「这 9 张为什么现在用不了」。
REASON_OK = "OK"
REASON_NOT_STARTED = "NOT_STARTED"
REASON_EXPIRED = "EXPIRED"
REASON_WRONG_STORE = "WRONG_STORE"
REASON_THRESHOLD = "THRESHOLD_NOT_MET"
REASON_ITEM_MISMATCH = "ITEM_MISMATCH"
REASON_ALREADY_USED = "ALREADY_USED"
REASON_NOT_BENEFICIAL = "NOT_BENEFICIAL"
REASON_OUT_OF_PERIOD = "OUT_OF_PERIOD"

REASON_TEXT = {
    REASON_OK: "可用",
    REASON_NOT_STARTED: "尚未开始",
    REASON_EXPIRED: "已过期",
    REASON_WRONG_STORE: "本店不可用",
    REASON_THRESHOLD: "未达使用门槛",
    REASON_ITEM_MISMATCH: "购物车中无适用商品",
    REASON_ALREADY_USED: "本单已使用过同类券",
    REASON_NOT_BENEFICIAL: "券面价高于商品原价，用了反而更贵",
    REASON_OUT_OF_PERIOD: "当前不在可用时段",
}


@dataclass(frozen=True)
class Verdict:
    """对一张券的可用性诊断结论。"""

    coupon: Coupon
    usable: bool
    reason_code: str
    reason: str
    saving_cents: int = 0
    # 门槛还差多少（分）；仅当 reason_code 为 THRESHOLD 时有意义
    shortfall_cents: int = 0

    def __str__(self) -> str:
        mark = "可用  " if self.usable else "不可用"
        detail = self.reason
        if self.reason_code == REASON_THRESHOLD and self.shortfall_cents:
            detail += f"（还差 ¥{self.shortfall_cents / 100:.2f}）"
        return f"[{mark}] {self.coupon.name} · {detail}"


# ---------------------------------------------------------------- 积分账户

@dataclass(frozen=True)
class PointsAccount:
    """麦享会积分账户（对应 MCP 的 `query-my-account`）。

    实测返回的全部字段都是**字符串数字**（"804"），这里统一转成 int。
    积分不是金额，因此**不套用「分」的口径**，避免与价格字段混淆。
    """

    account_id: str = ""
    currency: str = ""
    available: int = 0            # 可用积分
    accumulative: int = 0         # 累计积分
    used: int = 0                 # 已使用
    expired: int = 0              # 已过期
    frozen: int = 0               # 冻结中
    expiring_this_month: int = 0  # 本月将过期
    expiring_next_month: int = 0  # 下月将过期
    expiring_last_month: int = 0  # 上月已过期

    @property
    def masked_id(self) -> str:
        """账号 ID 脱敏展示：只保留末 4 位。

        账号 ID 属于个人标识，界面上没有完整展示的必要；
        保留末 4 位既够用户确认「是我的账号」，又不泄露完整标识。
        """
        raw = self.account_id
        if not raw:
            return "—"
        return f"****{raw[-4:]}" if len(raw) > 4 else raw

    @property
    def has_data(self) -> bool:
        return bool(self.account_id or self.currency or self.available or self.accumulative)


# ---------------------------------------------------------------- 积分商城

# 商品状态（来自服务端字段说明）
REWARD_IN_STOCK = 2        # 上架（可取）
REWARD_STATUS_TEXT = {
    1: "仓库中",
    2: "上架",
    3: "售罄",
    4: "下架",
    5: "预热",
}


@dataclass(frozen=True)
class RewardProduct:
    """积分商城里的一件可兑换商品（对应 MCP 的 `mall-points-products`）。

    `points = 0` 的商品（生日派对、品鉴会等）是**用钱买、不耗积分**的，
    推荐时必须单独归类 —— 否则会出现「0 积分可兑换生日派对」这种误导。
    """

    spu_id: int = 0
    name: str = ""
    points: int = 0
    price_yuan: str = ""
    selling: str = ""
    category: str = ""
    status: int = 0
    up_time: str = ""
    down_time: str = ""

    @property
    def status_text(self) -> str:
        return REWARD_STATUS_TEXT.get(self.status, str(self.status))

    @property
    def costs_points(self) -> bool:
        """是否真的消耗积分（0 积分商品只是付钱商品）。"""
        return self.points > 0

    def redeemable_at(self, moment) -> bool:
        """此刻是否处于可兑换窗口：已上架，且当前时间落在起止之间。"""
        return self.state_text(moment) == "可兑换"

    def state_text(self, moment) -> str:
        """展示用的可兑换状态。

        实测 `mall-points-products` 会把**已经下架但 status 仍为 2** 的历史商品
        一起返回（如兑换期早已结束的旧餐券），因此不能只看 status，
        必须把上下架时间也纳入判断，否则界面会给出「可兑换」的错误暗示。
        """
        from .schedule import parse_datetime

        if self.status != REWARD_IN_STOCK:
            return self.status_text
        start = parse_datetime(self.up_time)
        end = parse_datetime(self.down_time)
        if start and moment < start:
            return "未开始"
        if end and moment > end:
            return "已结束"
        return "可兑换"

    def affordable(self, points: int) -> bool:
        return self.costs_points and self.points <= points


# ---------------------------------------------------------------- 建议

ADVICE_URGENT = "urgent"     # 会损失已有权益，优先处理
ADVICE_TIP = "tip"           # 能省钱 / 能用上的机会
ADVICE_INFO = "info"         # 中性信息

ADVICE_LEVEL_TEXT = {
    ADVICE_URGENT: "优先",
    ADVICE_TIP: "机会",
    ADVICE_INFO: "提示",
}


@dataclass(frozen=True)
class Advice:
    """一条基于真实账号数据得出的建议。

    每条建议都由确定性规则算出（见 `advisor.py`），不由大模型生成 ——
    这样用户能顺着数字自己复核「为什么推荐这个」。
    """

    level: str
    title: str
    detail: str = ""

    @property
    def level_text(self) -> str:
        return ADVICE_LEVEL_TEXT.get(self.level, self.level)
