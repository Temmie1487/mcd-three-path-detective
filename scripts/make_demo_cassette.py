#!/usr/bin/env python3
"""生成仓库自带的离线演示录制文件（fixtures/demo-cassette.json）。

录制文件的**结构与字段名严格对齐麦当劳 MCP 线上真实返回**（含 {success,code,message,data}
信封、菜单价用「元」字符串、算价用「分」整数、券带 products 白名单、渠道用 orderType/beType/beCode
区分），但**内容是合成的演示数据**：门店编码、商品编码、券 ID、配送地址全部为虚构，
不含任何真实账号、个人地址或凭证。

用途：让没有 MCP Token 的人也能三行命令跑通完整链路、看到报告形态。
⚠️ 其中所有价格都不是麦当劳官方实时价格，不能作为任何报价依据。
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- 真实 inputSchema（由线上 tools/list 取得，字段名与枚举保持一致）----------------
SCHEMA_NEARBY = {
    "type": "object",
    "properties": {
        "beType": {"type": "integer", "enum": [1, 5],
                   "description": "1：到店自提, 5-得来速车道取餐(Drive Through)"},
        "city": {"type": "string", "description": "城市名"},
        "keyword": {"type": "string", "description": "关键词"},
        "searchType": {"type": "integer", "enum": [1, 2], "default": 1,
                       "description": "searchType=2 按位置进行搜索, 1 搜索收藏餐厅"},
    },
    "required": ["beType", "searchType"],
}
SCHEMA_ADDRESS = {"type": "object", "properties": {}}
SCHEMA_DELIV_STORES = {
    "type": "object",
    "properties": {
        "addressId": {"type": "string", "description": "地址ID，来源delivery-query-address"},
        "beType": {"type": "integer", "description": "beType：2: 麦乐送，6：团餐"},
    },
    "required": ["beType", "addressId"],
}


def meals_schema(extra_store=True):
    return {
        "type": "object",
        "properties": {
            "beCode": {"type": "string"},
            "beType": {"type": "integer", "enum": [1, 2, 5, 6]},
            "orderType": {"type": "integer", "enum": [1, 2]},
            "storeCode": {"type": "string"},
        },
        "required": ["storeCode", "orderType", "beType"],
    }


SCHEMA_CALC = {
    "type": "object",
    "properties": {
        "beCode": {"type": "string"},
        "beType": {"type": "integer", "enum": [1, 2, 5, 6]},
        "orderType": {"type": "integer", "enum": [1, 2]},
        "storeCode": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "productCode": {"type": "string"},
                    "quantity": {"type": "integer"},
                    "couponId": {"type": "string"},
                    "couponCode": {"type": "string"},
                },
            },
        },
    },
    "required": ["storeCode", "orderType", "beType", "items"],
}

STORE_IN_DT = "1420001"     # 同一家门店，同时支持到店自取与得来速
BE_CODE_DT = "142000141"    # 得来速渠道的 beCode
STORE_DL = "1420001"
BE_CODE_DL = "142000102"    # 外送渠道的 beCode（实测与得来速不同）

# 同一件商品在不同渠道的 productCode 与售价都不同 —— 这是三路比价真正的难点
MENU_INSTORE = [
    {"productCode": "1100", "name": "巨无霸", "currentPrice": "26.5", "originalPrice": "26.5"},
    {"productCode": "1700", "name": "麦辣鸡翅", "currentPrice": "15", "originalPrice": "15"},
    {"productCode": "4810", "name": "薯条", "currentPrice": "14", "originalPrice": "14"},
    {"productCode": "9900008751", "name": "可乐", "currentPrice": "9.5", "originalPrice": "9.5"},
    {"productCode": "2200", "name": "板烧鸡腿堡", "currentPrice": "23.5", "originalPrice": "23.5"},
    {"productCode": "3300", "name": "圆筒冰淇淋", "currentPrice": "5", "originalPrice": "5"},
]
MENU_DELIVERY = [
    {"productCode": "901100", "name": "巨无霸", "currentPrice": "29", "originalPrice": "29"},
    {"productCode": "901700", "name": "麦辣鸡翅2块", "currentPrice": "16", "originalPrice": "16"},
    {"productCode": "501205", "name": "薯条", "currentPrice": "15", "originalPrice": "15"},
    {"productCode": "903050", "name": "可口可乐", "currentPrice": "11", "originalPrice": "11"},
    {"productCode": "902200", "name": "板烧鸡腿堡", "currentPrice": "25.5", "originalPrice": "25.5"},
    {"productCode": "903300", "name": "圆筒冰淇淋", "currentPrice": "5.5", "originalPrice": "5.5"},
]

COUPONS_HELD = [
    {"id": "DEMO-CID-0001", "code": "MCDDEMO0001", "title": "薯条买一送一",
     "instructions": {"labels": [{"text": "到店"}]},
     "products": [{"productCode": "4810", "productName": "薯条"}],
     "tradeDateTime": "2026-10-20 00:00:00-2026-10-30 23:59:59"},
    {"id": "DEMO-CID-0002", "code": "MCDDEMO0002", "title": "麦乐送满49减10",
     "instructions": {"labels": [{"text": "外送"}]},
     "products": [],
     "tradeDateTime": "2026-10-20 00:00:00-2026-10-30 23:59:59"},
]
COUPONS_STORE = [
    {"couponId": "DEMO-CID-0003", "couponCode": "MCDDEMO0003", "title": "1024“码”力翻倍",
     "products": [{"productCode": "1100", "productName": "巨无霸"}],
     "tradeDateTime": "2026-10-24 00:00:00-2026-10-30 23:59:59"},
]


def env(payload, success=True, code=200, message="请求成功"):
    return {"ok": True, "data": {"success": success, "code": code, "message": message,
                                 "datetime": "2026-10-24 21:47:00", "traceId": "demo-trace",
                                 "data": payload},
            "raw": None, "error": None, "code": None}


def fail(message, code):
    return {"ok": True, "data": {"success": False, "code": code, "message": message,
                                 "datetime": "2026-10-24 21:47:00", "traceId": "demo-trace",
                                 "data": None},
            "raw": None, "error": None, "code": None}


def store_row(name, be_code=None, distance=850, start="07:00", end="23:00", status=True):
    row = {"storeCode": STORE_IN_DT, "storeName": name, "address": "演示东路 1 号（演示数据）",
           "distance": distance, "businessStatus": status,
           "businessStartTime": start, "businessEndTime": end,
           "reservation": True,
           "reservationTimeOptions": [{"date": "2026-10-25", "today": False,
                                       "reservationOptionText": "早餐(07:14至10:15)，午餐(10:44至14:15)"}]}
    if be_code:
        row["beCode"] = be_code
    return row


def calc(store_code, order_type, be_type, be_code, goods, fee, packing, discount):
    fee = fee or 0
    packing = packing or 0
    total = goods + fee + packing - discount
    payload = {"productOriginalPrice": goods, "productPrice": goods,
               "originalPrice": goods + fee + packing, "discount": discount, "price": total}
    if fee:
        payload["deliveryOriginalPrice"] = fee
        payload["deliveryPrice"] = fee
    if packing:
        payload["packingOriginalPrice"] = packing
        payload["packingPrice"] = packing
    return payload


def tape(tool, match, response):
    return {"tool": tool, "match": match, "response": response}


CART_BASE_IN = 2650 + 1500 + 2 * 1400 + 950     # 巨无霸+鸡翅+2份薯条+可乐 = ¥75.00
CART_BASE_DL = 2900 + 1600 + 2 * 1500 + 1100    # 外送同名商品 = ¥86.00

cassette = {
    "meta": {
        "source": "demo",
        "generated_by": "scripts/make_demo_cassette.py",
        "schema_aligned_to": "MCD MCP v1.0.9（线上 35 工具，字段名取自 tools/list 真实 inputSchema）",
        "disclaimer": "演示数据：门店/商品/券编码均为虚构，价格非麦当劳官方实时价格，"
                      "仅用于无 Token 的离线复现与 CI，不构成任何报价依据。",
    },
    "tools": [
        {"name": "now-time-info", "description": "获取当前时间信息", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "query-nearby-stores", "description": "查询附近可用门店", "inputSchema": SCHEMA_NEARBY},
        {"name": "delivery-query-addresses", "description": "获取用户可配送地址列表", "inputSchema": SCHEMA_ADDRESS},
        {"name": "delivery-query-stores", "description": "查询可配送的门店列表", "inputSchema": SCHEMA_DELIV_STORES},
        {"name": "query-meals", "description": "查询当前可售卖的餐品列表", "inputSchema": meals_schema()},
        {"name": "query-store-coupons", "description": "查询当前门店下可用的优惠券列表", "inputSchema": meals_schema()},
        {"name": "query-my-coupons", "description": "我的优惠券查询",
         "inputSchema": {"type": "object", "properties": {"page": {"type": "string"},
                                                          "pageSize": {"type": "string"}}}},
        {"name": "available-coupons", "description": "麦麦省券列表查询", "inputSchema": SCHEMA_ADDRESS},
        {"name": "calculate-price", "description": "商品价格计算", "inputSchema": SCHEMA_CALC},
    ],
    "tapes": [
        tape("now-time-info", {}, env({"currentTime": "2026-10-24 21:47:00", "weekday": "星期六"})),
        tape("query-nearby-stores", {"beType": 1, "searchType": 2, "city": "上海"},
             env([store_row("麦当劳演示东路餐厅（演示）", None, 850, "00:00", "23:59")])),
        tape("query-nearby-stores", {"beType": 5, "searchType": 2, "city": "上海"},
             env([store_row("麦当劳演示东路得来速餐厅（演示）", BE_CODE_DT, 850, "07:00", "23:00")])),
        tape("delivery-query-addresses", {},
             env({"addresses": [{"addressId": "DEMO-ADDRESS-0001", "contactName": "Demo",
                                 "phone": "138****0000", "fullAddress": "上海市演示区演示路 1 号（演示数据）"}]})),
        tape("delivery-query-stores", {"beType": 2, "addressId": "DEMO-ADDRESS-0001"},
             env([store_row("麦当劳演示东路餐厅（演示·外送）", BE_CODE_DL, 1200, "09:00", "22:00")])),
        tape("query-meals", {"storeCode": STORE_IN_DT, "orderType": 1, "beType": 1},
             env({"categories": [{"name": "人气热卖", "meals": [{"code": m["productCode"]} for m in MENU_INSTORE]}],
                  "meals": {m["productCode"]: m for m in MENU_INSTORE}})),
        tape("query-meals", {"storeCode": STORE_IN_DT, "orderType": 1, "beType": 5, "beCode": BE_CODE_DT},
             env({"categories": [], "meals": {m["productCode"]: m for m in MENU_INSTORE}})),
        tape("query-meals", {"storeCode": STORE_DL, "orderType": 2, "beType": 2, "beCode": BE_CODE_DL},
             env({"categories": [], "meals": {m["productCode"]: m for m in MENU_DELIVERY}})),
        tape("query-my-coupons", {}, env({"coupons": COUPONS_HELD, "totalCount": len(COUPONS_HELD),
                                          "currentPage": 1, "pageSize": 200, "totalPages": 1})),
        tape("query-store-coupons", {"storeCode": STORE_IN_DT, "orderType": 1, "beType": 1},
             env(COUPONS_STORE)),
        tape("query-store-coupons", {"storeCode": STORE_IN_DT, "orderType": 1, "beType": 5, "beCode": BE_CODE_DT},
             env([])),
        tape("query-store-coupons", {"storeCode": STORE_DL, "orderType": 2, "beType": 2, "beCode": BE_CODE_DL},
             env([])),
        tape("available-coupons", {}, env({"coupons": [{"couponId": "DEMO-AVAILABLE-1"},
                                                       {"couponId": "DEMO-AVAILABLE-2"}]})),

        # 到店自取：¥75.00，薯条买一送一（白名单含 4810）→ 省 ¥14.00
        tape("calculate-price",
             {"storeCode": STORE_IN_DT, "orderType": 1, "beType": 1,
              "items": [{"productCode": "1100", "quantity": 1}, {"productCode": "1700", "quantity": 1},
                        {"productCode": "4810", "quantity": 2}, {"productCode": "9900008751", "quantity": 1}]},
             env(calc(STORE_IN_DT, 1, 1, None, CART_BASE_IN, None, None, 0))),
        tape("calculate-price",
             {"storeCode": STORE_IN_DT, "orderType": 1, "beType": 1,
              "items": [{"productCode": "1100", "quantity": 1}, {"productCode": "1700", "quantity": 1},
                        {"productCode": "4810", "quantity": 2, "couponId": "DEMO-CID-0001",
                         "couponCode": "MCDDEMO0001"},
                        {"productCode": "9900008751", "quantity": 1}]},
             env(calc(STORE_IN_DT, 1, 1, None, CART_BASE_IN, None, None, 1400))),
        tape("calculate-price",
             {"storeCode": STORE_IN_DT, "orderType": 1, "beType": 1,
              "items": [{"productCode": "1100", "quantity": 1, "couponId": "DEMO-CID-0002",
                         "couponCode": "MCDDEMO0002"},
                        {"productCode": "1700", "quantity": 1},
                        {"productCode": "4810", "quantity": 2}, {"productCode": "9900008751", "quantity": 1}]},
             fail("当前优惠券不可用", 600012)),
        tape("calculate-price",
             {"storeCode": STORE_IN_DT, "orderType": 1, "beType": 1,
              "items": [{"productCode": "1100", "quantity": 1, "couponId": "DEMO-CID-0003",
                         "couponCode": "MCDDEMO0003"},
                        {"productCode": "1700", "quantity": 1},
                        {"productCode": "4810", "quantity": 2}, {"productCode": "9900008751", "quantity": 1}]},
             env(calc(STORE_IN_DT, 1, 1, None, CART_BASE_IN, None, None, 500))),

        # 得来速：商品价与到店一致，但本店券与已持有券的商品白名单在该渠道不命中 → 原价
        tape("calculate-price",
             {"storeCode": STORE_IN_DT, "orderType": 1, "beType": 5, "beCode": BE_CODE_DT,
              "items": [{"productCode": "1100", "quantity": 1}, {"productCode": "1700", "quantity": 1},
                        {"productCode": "4810", "quantity": 2}, {"productCode": "9900008751", "quantity": 1}]},
             env(calc(STORE_IN_DT, 1, 5, BE_CODE_DT, CART_BASE_IN, None, None, 0))),
        tape("calculate-price",
             {"storeCode": STORE_IN_DT, "orderType": 1, "beType": 5, "beCode": BE_CODE_DT,
              "items": [{"productCode": "1100", "quantity": 1}, {"productCode": "1700", "quantity": 1},
                        {"productCode": "4810", "quantity": 2, "couponId": "DEMO-CID-0001",
                         "couponCode": "MCDDEMO0001"},
                        {"productCode": "9900008751", "quantity": 1}]},
             fail("当前优惠券不可用", 600012)),
        tape("calculate-price",
             {"storeCode": STORE_IN_DT, "orderType": 1, "beType": 5, "beCode": BE_CODE_DT,
              "items": [{"productCode": "1100", "quantity": 1, "couponId": "DEMO-CID-0002",
                         "couponCode": "MCDDEMO0002"},
                        {"productCode": "1700", "quantity": 1},
                        {"productCode": "4810", "quantity": 2}, {"productCode": "9900008751", "quantity": 1}]},
             fail("当前优惠券不可用", 600012)),
        tape("calculate-price",
             {"storeCode": STORE_IN_DT, "orderType": 1, "beType": 5, "beCode": BE_CODE_DT,
              "items": [{"productCode": "1100", "quantity": 1, "couponId": "DEMO-CID-0003",
                         "couponCode": "MCDDEMO0003"},
                        {"productCode": "1700", "quantity": 1},
                        {"productCode": "4810", "quantity": 2}, {"productCode": "9900008751", "quantity": 1}]},
             fail("当前优惠券不可用", 600012)),

        # 麦乐送：同名商品更贵（¥86.00）+ 配送费 ¥6 + 打包服务费 ¥1.9，满49减10 后 ¥83.90
        tape("calculate-price",
             {"storeCode": STORE_DL, "orderType": 2, "beType": 2, "beCode": BE_CODE_DL,
              "items": [{"productCode": "901100", "quantity": 1}, {"productCode": "901700", "quantity": 1},
                        {"productCode": "501205", "quantity": 2}, {"productCode": "903050", "quantity": 1}]},
             env(calc(STORE_DL, 2, 2, BE_CODE_DL, CART_BASE_DL, 600, 190, 0))),
        tape("calculate-price",
             {"storeCode": STORE_DL, "orderType": 2, "beType": 2, "beCode": BE_CODE_DL,
              "items": [{"productCode": "901100", "quantity": 1, "couponId": "DEMO-CID-0002",
                         "couponCode": "MCDDEMO0002"},
                        {"productCode": "901700", "quantity": 1},
                        {"productCode": "501205", "quantity": 2}, {"productCode": "903050", "quantity": 1}]},
             env(calc(STORE_DL, 2, 2, BE_CODE_DL, CART_BASE_DL, 600, 190, 1000))),
        # 到店薯条券的白名单是到店编码 4810，在外送渠道根本不存在 → 不会进入试挂列表
    ],
}

out = os.path.join(HERE, "..", "fixtures", "demo-cassette.json")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w", encoding="utf-8") as fp:
    json.dump(cassette, fp, ensure_ascii=False, indent=2)
print("written:", os.path.abspath(out), len(cassette["tapes"]), "tapes")
