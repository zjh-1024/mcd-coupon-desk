"""数据源适配层：把麦当劳 MCP 的真实返回翻译成本项目的领域模型。

两套实现
--------
OfflineSource —— 读本地样例 JSON。无需 Token、无需联网，也是单元测试的数据来源。
McpSource     —— 真实调用麦当劳 MCP。

真实接口形态（2026-10-10 实测，非推测）
----------------------------------------
1. **返回不是纯 JSON**，而是「说明文字 + 内嵌原始 JSON + 尾部展示要求」。
   因此先用 `textparse.extract_payload()` 做括号配平扫描把 JSON 抠出来。
2. **`query-meals` 的 `data.meals` 是以餐品编码为键的字典**，不是数组：

       "meals": {
         "9900005456": {"name":"麦辣鸡腿汉堡三件套","currentPrice":"38",
                        "originalPrice":"48.5","canWithOrder":false},
         "521906":   {"name":"玫瑰红糖糍粑风味派","currentPrice":"5",
                      "originalPrice":"10.5","discountType":"促销优惠"}
       }

   编码只在**键**上，对象内部没有 code 字段。
3. **价格是字符串，单位「元」**（"38"、"48.5"），不是分。换算成分交给 `to_cents`。
4. `query-nearby-stores` 的 `city` 与 `keyword` **必须同时传**，
   只传 `city` 会返回 code=600058「城市名或者关键词不能为空」。
5. 门店编码是 **7 位纯数字**（如 `1410419`），服务端明确要求「storeCode 必须全部由数字组成」。

待验证项（不要当成已知）
------------------------
* `query-store-coupons` / `query-my-coupons` 的**券字段结构尚未验证** ——
  测试账号当前券包为空（返回 `data: []`），拿不到样本。
  下面的 `coupon_from_raw` 仍是容错映射，需在券包非空后收敛。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .mcp_client import McpClient, McpError
from .models import SCOPE_ACCOUNT, SCOPE_STORE, Catalog, Coupon, Item
from .textparse import (
    PayloadError,
    extract_payload,
    is_empty_result,
    parse_coupon_markdown,
    parse_nutrition_table,
    require_ok,
)

# 麦当劳 MCP 的枚举值（来自服务端 inputSchema，实测）
ORDER_TYPE_STORE = 1        # 到店（含到店自取 + 得来速车道取餐）
ORDER_TYPE_DELIVERY = 2     # 外送（含麦乐送 + 企业团餐）

BE_TYPE_STORE = 1           # 到店自取
BE_TYPE_DELIVERY = 2        # 麦乐送到家
BE_TYPE_DRIVE_THRU = 5      # 得来速（DT）
BE_TYPE_GROUP_MEAL = 6      # 企业团餐

# 这些场景必须带 beCode（到店自取不需要）
BE_TYPES_NEEDING_BE_CODE = (BE_TYPE_DRIVE_THRU, BE_TYPE_DELIVERY, BE_TYPE_GROUP_MEAL)


# ---------------------------------------------------------------- 通用取值工具


def pick(raw: dict, *names: str, default: Any = None) -> Any:
    """按顺序尝试多个键名，返回第一个非空值。"""
    for name in names:
        if name in raw:
            value = raw[name]
            if value is not None and value != "":
                return value
    return default


def to_cents(value: Any) -> int:
    """把价格换算成「分」。

    实测麦当劳返回的是**字符串、单位元**（"38"、"48.5"）。规则：
      * 带小数点      → 按「元」处理
      * 整数且 < 1000 → 按「元」处理
      * 整数且 ≥ 1000 → 按「分」处理（兜底，防止上游换单位）

    校准样本："38" → 3800，"48.5" → 4850，"5" → 500，均取自真实菜单。
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value if value >= 1000 else value * 100
    if isinstance(value, float):
        return int(round(value * 100))
    text = str(value).strip().replace(",", "").replace("¥", "").replace("元", "")
    if not text:
        return 0
    try:
        if "." in text:
            return int(round(float(text) * 100))
        number = int(text)
        return number if number >= 1000 else number * 100
    except ValueError:
        return 0


