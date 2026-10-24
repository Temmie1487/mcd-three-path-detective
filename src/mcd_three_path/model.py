"""金额、购物车、渠道等值对象。所有金额内部一律用「分」表示。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple


# --------------------------------------------------------------------- 金额
def to_cents(value: Any, unit: str = "auto") -> Optional[int]:
    """把服务端返回的金额统一成分。

    ``auto`` 规则（官方约定"算价单位为分"）：

    * ``int``  → 视为分（例：``11450`` == ¥114.50）
    * ``float``/``str`` → 视为元（例：``"114.5"`` == ¥114.50）

    若某工具返回了整数元（如 ``114``），请用 ``--money-unit yuan`` 强制口径，
    本项目不做"看起来太小所以一定是元"这种猜测。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and unit == "cents":
        return int(round(float(value)))
    if isinstance(value, (int, float)) and unit == "yuan":
        return int(round(float(value) * 100))
    if isinstance(value, str):
        s = re.sub(r"[^\d.\-]", "", value)
        if not s:
            return None
        return int(round(float(s) * 100))
    if isinstance(value, int):
        return int(value)
    return int(round(float(value) * 100))


def yuan(cents: Optional[int]) -> str:
    if cents is None:
        return "—"
    return f"¥{cents / 100:,.2f}"


# --------------------------------------------------------------------- 渠道
class Channel(str, Enum):
    IN_STORE = "in_store"
    DELIVERY = "delivery"
    DRIVE_THRU = "drive_thru"

    @property
    def cn(self) -> str:
        return {
            Channel.IN_STORE: "到店自取",
            Channel.DELIVERY: "麦乐送",
            Channel.DRIVE_THRU: "得来速",
        }[self]

    @property
    def order_mode(self) -> str:
        # 点餐场景码；服务端不认识时会在 compare 中自动降级并记录原因
        return {
            Channel.IN_STORE: "pickup",
            Channel.DELIVERY: "delivery",
            Channel.DRIVE_THRU: "driveThru",
        }[self]

    @classmethod
    def all(cls) -> List["Channel"]:
        return [cls.IN_STORE, cls.DELIVERY, cls.DRIVE_THRU]


# ------------------------------------------------------------------ 餐品与购物车
@dataclass
class CartItem:
    """用户口头点的一件商品，例如「中薯条 x2」「巨无霸」。"""

    raw: str
    qty: int = 1
    note: str = ""
    code: Optional[str] = None          # 服务端餐品编码
    resolved_name: Optional[str] = None  # 匹配到的官方名称
    price_cents: Optional[int] = None    # 单点原价（分）
    match_score: float = 0.0
    candidates: List[str] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.code is not None

    @property
    def subtotal_cents(self) -> Optional[int]:
        if self.price_cents is None:
            return None
        return self.price_cents * self.qty


def parse_items(text: str) -> List[CartItem]:
    """解析「巨无霸 + 麦辣鸡翅2块 + 2份中薯条 + 可乐(中)」这类自然语言点单。"""
    parts = re.split(r"[,，、+＋/]+|\s{2,}", text.strip())
    items: List[CartItem] = []
    for part in parts:
        p = part.strip()
        if not p:
            continue
        qty = 1
        m = re.search(r"(?:^|\s)(\d+)\s*[份个杯条块]?\s*[x×*]\s*(?P<name>.+)$", p)
        if m:
            qty, p = int(m.group(1)), m.group("name").strip()
        else:
            m = re.search(r"^(?P<name>.*?)\s*[x×*]\s*(?P<n>\d+)$", p)
            if m:
                qty, p = int(m.group("n")), m.group("name").strip()
            else:
                m = re.search(r"^(?P<n>\d+)\s*[份个杯]?(?P<name>.+)$", p)
                if m and m.group("n"):
                    qty, p = int(m.group("n")), m.group("name").strip()
        items.append(CartItem(raw=part.strip(), qty=qty, note=p))
    return items


_SIZE_NOISE = ("大杯", "中杯", "小杯", "超大杯", "标准")


def normalize(name: str) -> str:
    s = re.sub(r"[（(].*?[)）]", "", str(name))
    for noise in _SIZE_NOISE:
        s = s.replace(noise, "")
    s = re.sub(r"[\s\d元份个杯块条]+", "", s)
    return s.lower()


def match_score(wanted: str, candidate: str) -> float:
    a, b = normalize(wanted), normalize(candidate)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.7 + 0.2 * (min(len(a), len(b)) / max(len(a), len(b)))
    common = sum(1 for ch in set(a) if ch in b)
    return 0.6 * common / max(len(set(a)), 1)


# --------------------------------------------------------------------- 门店/券
@dataclass
class Store:
    store_id: str
    name: str
    raw: Dict[str, Any] = field(default_factory=dict)
    drive_thru: bool = False
    distance_m: Optional[int] = None
    tags: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        d = f"（约{self.distance_m / 1000:.1f}km）" if self.distance_m else ""
        return f"{self.name}{d}"


@dataclass
class Coupon:
    coupon_id: Optional[str]
    coupon_code: Optional[str]
    name: str
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def pair(self) -> Optional[Tuple[str, str]]:
        """实测：算价挂券必须 couponId 与 couponCode 成对，缺一报 600010。"""
        if self.coupon_id and self.coupon_code:
            return self.coupon_id, self.coupon_code
        return None

    @property
    def label(self) -> str:
        return self.name or f"券{self.coupon_id or self.coupon_code or '?'}"


# --------------------------------------------------------------------- 结果
@dataclass
class ChannelQuote:
    channel: Channel
    store: Optional[Store] = None
    items: List[CartItem] = field(default_factory=list)
    goods_cents: Optional[int] = None
    fee_cents: Optional[int] = None
    discount_cents: Optional[int] = None
    total_cents: Optional[int] = None
    coupon: Optional[Coupon] = None
    source: str = "official"       # official=官方算价, local=本地推算, none=未取到
    unavailable_reason: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.total_cents is not None and self.unavailable_reason is None

    @property
    def badge(self) -> str:
        if self.unavailable_reason:
            return "不可用"
        return {"official": "官方实算", "local": "本地推算"}.get(self.source, "—")


def flatten_numbers(obj: Any, key_pattern: str = r"(amount|price|fee|total|discount|delivery)") -> List[int]:
    """在服务端字段名不完全稳定时，把疑似金额挖出来（仅在无法精确定位时兜底）。"""
    out: List[int] = []
    rx = re.compile(key_pattern, re.I)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, (int, float, str)) and rx.search(str(k)):
                    c = to_cents(v)
                    if c is not None:
                        out.append(c)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(obj)
    return out


def first_key(d: Dict[str, Any], *names: str) -> Any:
    for n in names:
        if n in d and d[n] not in (None, ""):
            return d[n]
    low = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        if n.lower() in low and low[n.lower()] not in (None, ""):
            return low[n.lower()]
    return None
