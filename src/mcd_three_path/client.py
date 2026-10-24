"""麦当劳 MCP Server 的极简 Streamable HTTP 客户端（仅用标准库）。

设计要点（均为实测得出的工程约束）：

1. **无状态优先**：部分部署不返回 ``Mcp-Session-Id``，此时每次调用前都必须重新
   ``initialize``；若返回了 session id 则会话内复用。
2. **协议版本**：官方声明支持 ``2025-06-18`` 及更早版本，这里固定协商该版本。
3. **SSE 兼容**：Streamable HTTP 的响应 ``Content-Type`` 可能是
   ``application/json``，也可能是 ``text/event-stream``，两种都要能解析。
4. **限流保护**：单 Token 600 次/分钟。内置最小调用间隔与 429 指数退避。
5. **凭证隔离**：Token 只从环境变量读取，任何日志与异常信息都会做脱敏。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "mcd-three-path-detective"
CLIENT_VERSION = "1.0.0"

DEFAULT_URL = "https://mcp.mcd.cn"
ENV_TOKEN = "MCD_MCP_TOKEN"
ENV_URL = "MCD_MCP_URL"

_TOKEN_PATTERN = re.compile(r"(?i)(mcdt[a-z0-9\-_]{6,}|bearer\s+[a-z0-9\-._~+/]{6,})")


class McpError(RuntimeError):
    """MCP 调用失败。``code`` 保留服务端原始错误码，便于上层做容错回退。"""

    def __init__(self, message: str, code: Optional[int] = None, detail: Any = None):
        super().__init__(message)
        self.code = code
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - 仅用于人读
        base = super().__str__()
        return f"{base} (code={self.code})" if self.code is not None else base


def redact(text: str) -> str:
    """把日志/报错里可能出现的凭证打码。"""
    if not text:
        return text
    return _TOKEN_PATTERN.sub(lambda m: (m.group(0)[:4] + "****"), text)


def load_token(explicit: Optional[str] = None) -> str:
    """Token 解析顺序：显式参数 → 环境变量。不落盘、不写死。"""
    token = explicit or os.environ.get(ENV_TOKEN, "").strip()
    if not token:
        raise McpError(
            f"未找到 MCP Token：请设置环境变量 {ENV_TOKEN}，"
            "或先访问 https://open.mcd.cn/mcp 登录控制台激活申请。"
        )
    return token


def load_url(explicit: Optional[str] = None) -> str:
    return (explicit or os.environ.get(ENV_URL) or DEFAULT_URL).rstrip("/")


@dataclass
class McpClient:
    """一个足够"诚实"的 MCP 客户端：能列出工具、能调用工具、错误原样上抛。"""

    url: str = field(default_factory=lambda: load_url())
    token: str = field(default_factory=lambda: load_token())
    timeout: float = 30.0
    min_interval: float = 0.25
    max_retries: int = 3
    session_id: Optional[str] = None
    recorder: Optional[Any] = None
    _seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _last_call: float = 0.0

    # ------------------------------------------------------------------ 传输层
    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            # Streamable HTTP 允许两种返回形态，必须同时声明
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "User-Agent": f"{CLIENT_NAME}/{CLIENT_VERSION}",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _throttle(self) -> None:
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None,
             notification: bool = False) -> Optional[Dict[str, Any]]:
        if notification:
            payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        else:
            self._seq += 1
            payload = {"jsonrpc": "2.0", "id": self._seq, "method": method, "params": params or {}}

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_err: Optional[Exception] = None

        for attempt in range(self.max_retries):
            self._throttle()
            try:
                req = Request(self.url, data=body, headers=self._headers(), method="POST")
                with urlopen(req, timeout=self.timeout) as resp:
                    sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
                    if sid:
                        self.session_id = sid
                    raw = resp.read().decode("utf-8", "replace")
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                if notification:
                    return None
                return self._parse(payload["id"], raw, ctype)
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
                if exc.code == 429 and attempt < self.max_retries - 1:
                    time.sleep(min(2 ** attempt, 8))
                    last_err = exc
                    continue
                if exc.code == 401:
                    raise McpError("认证失败(401)：MCP Token 无效、已过期或未提供。", code=401) from exc
                raise McpError(f"HTTP {exc.code}: {redact(detail)[:400]}", code=exc.code) from exc
            except (URLError, TimeoutError) as exc:
                if attempt < self.max_retries - 1:
                    time.sleep(min(2 ** attempt, 8))
                    last_err = exc
                    continue
                raise McpError(f"网络不可达：{redact(str(exc))}") from exc

        raise McpError(f"请求最终失败：{redact(str(last_err))}")  # pragma: no cover

    @staticmethod
    def _parse(req_id: int, raw: str, ctype: str) -> Dict[str, Any]:
        """兼容 application/json 与 text/event-stream 两种响应。"""
        if not raw.strip():
            raise McpError("服务端返回空响应，请确认接入地址是否正确。")

        candidates: Iterable[str]
        if "text/event-stream" in ctype or raw.lstrip().startswith("event:"):
            candidates = (
                line[5:].lstrip() for line in raw.splitlines() if line.startswith("data:")
            )
        else:
            candidates = [raw]

        for chunk in candidates:
            chunk = chunk.strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                msg = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, list):
                msg = next((m for m in msg if m.get("id") == req_id), msg[0] if msg else None)
            if not isinstance(msg, dict):
                continue
            if "error" in msg:
                err = msg["error"]
                raise McpError(redact(str(err.get("message", err))), code=err.get("code"),
                               detail=err.get("data"))
            if "result" in msg:
                return msg["result"]
        raise McpError(f"响应中未找到 JSON-RPC result：{redact(raw)[:200]}")

    # ------------------------------------------------------------------ 生命周期
    def initialize(self, force: bool = False) -> Dict[str, Any]:
        if self.session_id and not force:
            return {"protocolVersion": PROTOCOL_VERSION, "resumed": True}
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        )
        try:
            self._rpc("notifications/initialized", {}, notification=True)
        except McpError:
            # 个别实现对通知不敏感，忽略即可，不影响后续 tools/call
            pass
        return result or {}

    def list_tools(self) -> List[Dict[str, Any]]:
        self.initialize()
        return (self._rpc("tools/list") or {}).get("tools", [])

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """调用工具，返回 ``{"ok": bool, "data": Any, "raw": Any, "error": str|None}``。

        结构化数据优先取 ``structuredContent``（MCP 2025-06 起支持），
        其次尝试把 ``content[0].text`` 当 JSON 解析，最后原样返回文本。
        """
        self.initialize()
        try:
            result = self._rpc("tools/call", {"name": name, "arguments": arguments}) or {}
        except McpError as exc:
            res = {"ok": False, "data": None, "raw": None, "error": str(exc), "code": exc.code}
            self._record(name, arguments, res)
            return res

        if result.get("isError"):
            res = {
                "ok": False,
                "data": None,
                "raw": result,
                "error": redact(_flatten_content(result)),
                "code": None,
            }
            self._record(name, arguments, res)
            return res
        res = {"ok": True, "data": _coerce(result), "raw": result, "error": None, "code": None}
        self._record(name, arguments, res)
        return res

    def _record(self, name: str, arguments: Dict[str, Any], res: Dict[str, Any]) -> None:
        """把一次调用写进录制器（``--record``），自动脱敏，便于离线复现与提 Issue。"""
        if self.recorder is None:
            return
        try:
            safe = json.loads(redact(json.dumps(res, ensure_ascii=False)))
            self.recorder(name, arguments, safe)
        except Exception:  # 录制失败绝不影响主流程
            pass


def _flatten_content(result: Dict[str, Any]) -> str:
    parts: List[str] = []
    for item in result.get("content", []) or []:
        if item.get("type") == "text":
            parts.append(item.get("text", ""))
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    return " | ".join(p for p in parts if p)


def _coerce(result: Dict[str, Any]) -> Any:
    if "structuredContent" in result and result["structuredContent"] is not None:
        sc = result["structuredContent"]
        if isinstance(sc, dict) and set(sc.keys()) <= {"result"}:
            return sc["result"]
        return sc
    text = _flatten_content(result)
    if not text:
        return result
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # 服务端有时把业务错误包在文本里：{"code":600012,...}
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return text
    if isinstance(parsed, dict) and parsed.get("code") not in (None, 0, 200) and "data" in parsed:
        # 形如 {"code":600012,"message":"当前优惠券不可用"} 的业务信封
        if parsed.get("data") is None:
            return parsed
    return parsed