def _clean(text: Any) -> str:
    """清理服务端返回里的换行与多余空白。

    实测 `query-meals` 的分类名带换行（"鸡肉汉堡\\n/卷"），直接用会污染展示。
    """
    return " ".join(str(text or "").split())


def _as_tuple(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Iterable):
        return tuple(str(v) for v in value)
    return ()


# ---------------------------------------------------------------- 券名解析


# 「9.9元中杯冰美式」/「23.9元四件套」这类券面价
_TITLE_PRICE = re.compile(r"(\d+(?:\.\d+)?)\s*元")
# 「免费脆薯饼」这类零元券
_TITLE_FREE = re.compile(r"^\s*免费")
# 兜底：「满30减12」这类满减券（真实样本中未出现，保留以防上游混用）
_TITLE_THRESHOLD = re.compile(r"满\s*(\d+(?:\.\d+)?)\s*减\s*(\d+(?:\.\d+)?)")

# 有效期区间：「2026-10-10 00:00:00-2026-10-16 23:59:59」
_VALID_RANGE = re.compile(
    r"(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?)"
    r"\s*[-~至]\s*"
    r"(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?)"
)


def parse_coupon_price(title: str) -> int | None:
    """从券名解析「用券价」，单位为分。

    ⚠️ 这是对上游 schema 的**补偿措施**：实测 `query-store-coupons` 的返回里
    没有结构化的折扣字段，券面价只出现在券名里。

    * 「免费脆薯饼」   → 0（零元券）
    * 「9.9元中杯冰美式」→ 990
    * 无法识别         → None（调用方据此降级，不要臆造一个价格）
    """
    text = _clean(title)
    if not text:
        return None
    if _TITLE_FREE.search(text) or "0元" in text:
        return 0
    match = _TITLE_PRICE.search(text)
    if not match:
        return None
    try:
        return int(round(float(match.group(1)) * 100))
    except ValueError:
        return None


def parse_threshold_discount(title: str) -> tuple[int, int]:
    """从券名解析（门槛, 减免），用于兜底满减券；识别不到返回 (0, 0)。"""
    match = _TITLE_THRESHOLD.search(_clean(title))
    if not match:
        return 0, 0
    try:
        return int(round(float(match.group(1)) * 100)), int(round(float(match.group(2)) * 100))
    except ValueError:
        return 0, 0


def parse_validity(text: str) -> tuple[str, str]:
    """从 `tradeDateTime` 解析出 (起始, 截止)。

    实测格式为「2026-10-10 00:00:00-2026-10-16 23:59:59」——
    连字符既是日期分隔符又是区间分隔符，所以必须用带上下文的模式匹配，
    不能简单 split('-')。
    """
    raw = str(text or "").strip()
    if not raw:
        return "", ""
    match = _VALID_RANGE.search(raw)
    if match:
        return match.group(1), match.group(2)
    return "", raw


# ---------------------------------------------------------------- 原始对象 → 领域模型


def item_from_raw(raw: dict, code: str = "") -> Item:
    """把一条餐品记录翻译成 Item。

    `code` 单独传入，因为实测 `query-meals` 把编码放在**字典的键**上，
    对象内部没有 code 字段。
    """
    return Item(
        code=code or str(pick(raw, "code", "itemCode", "item_code", "sku", "id", default="")),
        name=_clean(pick(raw, "name", "itemName", "productName", "title", default="未命名餐品")),
        price_cents=to_cents(pick(raw, "currentPrice", "price", "priceCents", "salePrice")),
        category=_clean(pick(raw, "category", "categoryName", "group", default="")),
        tags=_as_tuple(pick(raw, "tags", "labels", "tag")),
    )


