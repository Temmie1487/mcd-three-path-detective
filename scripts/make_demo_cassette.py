#!/usr/bin/env python3
"""生成本仓库自带的离线演示录制文件（demo-cassette.json）。

⚠️ 这份文件里的价格、门店、优惠券全部是**演示数据**（meta.source = "demo"），
    仅用于让评审在**没有 MCP Token** 的情况下三行命令跑通全链路，
    不代表麦当劳官方实时价格，也不构成任何报价依据。
    参赛者可用 `python -m mcd_three_path probe --record` 录制自己的真实响应替换它。
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

SCHEMA_STORE = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "keyword": {"type": "string"},
        "longitude": {"type": "number"},
        "latitude": {"type": "number"},
    },
    "required": ["city", "keyword"],
}
SCHEMA_DELIV_STORES = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "keyword": {"type": "string"},
        "address": {"type": "string"},
    },
}
SCHEMA_STORE_ID = {
    "type": "object",
    "properties": {"storeId": {"type": "string"}},
    "required": ["storeId"],
}
SCHEMA_CALC = {
    "type": "object",
    "properties": {
        "storeId": {"type": "string"},
        "orderMode": {"type": "string"},
        "items": {"type": "array"},
        "couponId": {"type": "string"},
        "couponCode": {"type": "string"},
    },
    "required": ["storeId", "orderMode", "items"],
}

MENU_ITEMS = [
    {"mealCode": "BIGMAC", "mealName": "巨无霸", "price": 2350},
    {"mealCode": "MCSPICYWING", "mealName": "麦辣鸡翅2块", "price": 1450},
    {"mealCode": "FRIES_M", "mealName": "中薯条", "price": 1400},
    {"mealCode": "COLA_M", "mealName": "可乐(中)", "price": 950},
    {"mealCode": "SUNDIE", "mealName": "圆筒冰淇淋", "price": 500},
    {"mealCode": "MCCHICKEN", "mealName": "麦辣鸡腿汉堡", "price": 2250},
]


def menu_payload(store_id: str):
    return {
        "storeId": store_id,
        "menu": [{"categoryId": "101", "categoryName": "汉堡/主食", "items": MENU_ITEMS[:4]},
                 {"categoryId": "201", "categoryName": "小食/甜品", "items": MENU_ITEMS[4:]}],
    }


def calc(store_id, mode, coupon_id, goods, fee, discount):
    total = goods + fee - discount
    return {
        "tool": "calculate-price",
        "match": {"storeId": store_id, "orderMode": mode, "couponId": coupon_id},
        "response": {
            "ok": True,
            "data": {
                "storeId": store_id,
                "orderMode": mode,
                "goodsAmount": goods,
                "deliveryFee": fee,
                "discountAmount": discount,
                "totalPay": total,
                "currencyUnit": "cent",
            },
            "raw": None,
            "error": None,
            "code": None,
        },
    }


def calc_fail(store_id, mode, coupon_id, code=600012, msg="当前优惠券不可用"):
    return {
        "tool": "calculate-price",
        "match": {"storeId": store_id, "orderMode": mode, "couponId": coupon_id},
        "response": {"ok": False, "data": None, "raw": None,
                     "error": f"{msg}({code})", "code": code},
    }


STORE_IN = "1990583"
STORE_DT = "1990583"   # 同一家门店，同时支持到店自取与得来速
STORE_DL = "1990601"

cassette = {
    "meta": {
        "source": "demo",
        "generated_by": "scripts/make_demo_cassette.py",
        "disclaimer": "演示数据，非麦当劳官方实时价格；仅用于无 Token 的离线复现与 CI。",
    },
    "tools": [
        {"name": "now-time-info", "description": "获取当前时间信息", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "query-nearby-stores", "description": "查询附近可用门店", "inputSchema": SCHEMA_STORE},
        {"name": "delivery-query-addresses", "description": "获取用户可配送地址列表", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "delivery-query-stores", "description": "查询可配送的门店列表", "inputSchema": SCHEMA_DELIV_STORES},
        {"name": "query-meals", "description": "查询当前可售卖的餐品列表", "inputSchema": SCHEMA_STORE_ID},
        {"name": "query-store-coupons", "description": "查询当前门店可用优惠券", "inputSchema": SCHEMA_STORE_ID},
        {"name": "query-my-coupons", "description": "我的优惠券查询", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "available-coupons", "description": "麦麦省券列表查询", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "calculate-price", "description": "商品价格计算", "inputSchema": SCHEMA_CALC},
    ],
    "tapes": [
        {"tool": "now-time-info", "match": {},
         "response": {"ok": True, "data": {"currentTime": "2026-10-24T21:47:00+08:00",
                                           "weekday": "星期六"}, "raw": None, "error": None, "code": None}},
        {"tool": "query-nearby-stores", "match": {"city": "上海", "keyword": "南京西路"},
         "response": {"ok": True, "data": {"stores": [
             {"storeId": STORE_IN, "storeName": "上海南京西路餐厅（演示）",
              "distance": 850, "tags": ["堂食", "得来速", "24小时"]},
             {"storeId": "1990777", "storeName": "上海人民广场餐厅（演示）",
              "distance": 2100, "tags": ["堂食"]},
         ]}, "raw": None, "error": None, "code": None}},
        {"tool": "delivery-query-addresses", "match": {},
         "response": {"ok": True, "data": {"addresses": [
             {"addressId": "ADDR-DEMO-1", "address": "上海市黄浦区演示路 1 号"},
         ]}, "raw": None, "error": None, "code": None}},
        {"tool": "delivery-query-stores", "match": {"city": "上海"},
         "response": {"ok": True, "data": {"stores": [
             {"storeId": STORE_DL, "storeName": "麦乐送演示路配送站（演示）", "distance": 1200},
         ]}, "raw": None, "error": None, "code": None}},
        {"tool": "query-meals", "match": {"storeId": STORE_IN},
         "response": {"ok": True, "data": menu_payload(STORE_IN), "raw": None, "error": None, "code": None}},
        {"tool": "query-meals", "match": {"storeId": STORE_DL},
         "response": {"ok": True, "data": menu_payload(STORE_DL), "raw": None, "error": None, "code": None}},
        {"tool": "query-my-coupons", "match": {},
         "response": {"ok": True, "data": {"coupons": [
             {"couponId": "C-DEMO-FRIES", "couponCode": "9900012387",
              "couponName": "13.5元中薯条买一送一", "scope": "到店专用"},
             {"couponId": "C-DEMO-DELIV", "couponCode": "9900012400",
              "couponName": "麦乐送满49减10", "scope": "外送专用"},
         ]}, "raw": None, "error": None, "code": None}},
        {"tool": "query-store-coupons", "match": {"storeId": STORE_IN},
         "response": {"ok": True, "data": {"coupons": [
             {"couponId": "C-DEMO-SUNDIE", "couponCode": "9900012411",
              "couponName": "3.9元圆筒冰淇淋", "scope": "到店专用"},
         ]}, "raw": None, "error": None, "code": None}},
        {"tool": "query-store-coupons", "match": {"storeId": STORE_DL},
         "response": {"ok": True, "data": {"coupons": []}, "raw": None, "error": None, "code": None}},
        {"tool": "available-coupons", "match": {},
         "response": {"ok": True, "data": {"coupons": [
             {"couponId": "A-1", "couponName": "可领：9.9元脆汁鸡任选"},
             {"couponId": "A-2", "couponName": "可领：新派组合两件套"},
         ]}, "raw": None, "error": None, "code": None}},

        # 到店自取：无券 7550，挂「中薯条买一送一」省 1400 → 6150
        calc(STORE_IN, "pickup", None, 7550, 0, 0),
        calc(STORE_IN, "pickup", "C-DEMO-FRIES", 7550, 0, 1400),
        calc_fail(STORE_IN, "pickup", "C-DEMO-DELIV"),
        calc_fail(STORE_IN, "pickup", "C-DEMO-SUNDIE"),
        # 得来速：同一门店同一商品金额，但两张券都不参与 → 只能原价
        calc(STORE_DT, "driveThru", None, 7550, 0, 0),
        calc_fail(STORE_DT, "driveThru", "C-DEMO-FRIES"),
        calc_fail(STORE_DT, "driveThru", "C-DEMO-DELIV"),
        calc_fail(STORE_DT, "driveThru", "C-DEMO-SUNDIE"),
        # 麦乐送：商品 7550 + 配送费 900，满49减10 → 7450
        calc(STORE_DL, "delivery", None, 7550, 900, 0),
        calc(STORE_DL, "delivery", "C-DEMO-DELIV", 7550, 900, 1000),
        calc_fail(STORE_DL, "delivery", "C-DEMO-FRIES"),
        calc_fail(STORE_DL, "delivery", "C-DEMO-SUNDIE"),
    ],
}

out = os.path.join(HERE, "..", "fixtures", "demo-cassette.json")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "w", encoding="utf-8") as fp:
    json.dump(cassette, fp, ensure_ascii=False, indent=2)
print("written:", os.path.abspath(out), len(cassette["tapes"]), "tapes")
