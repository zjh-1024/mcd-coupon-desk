"""解析麦当劳 MCP 的返回文本。

实测发现（2026-10-10，真实接口）
--------------------------------
麦当劳 MCP 的工具返回**不是纯 JSON**，而是「给大模型看的说明文字 + 内嵌原始 JSON」，
再在末尾追加一句展示要求。典型形态：

    每个门店的基本信息按单行展示：
    - **门店编号**: data[].storeCode, 唯一编号，工具入参
    ...
    {"success":true,"code":200,...,"data":[...]}
    ## 展示结果时，每个门店的 storeCode 和 beCode 必须完整展示

部分工具（如 `list-nutrition-foods`）则把**整张表以字符串形式**塞在 data 里。

因此本项目必须先把原始 JSON 从句子里抠出来，再做字段映射。
抠取用**括号配平扫描**而不是正则 —— 说明文字里也含 `{}`，正则会误匹配。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 统一响应里表示失败的字段
CODE_OK = 200


class PayloadError(ValueError):
    """返回文本里没有可用的内嵌 JSON。"""


def extract_payload(text: Any) -> dict:
    """从 MCP 返回文本中提取内嵌的原始响应对象。

    找不到可解析的 JSON 时抛 PayloadError，由调用方决定如何降级 ——
    绝不返回一个「看起来像成功」的空结构。
    """
    if isinstance(text, (dict, list)):
        return _as_response(text)

    raw = str(text)
    for anchor in ('{"success"', '{"code"', "{"):
        start = raw.find(anchor)
        while start >= 0:
            candidate = _balanced_object(raw, start)
            if candidate is not None:
                return _as_response(candidate)
            start = raw.find(anchor, start + 1)
    raise PayloadError("返回文本中未找到可解析的 JSON 响应")


def _as_response(obj: Any) -> dict:
    if isinstance(obj, list):
        return {"success": True, "code": CODE_OK, "data": obj}
    return obj


def _balanced_object(text: str, start: int) -> Any | None:
    """从 text[start] 处开始做括号配平扫描，返回解析出的对象或 None。"""
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:idx + 1])
                except json.JSONDecodeError:
                    return None
    return None


# 服务端在「结果为空」时会直接用一句大白话回复，不返回 JSON。
# 例如 query-my-coupons 券包为空时返回「暂无可用优惠券」。
EMPTY_HINTS = (
    "暂无",
    "没有",
    "未找到",
    "不存在",
    "无可用",
    "无符合",
    "空",
)


def is_empty_result(text: Any) -> bool:
    """判断返回文本是否是「空结果」的大白话回复（而非错误）。

    只有在文本里**不含任何 JSON**时才成立 —— 否则「订单中没有…」这类
    真实业务数据会被误判成空。
    """
    raw = str(text)
    if "{" in raw:
        return False
    return any(hint in raw for hint in EMPTY_HINTS)


def require_ok(payload: dict, tool: str) -> Any:
    """校验响应成功并返回 data；失败时把服务端的错误码与文案带出来。"""
    if payload.get("success") is True:
        return payload.get("data")
    code = payload.get("code")
    message = payload.get("message") or payload.get("msg") or "未知错误"
    raise PayloadError(f"{tool} 调用失败（code={code}）：{message}")


# `query-my-coupons` 返回的是 markdown 卡片（而 `query-store-coupons` 返回 JSON，
# 同属券类工具却格式不同）。形如：

#     ## 9.9元中杯冰美式
#     - **优惠**: ¥9.9 (用券价格)
#     - **有效期**: 2026-10-10 00:00-2026-10-16 23:59
#     - **标签**: 到店专用、外送专用
_COUPON_BLOCK = re.compile(r"(?m)^##[ \t]+(.+?)[ \t]*$")
_COUPON_FIELD = re.compile(r"(?m)^-[ \t]+\*\*(.+?)\*\*[:：][ \t]*(.*)$")
_FIELD_PRICE = re.compile(r"¥[ \t]*(\d+(?:\.\d+)?)")


def parse_coupon_markdown(text: Any) -> list[dict]:
    """解析 `query-my-coupons` 的 markdown 券列表。

    返回形如::

        [{"title": "9.9元中杯冰美式", "priceCents": 990,
          "tradeDateTime": "2026-10-10 00:00-2026-10-16 23:59",
          "tags": "到店专用、外送专用"}]

    ⚠️ 该接口**不返回适用商品**，因此解析出的券无法直接参与精确求解。
    调用方应据此降级（本项目里这类券只用于展示，不进优化器）。
    """
    raw = str(text or "")
    heads = list(_COUPON_BLOCK.finditer(raw))
    blocks: list[dict] = []
    for idx, head in enumerate(heads):
        end = heads[idx + 1].start() if idx + 1 < len(heads) else len(raw)
        body = raw[head.end():end]
        fields = {k.strip(): v.strip() for k, v in _COUPON_FIELD.findall(body)}
        price_match = _FIELD_PRICE.search(fields.get("优惠", ""))
        blocks.append({
            "title": head.group(1).strip(),
            "priceCents": (
                int(round(float(price_match.group(1)) * 100)) if price_match else None
            ),
            "tradeDateTime": fields.get("有效期", ""),
            "tags": fields.get("标签", ""),
        })
    return blocks


def parse_nutrition_table(data: Any) -> list[dict]:
    """解析 `list-nutrition-foods` 返回的营养表。

    实测该工具的 data 是一段「表头 + 逗号分隔行」的文本：

        [160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
          猪柳麦满分,null,1288,308,16,16,24,781,213
          ...

    这里把它转成 dict 列表，字段名保持原样，数值字段转 int（无法转换时保留原文）。
    """
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if not isinstance(data, str):
        return []

    header: list[str] = []
    rows: list[dict] = []
    for raw_line in data.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[") and "{" in line:
            inside = line[line.find("{") + 1: line.rfind("}")]
            header = [h.strip() for h in inside.split(",") if h.strip()]
            continue
        if not header:
            continue
        values = [v.strip() for v in line.split(",")]
        if len(values) != len(header):
            continue
        row: dict = {}
        for key, value in zip(header, values):
            if value in ("null", "", "-"):
                row[key] = None
                continue
            try:
                row[key] = int(value)
            except ValueError:
                try:
                    row[key] = float(value)
                except ValueError:
                    row[key] = value
        rows.append(row)
    return rows


__all__ = [
    "CODE_OK",
    "EMPTY_HINTS",
    "PayloadError",
    "extract_payload",
    "is_empty_result",
    "parse_coupon_markdown",
    "parse_nutrition_table",
    "require_ok",
]