def coupon_from_raw(raw: dict, scope: str = SCOPE_ACCOUNT) -> Coupon:
    """把一条优惠券记录翻译成 Coupon。

    真实字段（2026-10-10 实测 `query-store-coupons`，字段名取自服务端自带的字段说明）::

        {"title": "9.9元中杯冰美式",
         "couponId": "SYNTH-COUPON-ID-0001",   # 券ID，分组标识（此处为合成值）
         "couponCode": "SYNTH-CODE-0001",      # 券码，核销凭据（此处为合成值）
         "tradeDateTime": "2026-10-10 00:00:00-2026-10-16 23:59:59",
         "products": [{"productCode": "507411", "productName": "麦咖啡™冰美式中杯"}]}

    ⚠️ **关键差异**：返回里**没有折扣金额或门槛字段** ——
    麦当劳的券不是「满减券」，而是「指定商品按固定价购买」。
    券面价只写在 `title` 里，因此只能从券名解析（见 `parse_coupon_price`）。
    这是对上游 schema 的补偿措施，已在 MCP_INTEGRATION.md 中列为已知风险。

    `scope` 由调用方指定：`query-store-coupons` 返回的券已通过门店校验，
    应传 SCOPE_STORE；`query-my-coupons` 的券未做门店校验，保持 SCOPE_ACCOUNT。
    """
    title = _clean(pick(raw, "title", "couponName", "name", "desc", default="未命名优惠券"))

    # 券面条件有两种表达方式，按优先级取值：
    #   ① 显式字段（threshold / discountAmount）—— 离线样例用这种方式，
    #      也兼容未来上游补齐结构化字段；
    #   ② 从券名解析 —— 真实接口目前**只给券名**（「9.9元中杯冰美式」「免费脆薯饼」）。
    explicit_discount = to_cents(pick(raw, "discountAmount", "discount"))
    explicit_threshold = to_cents(pick(raw, "threshold", "minAmount"))
    if explicit_discount or explicit_threshold:
        fixed_price: int | None = None
        threshold, discount = explicit_threshold, explicit_discount
    else:
        fixed_price = parse_coupon_price(title)
        threshold, discount = parse_threshold_discount(title)

    # 有效期同理：显式字段优先，否则解析 tradeDateTime 区间
    explicit_from = str(pick(raw, "validFrom", "startTime", default=""))
    explicit_to = str(pick(raw, "validTo", "endTime", default=""))
    if explicit_from or explicit_to:
        valid_from, valid_to = explicit_from, explicit_to
    else:
        valid_from, valid_to = parse_validity(str(pick(raw, "tradeDateTime", default="")))

    products = raw.get("products") or []
    product_codes = tuple(
        str(p.get("productCode"))
        for p in products
        if isinstance(p, dict) and p.get("productCode")
    )
    product_names = [
        _clean(p.get("productName")) for p in products if isinstance(p, dict) and p.get("productName")
    ]

    return Coupon(
        coupon_id=str(pick(raw, "couponId", "coupon_id", "id", "code", default="")),
        name=title,
        discount_cents=discount,
        threshold_cents=threshold,
        scope=scope,
        store_code=str(pick(raw, "storeCode", "store_code", default="")),
        group=str(pick(raw, "exclusiveGroup", "group", "stackGroup", default=""))
        or ("满减" if threshold else ""),
        valid_from=valid_from,
        valid_to=valid_to,
        item_codes=product_codes or _as_tuple(pick(raw, "itemCodes", "applicableItems")),
        item_tags=_as_tuple(pick(raw, "itemTags", "tags")),
        fixed_price_cents=fixed_price,
        coupon_code=str(pick(raw, "couponCode", "coupon_code", default="")),
        note="、".join(product_names)[:120] or str(pick(raw, "remark", "note", default="")),
    )


# ---------------------------------------------------------------- 离线数据源


