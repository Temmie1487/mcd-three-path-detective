"""三路比价引擎：把「同一份餐在三种取餐方式下的真实到手价」算出来。

调用编排（本项目的核心链路）::

    now-time-info            校准服务端时间 → 判断营业时段，避免算出"此刻点不了"的价
      └─ query-nearby-stores 到店自取 / 得来速的候选门店（city + keyword 必须同传）
      └─ delivery-query-addresses → delivery-query-stores   外送地址与可配送门店
           └─ query-meals            门店实时菜单 → 把口语点单解析成官方餐品编码
                └─ query-my-coupons / query-store-coupons   已持有券 + 本店可用券
                     └─ calculate-price × 渠道 × 券         官方逐路实算（金额单位：分）

设计原则：**诚实降级**。任何一路算不通（门店无得来速、券不可用 600012、菜单无此品）
都会带上原因写进结果，绝不用估算值冒充官方实算价。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .model import (
    CartItem,
    Channel,
    ChannelQuote,
    Coupon,
    Store,
    match_score,
    parse_items,
    to_cents,
    yuan,
)
from .tools import ToolCatalog

_ID_KEYS = ("storeId", "storeCode", "shopId", "restaurantId", "id", "code", "deptId")
_NAME_KEYS = ("storeName", "name", "shopName", "restaurantName", "title", "address")
_CODE_KEYS = ("mealCode", "skuCode", "productCode", "code", "itemId", "id", "foodCode")
_ITEM_NAME_KEYS = ("mealName", "productName", "skuName", "name", "title", "foodName")
_PRICE_KEYS = ("price", "salePrice", "originPrice", "unitPrice", "sellPrice", "finalPrice", "amount")
_DT_MARK = ("得来速", "drive", "thru", "drive-thru", "汽车餐厅", "MDS", "免下车")
_COUPON_ID_KEYS = ("couponId", "id", "promId", "activityId")
_COUPON_CODE_KEYS = ("couponCode", "code", "promoCode", "cardCode")
_COUPON_NAME_KEYS = ("couponName", "name", "title", "promotionName", "desc")


# ------------------------------------------------------------------ 结构挖掘
def iter_dicts(node: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from iter_dicts(v)
    elif isinstance(node, list):
        for v in node:
            yield from iter_dicts(v)


def unwrap(data: Any) -> Any:
    """剥掉 {"code":0,"data":...} / {"result":...} 这类信封。"""
    seen = 0
    while isinstance(data, dict) and seen < 6:
        if set(data) <= {"code", "msg", "message", "success"} and "data" not in data:
            break
        for key in ("data", "result", "results", "list"):
            if key in data and isinstance(data[key], (dict, list)):
                data = data[key]
                seen += 1
                break
        else:
            break
    return data


def pick(data: Any, id_keys: Sequence[str], name_keys: Sequence[str]) -> List[Dict[str, Any]]:
    """从任意嵌套结构里挑出"像实体"的 dict（有 id 且有 name）。"""
    out: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for d in iter_dicts(unwrap(data)):
        _id = next((d[k] for k in id_keys if d.get(k) not in (None, "")), None)
        _name = next((d[k] for k in name_keys if isinstance(d.get(k), str) and d.get(k)), None)
        if _id is None or _name is None:
            continue
        key = (str(_id), _name)
        if key in seen_ids:
            continue
        seen_ids.add(key)
        out.append(d)
    return out


def _get(d: Dict[str, Any], keys: Sequence[str]) -> Any:
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    low = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if low.get(k.lower()) not in (None, ""):
            return low[k.lower()]
    return None


# ------------------------------------------------------------------- 结果容器
@dataclass
class Trace:
    """记录每一次 MCP 调用，供 MCP_INTEGRATION.md 与调试使用（自动脱敏）。"""

    steps: List[Dict[str, Any]] = field(default_factory=list)

    def log(self, tool: str, args: Dict[str, Any], ok: bool, note: str = "") -> None:
        self.steps.append({
            "tool": tool,
            "args": {k: v for k, v in args.items() if k not in ("token", "authorization")},
            "ok": ok,
            "note": note[:200],
        })

    def dump(self) -> str:
        return json.dumps(self.steps, ensure_ascii=False, indent=2)


@dataclass
class Comparison:
    item_text: str
    items: List[CartItem]
    quotes: List[ChannelQuote]
    trace: Trace
    server_time: Optional[str] = None
    time_bucket: Optional[str] = None
    coupons_held: List[Coupon] = field(default_factory=list)
    coupons_claimable: int = 0
    warnings: List[str] = field(default_factory=list)

    @property
    def best(self) -> Optional[ChannelQuote]:
        avail = [q for q in self.quotes if q.available]
        return min(avail, key=lambda q: q.total_cents) if avail else None

    @property
    def worst(self) -> Optional[ChannelQuote]:
        avail = [q for q in self.quotes if q.available]
        return max(avail, key=lambda q: q.total_cents) if avail else None


# --------------------------------------------------------------------- 引擎
class Comparator:
    def __init__(self, catalog: ToolCatalog, money_unit: str = "auto",
                 max_coupon_probes: int = 6, verbose: bool = False):
        self.cat = catalog
        self.money_unit = money_unit
        self.max_coupon_probes = max_coupon_probes
        self.verbose = verbose
        self.trace = Trace()

    # ---------------------------------------------------------------- 时间
    def server_now(self) -> Tuple[Optional[str], Optional[str]]:
        if not self.cat.has("now-time-info"):
            return None, None
        data, err = self.cat.call_ok("now-time-info")
        self.trace.log("now-time-info", {}, data is not None, err or "")
        if not isinstance(data, (dict, str)):
            return None, None
        if isinstance(data, str):
            ts = data
        else:
            ts = str(_get(data, ("currentTime", "time", "datetime", "now", "timestamp")) or "")
        bucket = None
        try:
            hour = datetime.fromisoformat(ts.replace("Z", "+00:00")).hour
        except Exception:
            hour = datetime.now().hour
        bucket = ("早餐 05:30-10:00" if 5 <= hour < 10 else
                  "午餐 10:00-14:00" if 10 <= hour < 14 else
                  "下午茶 14:00-17:00" if 14 <= hour < 17 else
                  "晚餐 17:00-21:00" if 17 <= hour < 21 else "夜宵 21:00-05:30")
        return ts or None, bucket

    # ---------------------------------------------------------------- 门店
    def nearby_stores(self, city: str, keyword: str, limit: int = 8) -> List[Store]:
        if not self.cat.has("query-nearby-stores"):
            return []
        data, err = self.cat.call_ok("query-nearby-stores", city=city, keyword=keyword)
        self.trace.log("query-nearby-stores", {"city": city, "keyword": keyword},
                       data is not None, err or "")
        stores: List[Store] = []
        for d in pick(data, _ID_KEYS, _NAME_KEYS)[: limit * 3]:
            sid = str(_get(d, _ID_KEYS))
            name = str(_get(d, _NAME_KEYS))
            blob = json.dumps(d, ensure_ascii=False).lower()
            tags = [str(v) for v in (d.get("tags") or d.get("labels") or []) if isinstance(v, (str, int))]
            drive = any(mark in blob or mark in " ".join(tags).lower() for mark in _DT_MARK)
            stores.append(Store(store_id=sid, name=name, raw=d, drive_thru=drive,
                                distance_m=_distance_m(d), tags=tags))
        return stores[:limit]

    def delivery_stores(self, city: str, keyword: str, address: Optional[str],
                        limit: int = 5) -> Tuple[List[Store], List[Dict[str, Any]]]:
        addr_data, addr_err = (None, "无配送地址")
        addrs: List[Dict[str, Any]] = []
        if self.cat.has("delivery-query-addresses"):
            addr_data, addr_err = self.cat.call_ok("delivery-query-addresses")
            self.trace.log("delivery-query-addresses", {}, addr_data is not None, addr_err or "")
            addrs = pick(addr_data, _ID_KEYS, _NAME_KEYS)
            if address and addrs:
                addrs = [a for a in addrs if address in json.dumps(a, ensure_ascii=False)] or addrs
        if not self.cat.has("delivery-query-stores"):
            return [], addrs
        args: Dict[str, Any] = {"city": city, "keyword": keyword}
        if addrs:
            aid = _get(addrs[0], ("addressId", *_ID_KEYS))
            if aid:
                args["address"] = str(aid)
        data, err = self.cat.call_ok("delivery-query-stores", **args)
        self.trace.log("delivery-query-stores", args, data is not None, err or "")
        stores = []
        for d in pick(data, _ID_KEYS, _NAME_KEYS)[:limit]:
            stores.append(Store(store_id=str(_get(d, _ID_KEYS)), name=str(_get(d, _NAME_KEYS)), raw=d))
        return stores, addrs

    # ---------------------------------------------------------------- 菜单
    def menu(self, store_id: str) -> List[Dict[str, Any]]:
        if not self.cat.has("query-meals"):
            return []
        data, err = self.cat.call_ok("query-meals", store_id=store_id)
        self.trace.log("query-meals", {"store_id": store_id}, data is not None, err or "")
        rows = pick(data, _CODE_KEYS, _ITEM_NAME_KEYS)
        for r in rows:
            r["_price"] = first_price(r, self.money_unit)
        return rows

    def resolve(self, items: List[CartItem], menu_rows: List[Dict[str, Any]]) -> None:
        for it in items:
            best_row, best_s, pool = None, 0.0, []
            for row in menu_rows:
                name = str(_get(row, _ITEM_NAME_KEYS) or "")
                s = match_score(it.note, name)
                if name:
                    pool.append((s, name))
                if s > best_s and row.get("_price") is not None:
                    best_row, best_s = row, s
            if best_row and best_s >= 0.65:
                it.code = str(_get(best_row, _CODE_KEYS))
                it.resolved_name = str(_get(best_row, _ITEM_NAME_KEYS))
                it.price_cents = int(best_row["_price"])
                it.match_score = round(best_s, 3)
            else:
                pool.sort(key=lambda t: -t[0])
                it.candidates = [n for _s, n in pool[:3]]

    # ---------------------------------------------------------------- 券
    def coupons(self, store_id: str) -> Tuple[List[Coupon], List[Coupon], int]:
        held_raw, e1 = (None, None)
        if self.cat.has("query-my-coupons"):
            held_raw, e1 = self.cat.call_ok("query-my-coupons")
            self.trace.log("query-my-coupons", {}, held_raw is not None, e1 or "")
        store_raw, e2 = (None, None)
        if self.cat.has("query-store-coupons"):
            store_raw, e2 = self.cat.call_ok("query-store-coupons", store_id=store_id)
            self.trace.log("query-store-coupons", {"store_id": store_id},
                           store_raw is not None, e2 or "")
        held = to_coupons(held_raw, self.money_unit)
        store_coupons = to_coupons(store_raw, self.money_unit)
        claimable = 0
        if self.cat.has("available-coupons"):
            av, _e = self.cat.call_ok("available-coupons")
            claimable = len(pick(av, _COUPON_ID_KEYS, _COUPON_NAME_KEYS))
            self.trace.log("available-coupons", {}, av is not None, "")
        return held, store_coupons, claimable

    # ---------------------------------------------------------------- 算价
    def price(self, store: Store, channel: Channel, items: List[CartItem],
              coupon: Optional[Coupon]) -> Tuple[Optional[Dict[str, int]], Optional[str]]:
        """调用官方 calculate-price，返回 {goods,fee,discount,total}（分）或错误原因。"""
        if not self.cat.has("calculate-price"):
            return None, "服务端无 calculate-price 工具"
        goods = [{"code": it.code, "quantity": it.qty, "name": it.resolved_name}
                 for it in items if it.code]
        args: Dict[str, Any] = {"store_id": store.store_id, "order_mode": channel.order_mode,
                                "items": goods}
        if coupon and coupon.pair:
            args["coupon_id"], args["coupon_code"] = coupon.pair
        elif coupon:  # 券信息不全 → 主动跳过，避免必然的 600010
            return None, "券缺少 couponId/couponCode 配对，已跳过"
        data, err = self.cat.call_ok("calculate-price", **args)
        self.trace.log("calculate-price", args, data is not None, err or "")
        if data is None:
            reason = err or "算价失败"
            if "600012" in reason:
                reason = "该券在当前时段/门店/SKU 不可用(600012)"
            elif "600010" in reason:
                reason = "券参数未成对(600010)"
            elif "orderMode" in reason or "order_mode" in reason or "渠道" in reason:
                reason = f"服务端不识别 `{channel.order_mode}` 场景码"
            return None, reason
        if isinstance(data, str):
            return None, f"算价返回非结构化文本：{data[:60]}"
        amounts = extract_amounts(data, self.money_unit)
        if amounts.get("total") is None:
            return None, "算价响应中未找到应付总额字段"
        return amounts, None

    # ------------------------------------------------------------------ 主流程
    def compare(self, city: str, keyword: str, item_text: str,
                channels: Sequence[Channel] = Channel.all(),
                force_store: Optional[str] = None) -> Comparison:
        warnings: List[str] = []
        items = parse_items(item_text)
        ts, bucket = self.server_now()

        nearby = self.nearby_stores(city, keyword)
        deliv, addrs = self.delivery_stores(city, keyword, keyword)
        if force_store and nearby:
            nearby = [s for s in nearby if force_store in (s.store_id + s.name)] or nearby
        if not nearby and not deliv:
            warnings.append("三种渠道都没定位到门店：请检查城市名与地址关键词是否更精确。")

        quotes: List[ChannelQuote] = []
        menu_cache: Dict[str, List[Dict[str, Any]]] = {}
        coupons_cache: Dict[str, Tuple[List[Coupon], List[Coupon], int]] = {}
        held_all: List[Coupon] = []
        claimable_total = 0
        unresolved_reported = False

        for ch in channels:
            store: Optional[Store] = None
            if ch is Channel.DELIVERY:
                store = deliv[0] if deliv else None
                if not store:
                    quotes.append(ChannelQuote(
                        channel=ch, unavailable_reason="该地址无可配送门店或未配置配送地址",
                        items=items))
                    continue
            elif ch is Channel.DRIVE_THRU:
                store = next((s for s in nearby if s.drive_thru), None)
                if not store:
                    quotes.append(ChannelQuote(
                        channel=ch, unavailable_reason="附近门店未标记得来速服务能力",
                        items=items))
                    continue
            else:
                store = nearby[0] if nearby else None
                if not store:
                    quotes.append(ChannelQuote(channel=ch, unavailable_reason="未找到附近门店",
                                              items=items))
                    continue

            rows = menu_cache.get(store.store_id)
            if rows is None:
                rows = self.menu(store.store_id)
                menu_cache[store.store_id] = rows
            ch_items = [CartItem(raw=i.raw, qty=i.qty, note=i.note) for i in items]
            self.resolve(ch_items, rows)
            missing = [i for i in ch_items if not i.resolved]
            if missing and not unresolved_reported:
                detail = "；".join(
                    f"{i.raw}" + (f"（最接近：{'/'.join(i.candidates)}）" if i.candidates else "")
                    for i in missing
                )
                warnings.append("部分商品在该门店菜单中未匹配到，已跳过不计入该渠道：" + detail)
                unresolved_reported = True

            if store.store_id in coupons_cache:
                held, store_coupons, n_claim = coupons_cache[store.store_id]
            else:
                held, store_coupons, n_claim = self.coupons(store.store_id)
                coupons_cache[store.store_id] = (held, store_coupons, n_claim)
            held_all += held
            claimable_total = max(claimable_total, n_claim)

            payable_items = [i for i in ch_items if i.resolved]
            quote = ChannelQuote(channel=ch, store=store, items=ch_items)
            if not payable_items:
                quote.unavailable_reason = "该渠道菜单未匹配到任何商品"
                quotes.append(quote)
                continue

            # ① 无券基准价
            amounts, err = self.price(store, ch, payable_items, None)
            if amounts is None and err and "场景码" in err:
                amounts, err = self.price_with_fallback_modes(store, ch, payable_items)
            if amounts:
                quote.goods_cents = amounts.get("goods")
                quote.fee_cents = amounts.get("fee")
                quote.discount_cents = amounts.get("discount")
                quote.total_cents = amounts.get("total")
                quote.source = "official"
            else:
                quote.notes.append(f"官方算价未取到：{err}")
                quote.total_cents = local_estimate(store, payable_items, ch)
                quote.goods_cents = sum(i.subtotal_cents or 0 for i in payable_items)
                quote.source = "local" if quote.total_cents else "none"

            # ② 用券择优（含券必须 id/code 成对；不可用时保留无券价并记录原因）
            if quote.total_cents is not None and quote.source == "official":
                candidates = dedupe(held + store_coupons)[: self.max_coupon_probes]
                best_total, best_coupon, fails = quote.total_cents, None, []
                for cp in candidates:
                    if not cp.pair:
                        continue
                    a2, e2 = self.price(store, ch, payable_items, cp)
                    if a2 and a2.get("total") is not None and a2["total"] < best_total:
                        best_total, best_coupon = a2["total"], cp
                        quote.discount_cents = a2.get("discount")
                        quote.goods_cents = a2.get("goods", quote.goods_cents)
                        quote.fee_cents = a2.get("fee", quote.fee_cents)
                    elif e2:
                        fails.append(f"{cp.label}:{e2}")
                if best_coupon:
                    quote.total_cents = best_total
                    quote.coupon = best_coupon
                    quote.notes.append(f"挂券最优：{best_coupon.label}")
                elif candidates and fails:
                    quote.notes.append("已持有券在本渠道均不可用（示例原因：" + fails[0] + "）")

            quotes.append(quote)

        return Comparison(
            item_text=item_text,
            items=items,
            quotes=quotes,
            trace=self.trace,
            server_time=ts,
            time_bucket=bucket,
            coupons_held=dedupe(held_all),
            coupons_claimable=claimable_total,
            warnings=warnings,
        )

    def price_with_fallback_modes(self, store: Store, ch: Channel,
                                  items: List[CartItem]) -> Tuple[Optional[Dict[str, int]], Optional[str]]:
        """服务端场景码命名可能是中文/数字枚举，逐个尝试，绝不猜价格。"""
        last = None
        for mode in (ch.order_mode, ch.cn, ch.value, str(list(Channel).index(ch))):
            goods = [{"code": it.code, "quantity": it.qty} for it in items if it.code]
            args = {"store_id": store.store_id, "order_mode": mode, "items": goods}
            data, err = self.cat.call_ok("calculate-price", **args)
            self.trace.log("calculate-price", args, data is not None, err or "fallback-mode")
            if data is not None and not isinstance(data, str):
                amounts = extract_amounts(data, self.money_unit)
                if amounts.get("total") is not None:
                    return amounts, None
            last = err
        return None, last or "所有场景码均不被接受"


# ------------------------------------------------------------------ 辅助函数
def _distance_m(d: Dict[str, Any]) -> Optional[int]:
    """距离字段口径不一（米/公里/字符串），只做保守解析，解析不出就当未知。"""
    raw = _get(d, ("distance", "distanceMeters", "meters", "gap"))
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).strip()
    m = re.search(r"([\d.]+)\s*(km|公里|m|米)?", s, re.I)
    if not m:
        return None
    val = float(m.group(1))
    return int(val * 1000) if (m.group(2) or "").lower() in ("km", "公里") else int(val)


def first_price(row: Dict[str, Any], unit: str) -> Optional[int]:
    v = _get(row, _PRICE_KEYS)
    if isinstance(v, list):
        v = v[0] if v else None
    return to_cents(v, unit)


def to_coupons(data: Any, unit: str) -> List[Coupon]:
    out: List[Coupon] = []
    for d in pick(unwrap(data), _COUPON_ID_KEYS, _COUPON_NAME_KEYS):
        out.append(Coupon(
            coupon_id=(str(_get(d, _COUPON_ID_KEYS)) if _get(d, _COUPON_ID_KEYS) is not None else None),
            coupon_code=(str(_get(d, _COUPON_CODE_KEYS)) if _get(d, _COUPON_CODE_KEYS) is not None else None),
            name=str(_get(d, _COUPON_NAME_KEYS) or ""),
            raw=d,
        ))
    return out


def dedupe(coupons: List[Coupon]) -> List[Coupon]:
    seen, out = set(), []
    for c in coupons:
        k = (c.coupon_id, c.coupon_code)
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out


def extract_amounts(data: Any, unit: str) -> Dict[str, Optional[int]]:
    """从算价响应里定位 商品金额/配送费/优惠/应付总额。"""
    result: Dict[str, Optional[int]] = {"goods": None, "fee": None, "discount": None, "total": None}
    flat: Dict[str, int] = {}
    for d in iter_dicts(unwrap(data)):
        for k, v in d.items():
            c = to_cents(v, unit) if isinstance(v, (int, float, str)) else None
            if c is not None and re.search(r"(amount|price|fee|total|discount|money)", str(k), re.I):
                flat.setdefault(str(k), c)
    for key, target in (
        (("totalPay", "payAmount", "actualAmount", "totalAmount", "payPrice", "finalAmount"), "total"),
        (("deliveryFee", "freight", "shippingFee", "sendFee"), "fee"),
        (("discountAmount", "couponAmount", "promotionAmount", "discount"), "discount"),
        (("goodsAmount", "totalPrice", "originalAmount", "originAmount", "mealAmount"), "goods"),
    ):
        for k in key:
            if k in flat:
                result[target] = flat[k]
                break
    if result["total"] is None and "totalPrice" in flat:
        result["total"] = flat["totalPrice"]
    return result


def local_estimate(store: Store, items: List[CartItem], ch: Channel) -> Optional[int]:
    """仅作为"官方实算失败"时的参考基线：单品累加，不含券与配送费。"""
    total = sum(i.subtotal_cents or 0 for i in items if i.resolved)
    return total or None
