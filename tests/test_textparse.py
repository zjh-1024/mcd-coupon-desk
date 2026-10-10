"""返回文本解析的单元测试。

麦当劳 MCP 返回「说明文字 + 内嵌 JSON」，这些用例固定住解析行为，
防止上游改格式或本地正则化改写导致静默失效。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcd_coupon_desk.textparse import (  # noqa: E402
    PayloadError,
    extract_payload,
    is_empty_result,
    parse_nutrition_table,
    require_ok,
)

# 2026-10-10 实测抓取的 now-time-info 返回原文（已截断说明段）
NOW_TIME_SAMPLE = (
    "# API Response Information\n\nBelow is the response from an API call.\n\n"
    "## Response Structure\n\n- **data**: 服务器时间信息 (Type: object)\n"
    "  - **data.date**: 日期字符串 (Type: string)\n\n"
    "## Original Response\n\n"
    '{"success":true,"code":200,"message":"请求成功","datetime":"2026-10-10 23:15:33",'
    '"data":{"timestamp":1791645333896,"date":"2026-10-10","dayOfWeek":"SATURDAY"}}'
)

# 实测的「空结果」大白话回复
EMPTY_COUPON_SAMPLE = "# 您的优惠券列表\n\n暂无可用优惠券"

# 说明文字里含花括号，正则方案会误匹配；必须用括号配平
TRAP_SAMPLE = (
    "说明：返回格式为 {\"a\":1} 这样的结构，请勿混淆\n"
    '{"success":true,"code":200,"data":[{"storeCode":"1410419"}]}\n'
    "## 展示时请完整展示 storeCode"
)


class TestExtractPayload(unittest.TestCase):
    def test_extracts_embedded_json(self):
        payload = extract_payload(NOW_TIME_SAMPLE)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["data"]["date"], "2026-10-10")

    def test_ignores_braces_in_prose(self):
        payload = extract_payload(TRAP_SAMPLE)
        self.assertEqual(payload["data"][0]["storeCode"], "1410419")

    def test_raises_when_no_json(self):
        with self.assertRaises(PayloadError):
            extract_payload(EMPTY_COUPON_SAMPLE)

    def test_accepts_already_parsed(self):
        self.assertEqual(extract_payload({"success": True, "data": 1})["data"], 1)

    def test_wraps_bare_list(self):
        payload = extract_payload([{"code": "x"}])
        self.assertTrue(payload["success"])
        self.assertEqual(payload["data"], [{"code": "x"}])


class TestEmptyResult(unittest.TestCase):
    def test_prose_empty_is_detected(self):
        self.assertTrue(is_empty_result(EMPTY_COUPON_SAMPLE))

    def test_json_response_is_never_empty(self):
        self.assertFalse(is_empty_result(NOW_TIME_SAMPLE))

    def test_real_data_with_negative_word_is_not_empty(self):
        # 「没有」出现在真实数据里，但有 JSON，不能判成空
        text = '{"success":true,"data":[{"name":"订单中没有的商品"}]}'
        self.assertFalse(is_empty_result(text))


class TestRequireOk(unittest.TestCase):
    def test_returns_data_on_success(self):
        self.assertEqual(require_ok({"success": True, "data": [1]}, "t"), [1])

    def test_raises_with_server_message(self):
        payload = {"success": False, "code": 600058, "message": "城市名或者关键词不能为空"}
        with self.assertRaises(PayloadError) as ctx:
            require_ok(payload, "query-nearby-stores")
        self.assertIn("600058", str(ctx.exception))
        self.assertIn("城市名", str(ctx.exception))


class TestNutritionTable(unittest.TestCase):
    # 实测 list-nutrition-foods 的 data 是「表头 + 逗号分隔行」文本
    SAMPLE = (
        "[160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:\n"
        "  猪柳麦满分,null,1288,308,16,16,24,781,213\n"
        "  中薯条,null,1210,289,4,12,38,165,18\n"
    )

    def test_parses_rows(self):
        rows = parse_nutrition_table(self.SAMPLE)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["productName"], "猪柳麦满分")
        self.assertEqual(rows[0]["energyKcal"], 308)

    def test_null_becomes_none(self):
        rows = parse_nutrition_table(self.SAMPLE)
        self.assertIsNone(rows[0]["nutritionDescription"])

    def test_list_passthrough(self):
        data = [{"productName": "x"}]
        self.assertEqual(parse_nutrition_table(data), data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