class OfflineSource:
    """离线数据源：从 JSON 文件读取门店、菜单与券包。

    字段结构与线上保持一致（`code` + `currentPrice`），
    这样离线结果与接真实接口后的行为不会出现结构性偏差。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        store = payload.get("store", {})
        self._catalog = Catalog(
            store_code=str(store.get("code", "")),
            store_name=str(store.get("name", "")),
            items=[item_from_raw(x, code=str(x.get("code", ""))) for x in payload.get("items", [])],
        )
        self._coupons = [
            coupon_from_raw(x, scope=str(x.get("scope") or SCOPE_ACCOUNT))
            for x in payload.get("coupons", [])
        ]

    @property
    def store_code(self) -> str:
        return self._catalog.store_code

    def menu(self, store_code: str | None = None) -> Catalog:
        return self._catalog

    def coupons(self) -> list[Coupon]:
        return list(self._coupons)

    def account_coupons(self) -> list[Coupon]:
        return [c for c in self._coupons if c.scope == SCOPE_ACCOUNT]

    def store_coupons(self, store_code: str | None = None) -> list[Coupon]:
        return [c for c in self._coupons if c.scope == SCOPE_STORE]

    def nutrition(self) -> list[dict]:
        return []


# ---------------------------------------------------------------- 真实数据源


@dataclass
class McpSource:
    """真实数据源：调用麦当劳 MCP。

    所有传参都走 `_args()`：先从服务端 `tools/list` 取 inputSchema，
    再按 schema 声明的属性名回填。服务端改字段名时只需改候选表。
    """

    client: McpClient
    order_type: int = ORDER_TYPE_STORE
    be_type: int = BE_TYPE_STORE
    be_code: str = ""
    store_code: str = ""
    stores: list[dict] = field(default_factory=list)
    # 因缺少「适用商品」而未能纳入求解的账号券张数（用于如实告知，不虚报）
    skipped_account_coupons: int = 0

    # -------------------------------------------------- 参数装配

    def _args(self, tool_name: str, candidates: dict[str, Any]) -> dict:
        try:
            spec = next(s for s in self.client.list_tools() if s.name == tool_name)
        except StopIteration as exc:
            available = ", ".join(s.name for s in self.client.list_tools())
            raise McpError(f"服务端没有工具 {tool_name}。当前可用：{available}") from exc

        props = spec.properties
        args = {k: v for k, v in candidates.items() if k in props and v is not None}

        missing = [k for k in spec.required if k not in args]
        if missing:
            raise McpError(
                f"工具 {tool_name} 缺少必填参数 {missing}；"
                f"服务端声明的字段为 {list(props)}。请据此补充候选表。"
            )
        return args

    def _scope(self, store_code: str = "") -> dict[str, Any]:
        """组装 orderType / beType / beCode 这组「下单场景」参数。"""
        args: dict[str, Any] = {"orderType": self.order_type, "beType": self.be_type}
        if store_code:
            args["storeCode"] = store_code
        if self.be_code and self.be_type in BE_TYPES_NEEDING_BE_CODE:
            args["beCode"] = self.be_code
        return args

    @staticmethod
    def _call(client: McpClient, tool: str, args: dict) -> Any:
        text = client.call_tool_json(tool, args)
        try:
            return require_ok(extract_payload(text), tool)
        except PayloadError as exc:
            raise McpError(str(exc)) from exc

    @staticmethod
    def _call_list(client: McpClient, tool: str, args: dict) -> list:
        """用于「列表型」工具。

        实测：券包为空时服务端会直接回一句大白话（如「暂无可用优惠券」），
        不返回 JSON。这属于**正常的空结果**，不应报错；
        但只有确实不含 JSON 且文本符合空结果特征时才这么判定，
        否则说明是真实异常，照旧抛出。
        """
        text = client.call_tool_json(tool, args)
        try:
            data = require_ok(extract_payload(text), tool)
        except PayloadError:
            if is_empty_result(text):
                return []
            raise McpError(f"{tool} 返回无法解析：{str(text)[:200]}") from None
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # 少数列表型工具会把记录包在对象里，统一归一成「记录列表」
            return _iter_rows(data)
        return []

    # -------------------------------------------------- 门店

    def nearby_stores(self, city: str, keyword: str = "") -> list[dict]:
        """查询附近门店（只读到店场景，beType 取 1 或 5）。

        实测约束：`city` 与 `keyword` **必须同时非空** ——
        只传 `city` 会返回 code=600058「城市名或者关键词不能为空」。
        """
        if not city or not keyword:
            raise McpError("query-nearby-stores 需要同时提供 city 与 keyword（实测约束）")
        data = self._call(self.client, "query-nearby-stores", self._args("query-nearby-stores", {
            "city": city,
            "keyword": keyword,
            "beType": self.be_type,
            "searchType": 2,          # 2 = 按位置搜索
        }))
        self.stores = data if isinstance(data, list) else []
        return self.stores

    def find_store_code(self, city: str, keyword: str = "") -> str:
        """按城市与关键词取第一个门店编码，并记忆到实例上。"""
        stores = self.nearby_stores(city, keyword)
        for store in stores:
            code = str(pick(store, "storeCode", "code", default=""))
            if code:
                self.store_code = code
                self.be_code = str(pick(store, "beCode", default="") or "")
                return code
        raise McpError(f"在「{city} / {keyword or city}」未找到可用门店")

    # -------------------------------------------------- 菜单

    def menu(self, store_code: str) -> Catalog:
        data = self._call(self.client, "query-meals", self._args("query-meals", self._scope(store_code)))
        meals = (data or {}).get("meals") or {}
        categories = (data or {}).get("categories") or []

        # categories 里只有 code 与 tags，建立索引补全分类与标签
        meta: dict[str, dict] = {}
        for cat in categories:
            if not isinstance(cat, dict):
                continue
            cat_name = _clean(cat.get("name", ""))
            for entry in cat.get("meals", []) or []:
                if not isinstance(entry, dict):
                    continue
                code = str(entry.get("code", ""))
                if not code:
                    continue
                slot = meta.setdefault(code, {"category": "", "tags": []})
                slot["category"] = slot["category"] or cat_name
                slot["tags"].extend(entry.get("tags", []) or [])

        items: list[Item] = []
        if isinstance(meals, dict):
            for code, raw in meals.items():
                if not isinstance(raw, dict):
                    continue
                base = item_from_raw(raw, code=str(code))
                info = meta.get(str(code), {})
                items.append(
                    Item(
                        code=base.code,
                        name=base.name,
                        price_cents=base.price_cents,
                        category=base.category or str(info.get("category", "")),
                        tags=tuple(dict.fromkeys(base.tags + tuple(info.get("tags", [])))),
                    )
                )
        elif isinstance(meals, list):
            for raw in meals:
                if isinstance(raw, dict):
                    items.append(item_from_raw(raw))

        store_name = ""
        for store in self.stores:
            if str(pick(store, "storeCode", "code", default="")) == store_code:
                store_name = str(pick(store, "storeName", "name", default=""))
                break
        return Catalog(store_code=store_code, store_name=store_name, items=items)

    # -------------------------------------------------- 券

    def account_coupons(self) -> list[Coupon]:
        """我拥有的券。

        实测该接口返回的是 **markdown 卡片**（而 `query-store-coupons` 返回 JSON，
        同属券类工具却格式不同），且**不包含适用商品** ——
        因此这些券无法参与精确求解，只在报告里展示。

        官方说明也明确：该接口不做门店 / 渠道校验，不承诺可用于当前订单。
        """
        text = self.client.call_tool_json("query-my-coupons", {"page": "1", "pageSize": "200"})
        try:
            rows = _iter_rows(require_ok(extract_payload(text), "query-my-coupons"))
            if rows:
                return [coupon_from_raw(r) for r in rows]
        except PayloadError:
            pass

        if is_empty_result(text):
            return []

        return [
            coupon_from_raw(
                {"title": block["title"], "tradeDateTime": block["tradeDateTime"]},
                scope=SCOPE_ACCOUNT,
            )
            for block in parse_coupon_markdown(text)
        ]

    def store_coupons(self, store_code: str) -> list[Coupon]:
        """指定门店 + 订单类型下**可用的**券 —— 本项目的权威数据源。

        这是唯一同时提供「适用商品编码」与「券面价」的接口，
        因此求解器只信任它。
        """
        data = self._call_list(
            self.client,
            "query-store-coupons",
            self._args("query-store-coupons", self._scope(store_code)),
        )
        return [coupon_from_raw(r, scope=SCOPE_STORE) for r in _iter_rows(data)]

    def coupons(self, store_code: str = "") -> list[Coupon]:
        """合并「本店当前可用券」与「我拥有的券」，按 coupon_id 去重。

        与 `OfflineSource.coupons()` 同名同义，让 CLI 无需区分两种数据源。

        合并规则刻意保守：门店券优先；账号券只在**信息完整**（有适用商品）时补入。
        缺适用商品的券会被跳过并计数，而不是当成「全场通用券」——
        后者会算出虚高的节省额，用户照着下单会踩空。
        """
        code = store_code or self.store_code
        merged: dict[str, Coupon] = {}
        if code:
            for coupon in self.store_coupons(code):
                merged[coupon.coupon_id] = coupon

        self.skipped_account_coupons = 0
        try:
            owned = self.account_coupons()
        except McpError:
            owned = []
        for coupon in owned:
            if coupon.coupon_id in merged:
                continue
            if not coupon.item_codes and not coupon.item_tags:
                # 不知道适用商品与品类 —— 当成「全场通用」会算出虚高节省额
                self.skipped_account_coupons += 1
                continue
            merged[coupon.coupon_id] = coupon
        return list(merged.values())

    def available_coupons(self) -> Any:
        """麦麦省当前可领取的券（只读列表）。"""
        return self._call(self.client, "available-coupons", {})

    def auto_bind_coupons(self, confirmed: bool = False) -> Any:
        """一键领取麦麦省全部可领券。

        ⚠️ **写操作**：会真实变更用户账号的券包。必须由用户显式确认后才可调用，
        因此这里要求显式传 confirmed=True，否则直接拒绝执行。
        """
        if not confirmed:
            raise McpError("auto_bind_coupons 是写操作，需用户显式确认后传 confirmed=True")
        return self._call(self.client, "auto-bind-coupons", {})

    # -------------------------------------------------- 计价

    def calculate_price(self, store_code: str, lines, coupons) -> Any:
        """用官方计价工具复核本地算出的方案（只读）。"""
        items = [{"code": ln.item.code, "quantity": ln.qty} for ln in lines]
        coupon_ids = [c.coupon_id for c in coupons] or None
        return self._call(self.client, "calculate-price", self._args("calculate-price", {
            **self._scope(store_code),
            "items": items,
            "products": items,
            "couponIds": coupon_ids,
            "coupons": coupon_ids,
        }))

    # -------------------------------------------------- 时间 / 营养

    def now_time_info(self) -> Any:
        return self._call(self.client, "now-time-info", {})

    def nutrition(self) -> list[dict]:
        return parse_nutrition_table(self._call(self.client, "list-nutrition-foods", {}))


# ---------------------------------------------------------------- 返回结构归一


def _iter_rows(data: Any) -> list[dict]:
    """从各种可能的返回结构里捞出「记录列表」。"""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for key in ("list", "records", "rows", "coupons", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
        values = [v for v in data.values() if isinstance(v, dict)]
        if values and len(values) == len(data):
            return values
    return []


__all__ = [
    "BE_TYPE_DELIVERY",
    "BE_TYPE_DRIVE_THRU",
    "BE_TYPE_GROUP_MEAL",
    "BE_TYPE_STORE",
    "McpSource",
    "OfflineSource",
    "ORDER_TYPE_DELIVERY",
    "ORDER_TYPE_STORE",
    "coupon_from_raw",
    "item_from_raw",
    "parse_coupon_price",
    "parse_threshold_discount",
    "parse_validity",
    "pick",
    "to_cents",
]
