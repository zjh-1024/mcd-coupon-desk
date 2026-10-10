"""零依赖的 MCP 客户端（Streamable HTTP + JSON-RPC 2.0）。

为什么不用官方 SDK
------------------
1. 评委和用户装完就能跑，不需要 `pip install` 任何东西；
2. 麦当劳 MCP 用的是标准协议，手写一遍反而更容易看清调用链；
3. 只有一个依赖「标准库」的项目，代码审计成本最低。

协议要点（对应 MCP 2025-06-18）
-------------------------------
- 所有请求 POST 到同一个 endpoint，body 是 JSON-RPC 2.0；
- 请求头必须同时声明 `application/json` 与 `text/event-stream` 两种 Accept；
- 服务端返回 `Mcp-Session-Id` 响应头，后续请求必须原样带回；
- 响应体可能是 JSON，也可能是 SSE（`data: {...}` 逐行），两种都要能解析。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_ENDPOINT = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "mcd-coupon-desk"
CLIENT_VERSION = "0.1.0"
TOKEN_ENV = "MCD_MCP_TOKEN"


class McpError(RuntimeError):
    """MCP 调用失败的统一异常。"""


class McpAuthError(McpError):
    """401：Token 无效、过期或未提供。"""


class McpRateLimitError(McpError):
    """429：超过 600 次/分钟的限流。"""


@dataclass
class ToolSpec:
    """一个 MCP 工具的描述（来自 tools/list）。"""

    name: str
    description: str = ""
    input_schema: dict = field(default_factory=dict)

    @property
    def required(self) -> list[str]:
        return list(self.input_schema.get("required", []) or [])

    @property
    def properties(self) -> dict:
        return self.input_schema.get("properties", {}) or {}


def _extract_payloads(body: str) -> list[dict]:
    """从响应体里取出所有 JSON-RPC 报文。

    服务端可能返回纯 JSON，也可能返回 SSE（多条 `data:` 行）。
    不做猜测，两种格式都尝试，取第一个带 `result` 或 `error` 的报文。
    """
    body = body.strip()
    if not body:
        return []

    payloads: list[dict] = []
    if body.startswith("{"):
        try:
            payloads.append(json.loads(body))
        except json.JSONDecodeError:
            pass

    if not payloads and ("data:" in body or "event:" in body):
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            chunk = line[len("data:"):].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                payloads.append(json.loads(chunk))
            except json.JSONDecodeError:
                continue

    return payloads


class McpClient:
    """麦当劳 MCP Server 的最小可用客户端。

    用法::

        client = McpClient()          # 从环境变量 MCD_MCP_TOKEN 读取 Token
        client.initialize()
        tools = client.list_tools()   # 运行时发现工具与入参 schema
        data = client.call_tool_json("query-meals", {"storeCode": "..."})
    """

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        token: str | None = None,
        timeout: int = 30,
        max_retries: int = 2,
    ) -> None:
        self.endpoint = endpoint
        self.token = (token if token is not None else os.environ.get(TOKEN_ENV, "")).strip()
        self.timeout = timeout
        self.max_retries = max_retries
        self.session_id: str | None = None
        self._next_id = 0
        self._tools: list[ToolSpec] | None = None

        if not self.token:
            raise McpAuthError(
                f"未找到 MCP Token。请先设置环境变量 {TOKEN_ENV}，"
                f"申请地址 https://open.mcd.cn/mcp"
            )

    # ------------------------------------------------------------ 传输层

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _post(self, payload: dict) -> list[dict]:
        """发送一条 JSON-RPC 报文，返回解析后的响应报文列表。"""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        request = urllib.request.Request(self.endpoint, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                sid = response.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                return _extract_payloads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            self._raise_http_error(exc)
            raise  # 理论上到不了这里，_raise_http_error 一定抛异常
        except urllib.error.URLError as exc:
            raise McpError(f"网络请求失败：{exc.reason}") from exc

    @staticmethod
    def _raise_http_error(exc: urllib.error.HTTPError) -> None:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001 - 读取响应体失败不应掩盖原始错误
            pass
        if exc.code == 401:
            raise McpAuthError(
                "401：MCP Token 无效、已过期或未提供。请检查环境变量 "
                f"{TOKEN_ENV} 是否填的是完整 Token。{detail}"
            )
        if exc.code == 429:
            raise McpRateLimitError(
                f"429：触发限流（每分钟上限 600 次）。请降低请求频率。{detail}"
            )
        raise McpError(f"HTTP {exc.code}：{detail or exc.reason}")

    @staticmethod
    def _unwrap(payloads: list[dict], rpc_id: int) -> Any:
        """从响应报文中取出指定 id 的 result，遇 error 抛异常。"""
        for msg in payloads:
            if msg.get("id") != rpc_id:
                continue
            if "error" in msg:
                err = msg["error"] or {}
                raise McpError(f"MCP 返回错误 {err.get('code')}：{err.get('message')}")
            return msg.get("result")
        raise McpError("响应中未找到匹配的 JSON-RPC 报文")

    # ------------------------------------------------------------ 协议层

    def initialize(self) -> dict:
        """握手。成功后服务端会下发 Mcp-Session-Id。"""
        rpc_id = self._new_id()
        payloads = self._post(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                },
            }
        )
        result = self._unwrap(payloads, rpc_id) or {}

        # 按协议要求补一条 initialized 通知（通知没有 id，不需要响应）
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return result

    def list_tools(self, refresh: bool = False) -> list[ToolSpec]:
        """发现服务端提供的全部工具。

        这一步是整套架构的关键：**入参 schema 不做硬编码，运行时问服务端要**。
        麦当劳官方未公开工具入参文档，动态发现是唯一可靠的做法。
        """
        if self._tools is not None and not refresh:
            return self._tools

        rpc_id = self._new_id()
        payloads = self._post(
            {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/list", "params": {}}
        )
        result = self._unwrap(payloads, rpc_id) or {}
        specs = [
            ToolSpec(
                name=t.get("name", ""),
                description=t.get("description", "") or "",
                input_schema=t.get("inputSchema", {}) or {},
            )
            for t in (result.get("tools") or [])
        ]
        self._tools = specs
        return specs

    def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        """调用一个工具，返回原始 result（含 content 数组）。"""
        rpc_id = self._new_id()
        payload = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                payloads = self._post(payload)
                return self._unwrap(payloads, rpc_id)
            except McpRateLimitError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    raise
                time.sleep(1.5 * (attempt + 1))   # 限流退避
            except McpError:
                raise
        raise last_error or McpError("调用失败")

    def call_tool_json(self, name: str, arguments: dict | None = None) -> Any:
        """调用工具并把返回的文本内容解析成 Python 对象。

        MCP 的 tools/call 统一返回 content 数组，文本型工具会把 JSON 放在
        text 字段里。这里做一层兼容：解析成功就返回对象，失败就返回原文本。
        """
        result = self.call_tool(name, arguments)
        if not isinstance(result, dict):
            return result
        if result.get("isError"):
            texts = [c.get("text", "") for c in result.get("content", []) if isinstance(c, dict)]
            raise McpError("工具 " + name + " 返回错误：" + " ".join(texts)[:500])

        chunks = [
            c.get("text", "")
            for c in result.get("content", [])
            if isinstance(c, dict) and c.get("type") == "text"
        ]
        text = "\n".join(chunks).strip()
        if not text:
            return result
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    # ------------------------------------------------------------ 便捷方法

    def describe_tools(self, keyword: str | None = None) -> str:
        """把工具清单渲染成可读文本，便于排错与写文档。"""
        lines: list[str] = []
        for spec in self.list_tools():
            if keyword and keyword not in spec.name:
                continue
            lines.append(f"- {spec.name}({', '.join(spec.required)}): {spec.description[:80]}")
        return "\n".join(lines) or "（无匹配工具）"


__all__ = [
    "CLIENT_NAME",
    "CLIENT_VERSION",
    "DEFAULT_ENDPOINT",
    "McpAuthError",
    "McpClient",
    "McpError",
    "McpRateLimitError",
    "PROTOCOL_VERSION",
    "TOKEN_ENV",
    "ToolSpec",
]
