"""工具目录：按服务端 ``tools/list`` 的真实 schema 适配参数，避免把参数名写死。

官方文档给出的工具名是稳定的（33 个），但字段名会随版本演进（1.0.0 → 1.0.9）。
本模块的策略是：

* 启动时拉一次 ``tools/list``，缓存 ``inputSchema``；
* 调用前把「领域语义参数」按别名表映射到 schema 里真实存在的属性名；
* 缺失必填字段时直接抛出「该工具需要 X / Y」这类可执行的错误，而不是让服务端报 500。

同一套逻辑在离线回放（``--replay``）模式下同样生效，只是 schema 来自录制文件。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .client import McpClient, McpError, redact

# 领域参数 → 服务端字段候选别名（按顺序命中 schema 中存在的第一个）
ALIASES: Dict[str, Tuple[str, ...]] = {
    "city": ("city", "cityName", "city_name", "adName"),
    "keyword": ("keyword", "address", "addressKeyword", "q", "name"),
    "latitude": ("latitude", "lat"),
    "longitude": ("longitude", "lng", "lon"),
    "store_id": ("storeId", "storeCode", "shopId", "restaurantId", "code", "id"),
    "order_mode": ("orderMode", "diningMode", "takeType", "scene", "businessType", "channel"),
    "items": ("items", "goods", "cart", "productList", "foods", "skuList"),
    "coupon_id": ("couponId", "couponid"),
    "coupon_code": ("couponCode", "couponcode", "promoCode"),
    "keyword_search": ("keyword", "searchKey", "searchWord"),
    "page": ("page", "pageNum", "pageNo"),
    "page_size": ("pageSize", "size", "limit"),
}

REQUIRED_HINTS: Dict[str, List[str]] = {
    # 实测：query-nearby-stores 的 city 与 keyword 必须同传，schema 上看不出这个联动约束
    "query-nearby-stores": ["city", "keyword"],
    "query-meals": ["store_id"],
    "query-store-coupons": ["store_id"],
    "query-meal-detail": ["store_id", "items"],
    "calculate-price": ["store_id", "items"],
}


class SchemaMismatch(McpError):
    """本地即可判定的参数缺失/不匹配。"""


@dataclass
class ToolCatalog:
    client: Optional[McpClient]
    replay: Optional["ReplayCassette"] = None
    _tools: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    _loaded: bool = False

    # -------------------------------------------------------------- discovery
    def load(self) -> Dict[str, Dict[str, Any]]:
        if self._loaded:
            return self._tools
        raw: List[Dict[str, Any]]
        if self.replay:
            raw = self.replay.tools()
        else:
            assert self.client, "非回放模式必须提供 McpClient"
            raw = self.client.list_tools()
        self._tools = {t["name"]: t for t in raw if t.get("name")}
        self._loaded = True
        return self._tools

    def names(self) -> List[str]:
        return sorted(self.load())

    def schema(self, name: str) -> Dict[str, Any]:
        tool = self.load().get(name)
        if not tool:
            raise SchemaMismatch(
                f"服务端未提供工具 `{name}`；当前可用：{', '.join(self.names())}", code=None
            )
        return tool.get("inputSchema") or {}

    def has(self, name: str) -> bool:
        return name in self.load()

    # -------------------------------------------------------------- normalise
    def bind(self, name: str, semantic: Dict[str, Any]) -> Dict[str, Any]:
        """把语义参数翻译成服务端 schema 认识的参数。"""
        schema = self.schema(name)
        props: Dict[str, Any] = schema.get("properties") or {}
        required: List[str] = list(schema.get("required") or [])
        aliased: Dict[str, Any] = {}
        unmatched: List[str] = []

        for key, value in semantic.items():
            if value is None:
                continue
            if key in props:  # 已经是真实字段名
                aliased[key] = value
                continue
            candidates = ALIASES.get(key) or (key,)
            for cand in candidates:
                if cand in props:
                    aliased[cand] = value
                    break
            else:
                unmatched.append(key)

        for hint_key in REQUIRED_HINTS.get(name, []):
            real = next((c for c in ALIASES.get(hint_key, (hint_key,)) if c in props), None)
            if real and real not in aliased:
                raise SchemaMismatch(
                    f"`{name}` 需要同时提供 `{real}`（实测约束，缺一即返回空结果）。", code=None
                )

        missing = [r for r in required if r not in aliased]
        if missing:
            raise SchemaMismatch(
                f"`{name}` 缺少必填参数：{', '.join(missing)}；schema 可填字段："
                f"{', '.join(sorted(props)) or '(未声明)'}",
                code=None,
            )
        if unmatched:
            # 未识别的参数不静默丢弃：交给调用方判断是否需要换写法
            aliased.setdefault("_extra", {}).update({k: semantic[k] for k in unmatched})
        return aliased

    # ------------------------------------------------------------------ call
    def call(self, name: str, **semantic: Any) -> Dict[str, Any]:
        arguments = self.bind(name, semantic)
        arguments.pop("_extra", None)
        if self.replay:
            return self.replay.play(name, arguments)
        assert self.client
        return self.client.call_tool(name, arguments)

    def call_ok(self, name: str, **semantic: Any) -> Tuple[Any, Optional[str]]:
        """返回 ``(data, error)``，把"这路走不通"变成一等公民。"""
        try:
            res = self.call(name, **semantic)
        except (SchemaMismatch, McpError) as exc:
            return None, redact(str(exc))
        if res.get("ok"):
            return res.get("data"), None
        return None, redact(str(res.get("error") or "工具返回错误"))


# --------------------------------------------------------------------- 录制/回放
@dataclass
class ReplayCassette:
    """离线演示：用真实录制（或明确标注的示例）响应跑通全链路，无需 Token。

    录制文件格式::

        {
          "meta": {"recorded_at": "...", "source": "real|demo"},
          "tools": [ {"name": "...", "inputSchema": {...}} ],
          "tapes": [ {"tool": "...", "match": {...}, "response": {...}} ]
        }
    """

    path: str
    _data: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not os.path.exists(self.path):
            raise McpError(f"找不到录制文件：{self.path}")
        with open(self.path, encoding="utf-8") as fp:
            self._data = json.load(fp)

    @property
    def source(self) -> str:
        return (self._data.get("meta") or {}).get("source", "unknown")

    def tools(self) -> List[Dict[str, Any]]:
        return self._data.get("tools") or []

    def play(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        best: Optional[Dict[str, Any]] = None
        best_score = -1
        for tape in self._data.get("tapes", []):
            if tape.get("tool") != name:
                continue
            match = tape.get("match") or {}
            score = sum(1 for k, v in match.items() if arguments.get(k) == v)
            exact = all(arguments.get(k) == v for k, v in match.items())
            if exact and score > best_score:
                best, best_score = tape, score
        if best is None:
            raise McpError(f"录制中没有 `{name}` 的匹配响应（离线模式覆盖不全）。")
        resp = best["response"]
        if isinstance(resp, dict) and "ok" in resp:
            return resp
        return {"ok": True, "data": resp, "raw": resp, "error": None, "code": None}


def tape_key(tool: str, arguments: Dict[str, Any]) -> str:
    blob = json.dumps({"t": tool, "a": arguments}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def record(client: McpClient, tool: str, arguments: Dict[str, Any], out_path: str) -> Dict[str, Any]:
    """把一次真实调用追加进录制文件，供离线复现（自动脱敏）。"""
    res = client.call_tool(tool, arguments)
    payload = redact(json.dumps(res, ensure_ascii=False))
    data = json.loads(payload)
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as fp:
            cassette = json.load(fp)
    else:
        cassette = {"meta": {"source": "real"}, "tools": [], "tapes": []}
    if not cassette["tools"]:
        cassette["tools"] = client.list_tools()
    cassette["tapes"].append({"tool": tool, "match": arguments, "response": data,
                              "key": tape_key(tool, arguments)})
    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump(cassette, fp, ensure_ascii=False, indent=2)
    return data


def slugify(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", text).strip("-").lower()
