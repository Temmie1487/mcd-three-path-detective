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
    """三个取餐渠道。

    线上真实 schema 用 ``orderType`` + ``beType`` 显式区分渠道（不是靠门店标签猜）：

    * 到店自取：orderType=1, beType=1，**不传** beCode
    * 得来速　：orderType=1, beType=5，**必传** beCode（来自 query-nearby-stores）
    * 麦乐送　：orderType=2, beType=2，**必传** beCode（来自 delivery-query-stores）

    同一家门店在不同渠道的 beCode 不同（实测 1420856 → 得来速 142085641 / 外送 142085602）。
    """

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
    def order_type(self) -> int:
        return 2 if self is Channel.DELIVERY else 1

    @property
    def be_type(self) -> int:
        return {Channel.IN_STORE: 1, Channel.DELIVERY: 2, Channel.DRIVE_THRU: 5}[self]

    @property
    def needs_be_code(self) -> bool:
        return self is not Channel.IN_STORE

    @property
    def order_mode(self) -> str:
        """兼容旧写法（部分客户端文档用字符串场景码）。"""
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
    store_code: str
    name: str
    be_code: Optional[str] = None          # 渠道级业务编码，跨渠道不同
    raw: Dict[str, Any] = field(default_factory=dict)
    distance_m: Optional[int] = None
    open_hours: str = ""                   # 例 "07:00-23:00"
    business_open: Optional[bool] = None
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
    product_codes: List[str] = field(default_factory=list)  # 券自带商品白名单
    scope_labels: List[str] = field(default_factory=list)   # 例 ["到店"] / ["外送"]
    valid_until: str = ""

    @property
    def pair(self) -> Optional[Tuple[str, str]]:
        """实测：券挂在具体商品上，且 couponId 与 couponCode 必须成对，缺一即被拒。"""
        if self.coupon_id and self.coupon_code:
            return self.coupon_id, self.coupon_code
        return None

    @property
    def label(self) -> str:
        scope = f"[{'/'.join(self.scope_labels)}]" if self.scope_labels else ""
        return f"{self.name or '券'}{scope}"

    def fits(self, item: "CartItem") -> bool:
        """券的 products 白名单里是否含该商品在本渠道的编码。"""
        if not self.product_codes or item.code is None:
            return True
        return item.code in self.product_codes


# --------------------------------------------------------------------- 结果
@dataclass
class ChannelQuote:
    channel: Channel
    store: Optional[Store] = None
    items: List[CartItem] = field(default_factory=list)
    goods_cents: Optional[int] = None        # 优惠后商品价
    original_cents: Optional[int] = None     # 优惠前合计
    fee_cents: Optional[int] = None          # 配送费（实测外送 ¥6.00）
    packing_cents: Optional[int] = None      # 打包服务费（实测外送 ¥1.90）
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
    def extra_cost_cents(self) -> int:
        """渠道附加成本：配送费 + 打包服务费，这正是外送比到店贵的机制。"""
        return (self.fee_cents or 0) + (self.packing_cents or 0)

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
