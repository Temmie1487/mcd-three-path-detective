import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mcd_three_path.compare import Comparator, envelope  # noqa: E402
from mcd_three_path.model import Channel  # noqa: E402
from mcd_three_path.report import to_html, to_json, to_markdown, to_terminal  # noqa: E402
from mcd_three_path.tools import ReplayCassette, SchemaMismatch, ToolCatalog  # noqa: E402

CASSETTE = os.path.join(os.path.dirname(__file__), "..", "fixtures", "demo-cassette.json")
ITEMS = "巨无霸 + 麦辣鸡翅 + 2份薯条 + 可乐"


@pytest.fixture(scope="module")
def cmp_result():
    catalog = ToolCatalog(client=None, replay=ReplayCassette(CASSETTE))
    return Comparator(catalog).compare("上海", "南京西路", ITEMS)


def q(cmp, ch):
    return next(x for x in cmp.quotes if x.channel is ch)


# ---------------------------------------------------------------- 录制文件契约
def test_cassette_is_marked_as_demo_and_schema_aligned():
    meta = json.load(open(CASSETTE, encoding="utf-8"))["meta"]
    assert meta["source"] == "demo"
    assert "官方实时价格" in meta["disclaimer"]
    assert ReplayCassette(CASSETTE).source == "demo"


def test_envelope_detects_business_failure_despite_http_ok():
    """最关键的坑：HTTP 成功但 success=false（508 / 60006 / 600012）。"""
    data, err = envelope({"success": False, "code": 508, "message": "太火爆了，请稍后再试", "data": None})
    assert data is None and "508" in err
    data, err = envelope({"success": True, "code": 200, "data": {"price": 100}})
    assert data == {"price": 100} and err is None


# ---------------------------------------------------------------- 三路实算结果
def test_all_three_channels_available(cmp_result):
    assert {x.channel for x in cmp_result.quotes} == set(Channel.all())
    assert all(x.available for x in cmp_result.quotes), [x.unavailable_reason for x in cmp_result.quotes]
    assert all(x.source == "official" for x in cmp_result.quotes)


def test_in_store_is_best_and_uses_eligible_coupon(cmp_result):
    ins = q(cmp_result, Channel.IN_STORE)
    assert cmp_result.best is ins
    assert ins.total_cents == 6500                 # 7900 - 1400
    assert ins.discount_cents == 1400
    assert ins.fee_cents in (None, 0) and ins.packing_cents in (None, 0)
    assert ins.coupon and "薯条" in ins.coupon.name


def test_drive_thru_keeps_list_price_when_coupons_rejected(cmp_result):
    """诚实降级：券被 600012 拒掉时保留无券官方实算价，并把原因写进 notes。"""
    dt = q(cmp_result, Channel.DRIVE_THRU)
    assert dt.total_cents == 7900
    assert dt.coupon is None
    assert any("600012" in n for n in dt.notes)


def test_delivery_priced_higher_by_fee_and_packing(cmp_result):
    dl = q(cmp_result, Channel.DELIVERY)
    assert dl.goods_cents == 8600                  # 同名商品在外送单价更高
    assert dl.fee_cents == 600                     # 配送费 ¥6
    assert dl.packing_cents == 190                 # 打包服务费 ¥1.9
    assert dl.discount_cents == 1000
    assert dl.total_cents == 8390                  # 8600+600+190-1000
    assert dl.extra_cost_cents == 790
    assert cmp_result.worst is dl


def test_channel_gap_is_reported(cmp_result):
    save = cmp_result.worst.total_cents - cmp_result.best.total_cents
    assert save == 1890
    text = to_terminal(cmp_result)
    assert "到店自取" in text and "麦乐送" in text and "省 ¥18.90" in text


def test_cross_channel_coupon_whitelist_is_surfaced(cmp_result):
    dl = q(cmp_result, Channel.DELIVERY)
    assert any("白名单" in n for n in dl.notes), "必须说明为什么有些券在这路压根没试"


# ---------------------------------------------------------------- 渠道参数契约
def test_channels_use_real_order_and_be_types():
    assert (Channel.IN_STORE.order_type, Channel.IN_STORE.be_type) == (1, 1)
    assert (Channel.DRIVE_THRU.order_type, Channel.DRIVE_THRU.be_type) == (1, 5)
    assert (Channel.DELIVERY.order_type, Channel.DELIVERY.be_type) == (2, 2)
    assert Channel.IN_STORE.needs_be_code is False
    assert Channel.DRIVE_THRU.needs_be_code and Channel.DELIVERY.needs_be_code


def test_be_code_is_required_for_non_pickup_channels():
    """缺 beCode 时服务端静默返回空，本项目必须自己拦住。"""
    catalog = ToolCatalog(client=None, replay=ReplayCassette(CASSETTE))
    with pytest.raises(SchemaMismatch):
        catalog.call("calculate-price", storeCode="1", orderType=1, beType=5,
                     items=[{"productCode": "1", "quantity": 1}])


def test_nearby_stores_requires_be_type_and_search_type():
    catalog = ToolCatalog(client=None, replay=ReplayCassette(CASSETTE))
    with pytest.raises(SchemaMismatch):
        catalog.call("query-nearby-stores", city="上海", keyword="南京西路")


def test_delivery_chain_needs_address_first():
    """麦乐送门店来自 addressId，不是 city+keyword。"""
    catalog = ToolCatalog(client=None, replay=ReplayCassette(CASSETTE))
    with pytest.raises(SchemaMismatch):
        catalog.call("delivery-query-stores", beType=2)


def test_store_codes_differ_across_channels(cmp_result):
    """实测核心事实：同一件商品在三个渠道的 productCode 不一定相同。"""
    codes = {ch: {i.code for i in q(cmp_result, ch).items if i.code} for ch in Channel.all()}
    assert codes[Channel.IN_STORE] != codes[Channel.DELIVERY]


# ---------------------------------------------------------------- 报告与轨迹
def test_reports_render(cmp_result):
    md, html, payload = to_markdown(cmp_result), to_html(cmp_result), to_json(cmp_result)
    assert "打包服务费" in md and "官方实算" in md
    assert "麦麦三路比价报告" in html
    assert payload.count('"total_yuan"') == 3
    assert '"packing_yuan"' in payload


def test_trace_records_every_call_including_failures(cmp_result):
    tools = {s["tool"] for s in cmp_result.trace.steps}
    assert {"now-time-info", "query-nearby-stores", "delivery-query-addresses",
            "delivery-query-stores", "query-meals", "query-store-coupons",
            "calculate-price"} <= tools
    assert any(not s["ok"] for s in cmp_result.trace.steps)


def test_unmatched_item_is_reported_not_invented():
    catalog = ToolCatalog(client=None, replay=ReplayCassette(CASSETTE))
    res = Comparator(catalog).compare("上海", "南京西路", "麦辣鸡腿堡 + 板烧鸡腿堡")
    assert res.warnings, "菜单里没有的商品必须给出告警，而不是编一个价格"
