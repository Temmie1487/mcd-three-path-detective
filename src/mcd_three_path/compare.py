"""三路比价引擎（按麦当劳 MCP 线上真实 schema 实现）。

真实链路（全部为本项目实际调用）::

    now-time-info                                        服务端时间 → 餐段判定
    query-nearby-stores(beType=1, searchType=2)          到店自取门店
    query-nearby-stores(beType=5, searchType=2)          得来速门店（带 beCode）
    delivery-query-addresses → addressId
    delivery-query-stores(beType=2, addressId)           麦乐送门店（带 beCode）
      └─ query-meals(storeCode, beCode?, orderType, beType)   各渠道**各自**的菜单与售价
           └─ query-my-coupons / query-store-coupons          券池（券自带 products 白名单）
                └─ calculate-price(items[].couponId/couponCode) 逐渠道逐券官方实算

三条实测结论决定了这个引擎的形态：

1. **渠道靠 orderType/beType 显式区分**，同一门店在不同渠道 beCode 不同；
2. **同一件商品在不同渠道的 productCode 与售价都不同**（到店"巨无霸"=1100 ¥26.5，
   外送"巨无霸"=901100 ¥29），所以跨渠道只能**按商品名对齐**，不能复用编码；
3. **券挂在具体商品上**且带 `products` 白名单，因此"这张券能不能用"是渠道相关的真实问题。

设计原则：**诚实降级**。任何一路算不通都会带上真实原因（含服务端错误码）写进结果，
绝不用其他渠道的价格或本地估算冒充官方实算价。
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
    normalize,
    parse_items,
    to_cents,
)
from .tools import ToolCatalog

# ---- 真实字段名（来自线上 tools/list 的 inputSchema 与实测响应）-------------------
STORE_ID_KEYS = ("storeCode", "storeId", "shopId")
STORE_NAME_KEYS = ("storeName", "name", "address")
MEAL_CODE_KEYS = ("productCode", "code", "mealCode", "skuCode")
MEAL_NAME_KEYS = ("name", "mealName", "productName", "title")
COUPON_ID_KEYS = ("couponId", "id", "promId")
COUPON_CODE_KEYS = ("couponCode", "code", "promoCode")
COUPON_NAME_KEYS = ("title", "couponName", "name", "promotionName")


# ------------------------------------------------------------------ 结构工具
def iter_dicts(node: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from iter_dicts(v)
    elif isinstance(node, list):
        for v in node:
            yield from iter_dicts(v)


def envelope(data: Any) -> Tuple[Any, Optional[str]]:
    """麦当劳 MCP 把业务结果包在 {success, code, message, data} 信封里。

    关键：HTTP 层成功但 ``success=false`` 的情况非常多（508 / 60006 / 600012），
    必须在这里判定，否则会把报错当成"这一路很便宜"。
    """
    if isinstance(data, dict) and "success" in data:
        if data.get("success") is False:
            code = data.get("code")
            msg = str(data.get("message") or "业务失败")
            return None, f"{msg}({code})" if code is not None else msg
        return data.get("data"), None
    return data, None


def dig_list(node: Any, *hint_keys: str) -> List[Dict[str, Any]]:
    """在信封里找出实体列表：优先按提示键定位，找不到再全结构扫描去重。"""
    if isinstance(node, list):          # query-nearby-stores / query-store-coupons 直接返回数组
        return [d for d in node if isinstance(d, dict)]
    for key in hint_keys:
        cur = node
        for part in key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        if isinstance(cur, list):
            return [d for d in cur if isinstance(d, dict)]
        if isinstance(cur, dict):  # meals 是 {code: {...}} 字典
            out = []
            for k, v in cur.items():
                if isinstance(v, dict):
                    out.append({**v, **{MEAL_CODE_KEYS[0]: k}})
            if out:
                return out
    seen, out = set(), []
    for d in iter_dicts(node):
        if any(k in d for k in hint_keys):
            sig = json.dumps(d, sort_keys=True, ensure_ascii=False)[:160]
            if sig in seen:
                continue
            seen.add(sig)
            out.append(d)
    return out


def pick(d: Dict[str, Any], keys: Sequence[str]) -> Any:
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
    steps: List[Dict[str, Any]] = field(default_factory=list)

    def log(self, tool: str, args: Dict[str, Any], ok: bool, note: str = "") -> None:
        self.steps.append({
            "tool": tool,
            "args": {k: v for k, v in args.items() if k not in ("token", "authorization")},
            "ok": bool(ok),
            "note": (note or "")[:200],
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
    def available(self) -> List[ChannelQuote]:
        return [q for q in self.quotes if q.available]

    @property
    def best(self) -> Optional[ChannelQuote]:
        return min(self.available, key=lambda q: q.total_cents) if self.available else None

    @property
    def worst(self) -> Optional[ChannelQuote]:
        return max(self.available, key=lambda q: q.total_cents) if self.available else None


# --------------------------------------------------------------------- 引擎
class Comparator:
    def __init__(self, catalog: ToolCatalog, max_coupon_probes: int = 8, verbose: bool = False):
        self.cat = catalog
        self.max_coupon_probes = max_coupon_probes
        self.verbose = verbose
        self.trace = Trace()

    # ---------------------------------------------------------------- 时间
    def server_now(self) -> Tuple[Optional[str], Optional[str]]:
        if not self.cat.has("now-time-info"):
            return None, None
        raw, err = self.cat.call_ok("now-time-info")
        self.trace.log("now-time-info", {}, raw is not None, err or "")
        body, err2 = envelope(raw)
        ts = None
        if isinstance(body, dict):
            ts = str(pick(body, ("currentTime", "datetime", "time", "now", "timestamp")) or "")
        elif isinstance(body, str):
            ts = body
        if not ts:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        m = re.search(r"(\d{1,2}):\d{2}", ts)
        hour = int(m.group(1)) if m else datetime.now().hour
        bucket = ("早餐 05:30-10:00" if 5 <= hour < 10 else
                  "午餐 10:00-14:00" if 10 <= hour < 14 else
                  "下午茶 14:00-17:00" if 14 <= hour < 17 else
                  "晚餐 17:00-21:00" if 17 <= hour < 21 else "夜宵 21:00-05:30")
        return ts, bucket

    # -------------------------------------------------------------- 门店发现
    def store_by_nearby(self, city: str, keyword: str, ch: Channel) -> Tuple[Optional[Store], Optional[str]]:
        if not self.cat.has("query-nearby-stores"):
            return None, "服务端无 query-nearby-stores"
        args = {"beType": ch.be_type, "searchType": 2}
        if city:
            args["city"] = city
        if keyword:
            args["keyword"] = keyword
        raw, err = self.cat.call_ok("query-nearby-stores", **args)
        body, berr = envelope(raw)
        self.trace.log("query-nearby-stores", args, berr is None and raw is not None, err or berr or "")
        if berr:
            return None, f"{ch.cn}门店查询失败：{berr}"
        rows = dig_list(body, "data", "stores", "list")
        rows = [r for r in rows if pick(r, STORE_ID_KEYS)]
        if not rows:
            return None, (f"{ch.cn}：该位置无可用门店（服务端返回空列表）"
                          if ch is Channel.DRIVE_THRU else f"{ch.cn}：未找到附近门店")
        return self._to_store(rows[0], ch), None

    def store_by_delivery(self, address_hint: str) -> Tuple[Optional[Store], Optional[str]]:
        if not self.cat.has("delivery-query-addresses"):
            return None, "服务端无 delivery-query-addresses"
        raw, err = self.cat.call_ok("delivery-query-addresses")
        self.trace.log("delivery-query-addresses", {}, raw is not None, err or "")
        body, berr = envelope(raw)
        if berr:
            return None, f"配送地址查询失败：{berr}"
        addrs = dig_list(body, "addresses", "list", "data")
        if not addrs:
            return None, "账号下没有可配送地址（麦乐送这一路无法计算）"
        chosen = addrs[0]
        if address_hint:
            hit = [a for a in addrs if address_hint in json.dumps(a, ensure_ascii=False)]
            chosen = hit[0] if hit else addrs[0]
        aid = str(pick(chosen, ("addressId", "id")) or "")
        if not self.cat.has("delivery-query-stores"):
            return None, "服务端无 delivery-query-stores"
        args = {"beType": 2, "addressId": aid}
        raw2, err2 = self.cat.call_ok("delivery-query-stores", **args)
        body2, berr2 = envelope(raw2)
        self.trace.log("delivery-query-stores", args, berr2 is None and raw2 is not None,
                       err2 or berr2 or "")
        if berr2:
            return None, f"可配送门店查询失败：{berr2}"
        rows = [r for r in dig_list(body2, "data", "stores") if pick(r, STORE_ID_KEYS)]
        if not rows:
            return None, "该地址当前无可配送门店"
        return self._to_store(rows[0], Channel.DELIVERY), None

    def _to_store(self, d: Dict[str, Any], ch: Channel) -> Store:
        hours = ""
        s, e = d.get("businessStartTime"), d.get("businessEndTime")
        if s and e:
            hours = f"{s}-{e}"
        return Store(
            store_code=str(pick(d, STORE_ID_KEYS)),
            name=str(pick(d, STORE_NAME_KEYS) or ""),
            be_code=(str(d["beCode"]) if d.get("beCode") else None),
            raw=d,
            distance_m=_distance_m(d),
            open_hours=hours,
            business_open=(d.get("businessStatus") if isinstance(d.get("businessStatus"), bool) else None),
            tags=[str(t) for t in (d.get("tags") or []) if isinstance(t, (str, int))],
        )

    # ---------------------------------------------------------------- 菜单
    def menu(self, store: Store, ch: Channel) -> List[Dict[str, Any]]:
        if not self.cat.has("query-meals"):
            return []
        args: Dict[str, Any] = {"storeCode": store.store_code,
                                "orderType": ch.order_type, "beType": ch.be_type}
        if ch.needs_be_code:
            if store.be_code:
                args["beCode"] = store.be_code
            else:
                return []
        elif store.be_code and ch is Channel.DELIVERY:
            args["beCode"] = store.be_code
        raw, err = self.cat.call_ok("query-meals", **args)
        body, berr = envelope(raw)
        self.trace.log("query-meals", args, berr is None and raw is not None, err or berr or "")
        if berr:
            return []
        rows = dig_list(body, "meals", "data.meals")
        out = []
        for r in rows:
            code = pick(r, MEAL_CODE_KEYS)
            name = pick(r, MEAL_NAME_KEYS)
            price = to_cents(r.get("currentPrice"), "yuan")   # 菜单价是「元」字符串
            if price is None:
                price = to_cents(r.get("originalPrice"), "yuan")
            if code and name:
                out.append({"code": str(code), "name": str(name), "price": price,
                            "tags": r.get("tags") or []})
        return out

    def resolve(self, items: List[CartItem], rows: List[Dict[str, Any]]) -> None:
        for it in items:
            best, best_s, pool = None, 0.0, []
            for r in rows:
                if r["price"] is None:
                    continue
                s = match_score(it.note, r["name"])
                pool.append((s, r["name"]))
                if s > best_s:
                    best, best_s = r, s
            if best and best_s >= 0.6:
                it.code, it.resolved_name = best["code"], best["name"]
                it.price_cents = best["price"]
                it.match_score = round(best_s, 3)
            else:
                pool.sort(key=lambda t: -t[0])
                it.candidates = [n for _s, n in pool[:3]]

    # ----------------------------------------------------------------- 券
    def coupons(self, store: Store, ch: Channel) -> Tuple[List[Coupon], List[Coupon], int]:
        held: List[Coupon] = []
        if self.cat.has("query-my-coupons"):
            raw, err = self.cat.call_ok("query-my-coupons")
            self.trace.log("query-my-coupons", {}, raw is not None, err or "")
            body, _b = envelope(raw)
            held = to_coupons(dig_list(body, "coupons", "list", "data"))
        store_coupons: List[Coupon] = []
        if self.cat.has("query-store-coupons"):
            args: Dict[str, Any] = {"storeCode": store.store_code,
                                    "orderType": ch.order_type, "beType": ch.be_type}
            if ch.needs_be_code and store.be_code:
                args["beCode"] = store.be_code
            raw, err = self.cat.call_ok("query-store-coupons", **args)
            self.trace.log("query-store-coupons", args, raw is not None, err or "")
            body, _b = envelope(raw)
            store_coupons = to_coupons(dig_list(body, "coupons", "list", "data"))
        claimable = 0
        if self.cat.has("available-coupons"):
            raw, _e = self.cat.call_ok("available-coupons")
            self.trace.log("available-coupons", {}, raw is not None, "")
            body, _b = envelope(raw)
            claimable = len(dig_list(body, "coupons", "list", "data"))
        return held, store_coupons, claimable

    # -------------------------------------------------------------- 官方算价
    def price(self, store: Store, ch: Channel, items: List[CartItem],
              coupon: Optional[Coupon]) -> Tuple[Optional[Dict[str, Optional[int]]], Optional[str]]:
        if not self.cat.has("calculate-price"):
            return None, "服务端无 calculate-price"
        payload_items = []
        attached = False
        for it in items:
            row: Dict[str, Any] = {"productCode": it.code, "quantity": it.qty}
            # 券挂到白名单命中的第一件商品上：服务端按商品维度核销，全挂反而会被拒
            if coupon and coupon.pair and not attached and coupon.fits(it):
                row["couponId"], row["couponCode"] = coupon.pair
                attached = True
            payload_items.append(row)
        args: Dict[str, Any] = {"storeCode": store.store_code, "orderType": ch.order_type,
                                "beType": ch.be_type, "items": payload_items}
        if ch.needs_be_code and store.be_code:
            args["beCode"] = store.be_code
        raw, err = self.cat.call_ok("calculate-price", **args)
        body, berr = envelope(raw)
        self.trace.log("calculate-price", args, berr is None and raw is not None, err or berr or "")
        if berr:
            return None, berr
        if not isinstance(body, dict):
            return None, "算价响应结构异常"
        return extract_amounts(body), None

    # ---------------------------------------------------------------- 主流程
    def compare(self, city: str, keyword: str, item_text: str,
                channels: Sequence[Channel] = Channel.all(),
                force_store: Optional[str] = None) -> Comparison:
        warnings: List[str] = []
        items = parse_items(item_text)
        ts, bucket = self.server_now()

        # ① 三渠道各自的门店
        resolved: Dict[Channel, Tuple[Optional[Store], Optional[str]]] = {}
        for ch in channels:
            if ch is Channel.DELIVERY:
                st, why = self.store_by_delivery(keyword)
            else:
                st, why = self.store_by_nearby(city, keyword, ch)
            if st and force_store and force_store not in (st.store_code + st.name):
                st2, why2 = None, f"按 --store={force_store} 过滤后无匹配门店"
                resolved[ch] = (None, why2)
            else:
                resolved[ch] = (st, why)

        quotes: List[ChannelQuote] = []
        menu_cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        coupon_cache: Dict[str, Tuple[List[Coupon], List[Coupon], int]] = {}
        held_all: List[Coupon] = []
        claimable_total = 0

        for ch in channels:
            store, why = resolved[ch]
            if not store:
                quotes.append(ChannelQuote(channel=ch, items=items, unavailable_reason=why))
                continue

            mkey = (store.store_code, ch.name)
            if mkey not in menu_cache:
                menu_cache[mkey] = self.menu(store, ch)
            rows = menu_cache[mkey]
            if not rows:
                hours = f"，营业时间 {store.open_hours}" if store.open_hours else ""
                quotes.append(ChannelQuote(channel=ch, store=store, items=items,
                                           unavailable_reason=(
                        f"该渠道菜单为空：当前时刻不在 {ch.cn} 可点时段内{hours}")))
                continue

            ch_items = [CartItem(raw=i.raw, qty=i.qty, note=i.note) for i in items]
            self.resolve(ch_items, rows)
            payable = [i for i in ch_items if i.resolved]
            missing = [i for i in ch_items if not i.resolved]
            if missing:
                detail = "；".join(f"{i.raw}" + (f"（最接近：{'/'.join(i.candidates)}）"
                                                 if i.candidates else "") for i in missing)
                warnings.append(f"{ch.cn}渠道菜单未匹配到：{detail}（该商品未计入本渠道）")
            if not payable:
                quotes.append(ChannelQuote(channel=ch, store=store, items=ch_items,
                                           unavailable_reason="本渠道菜单未匹配到任何商品"))
                continue

            # ② 无券基准价（官方实算）
            quote = ChannelQuote(channel=ch, store=store, items=ch_items)
            amounts, err = self.price(store, ch, payable, None)
            if amounts and amounts.get("total") is not None:
                apply_amounts(quote, amounts)
            else:
                quote.unavailable_reason = f"官方算价失败：{err or '无总额字段'}"
                quote.notes.append("本渠道无任何价格结论，不做估算替代")
                quotes.append(quote)
                continue

            if store.business_open is False:
                quote.notes.append(f"门店当前休息（营业时间 {store.open_hours}）")

            # ③ 券候选池：券自带 products 白名单，只试真正命中的券
            if ch.name not in coupon_cache:
                coupon_cache[ch.name] = self.coupons(store, ch)
            held, store_coupons, n_claim = coupon_cache[ch.name]
            held_all += held
            claimable_total = max(claimable_total, n_claim)

            pool = dedupe(held + store_coupons)
            usable = [cp for cp in pool if cp.pair and any(cp.fits(i) for i in payable)]
            skipped = len([cp for cp in pool if cp.pair]) - len(usable)
            if skipped > 0:
                quote.notes.append(
                    f"{skipped} 张券的商品白名单不含本次点单在本渠道的编码，已跳过"
                    "（跨渠道商品码不同，这正是同券不同命的原因）")
            best_total, best_cp, fails = quote.total_cents, None, []
            for cp in usable[: self.max_coupon_probes]:
                a2, e2 = self.price(store, ch, payable, cp)
                if a2 and a2.get("total") is not None and a2["total"] < best_total:
                    best_total, best_cp, best_amounts = a2["total"], cp, a2
                elif e2:
                    fails.append(f"「{cp.name}」{e2}")
            if best_cp:
                apply_amounts(quote, best_amounts)
                quote.coupon = best_cp
                quote.notes.append(f"挂券最优：{best_cp.label}")
            elif fails:
                quote.notes.append("已试挂券均不降价或不可用：" + "；".join(fails[:2]))

            quotes.append(quote)

        if all(q.unavailable_reason for q in quotes):
            warnings.append("三条渠道均未得到官方实算价格，请检查城市/地址关键词是否准确，或稍后重试（限流）。")

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


# ------------------------------------------------------------------ 辅助函数
def _distance_m(d: Dict[str, Any]) -> Optional[int]:
    raw = pick(d, ("distance", "distanceMeters", "meters"))
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    m = re.search(r"([\d.]+)\s*(km|公里|m|米)?", str(raw), re.I)
    if not m:
        return None
    val = float(m.group(1))
    return int(val * 1000) if (m.group(2) or "").lower() in ("km", "公里") else int(val)


def to_coupons(rows: List[Dict[str, Any]]) -> List[Coupon]:
    out: List[Coupon] = []
    for d in rows:
        cid, ccode = pick(d, COUPON_ID_KEYS), pick(d, COUPON_CODE_KEYS)
        name = pick(d, COUPON_NAME_KEYS)
        if not (cid or ccode or name):
            continue
        products = d.get("products") or []
        codes = [str(pick(p, MEAL_CODE_KEYS)) for p in products if isinstance(p, dict) and pick(p, MEAL_CODE_KEYS)]
        labels: List[str] = []
        instr = d.get("instructions") or {}
        for lab in (instr.get("labels") or []) if isinstance(instr, dict) else []:
            if isinstance(lab, dict) and lab.get("text"):
                labels.append(str(lab["text"]))
        valid = str(d.get("tradeDateTime") or d.get("tradeEndDate") or "")
        out.append(Coupon(coupon_id=(str(cid) if cid else None),
                          coupon_code=(str(ccode) if ccode else None),
                          name=str(name or ""), raw=d, product_codes=codes,
                          scope_labels=labels, valid_until=valid))
    return out


def dedupe(coupons: List[Coupon]) -> List[Coupon]:
    seen, out = set(), []
    for c in coupons:
        k = (c.coupon_id, c.coupon_code)
        if k in seen or not any(k):
            continue
        seen.add(k)
        out.append(c)
    return out


def extract_amounts(body: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """calculate-price 的真实字段全部是「分」整数。

    实测语义：``productPrice`` 已是**优惠后**商品价，
    ``price`` = productPrice + deliveryPrice + packingPrice；``originalPrice`` 为优惠前合计。
    若加总对不上，说明服务端口径变化，调用方会在 notes 里显式提示而不是静默采信。
    """
    goods = to_cents(body.get("productPrice"), "cents")
    fee = to_cents(body.get("deliveryPrice"), "cents") or 0
    packing = to_cents(body.get("packingPrice"), "cents") or 0
    total = to_cents(body.get("price"), "cents")
    out = {
        "goods": goods,
        "fee": to_cents(body.get("deliveryPrice"), "cents"),
        "packing": to_cents(body.get("packingPrice"), "cents"),
        "discount": to_cents(body.get("discount"), "cents"),
        "original": to_cents(body.get("originalPrice"), "cents"),
        "total": total,
    }
    if None not in (goods, total) and goods + fee + packing != total:
        out["_consistency"] = f"官方返回 price={total} 与 商品+配送+打包={goods + fee + packing} 不一致"
    else:
        out["_consistency"] = None
    return out


def apply_amounts(quote: ChannelQuote, a: Dict[str, Optional[int]]) -> None:
    if a.get("_consistency"):
        quote.notes.append("金额口径校验：" + a["_consistency"])
    quote.original_cents = a.get("original")
    quote.goods_cents = a.get("goods")
    quote.fee_cents = a.get("fee")
    quote.packing_cents = a.get("packing")
    quote.discount_cents = a.get("discount")
    quote.total_cents = a.get("total")
    quote.source = "official"
