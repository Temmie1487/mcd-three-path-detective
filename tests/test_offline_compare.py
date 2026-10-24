import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mcd_three_path.compare import Comparator  # noqa: E402
from mcd_three_path.model import Channel  # noqa: E402
from mcd_three_path.report import to_html, to_json, to_markdown, to_terminal  # noqa: E402
from mcd_three_path.tools import ReplayCassette, SchemaMismatch, ToolCatalog  # noqa: E402

CASSETTE = os.path.join(os.path.dirname(__file__), "..", "fixtures", "demo-cassette.json")
ITEMS = "巨无霸 + 麦辣鸡翅2块 + 2份中薯条 + 可乐(中)"


@pytest.fixture()
def cmp_result():
    catalog = ToolCatalog(client=None, replay=ReplayCassette(CASSETTE))
    engine = Comparator(catalog, money_unit="cents")
    return engine.compare("上海", "南京西路", ITEMS)


def test_cassette_is_marked_as_demo():
    assert ReplayCassette(CASSETTE).source == "demo"


def test_all_three_channels_are_computed(cmp_result):
    assert {q.channel for q in cmp_result.quotes} == set(Channel.all())
    assert all(q.available for q in cmp_result.quotes)


def test_best_path_is_pickup_with_coupon(cmp_result):
    best = cmp_result.best
    assert best.channel is Channel.IN_STORE
    assert best.total_cents == 6150          # 7550 - 1400
    assert best.coupon and "薯条" in best.coupon.label
    assert best.source == "official"


def test_delivery_includes_delivery_fee(cmp_result):
    dl = next(q for q in cmp_result.quotes if q.channel is Channel.DELIVERY)
    assert dl.fee_cents == 900
    assert dl.total_cents == 7450            # 7550 + 900 - 1000


def test_drive_thru_degrades_honestly(cmp_result):
    """演示数据里两张券都不参与得来速：必须保留无券价并说明原因，禁止用别的渠道价冒充。"""
    dt = next(q for q in cmp_result.quotes if q.channel is Channel.DRIVE_THRU)
    assert dt.total_cents == 7550
    assert dt.coupon is None
    assert any("不可用" in n for n in dt.notes)


def test_price_never_uses_estimated_source_here(cmp_result):
    assert all(q.source == "official" for q in cmp_result.quotes)


def test_reports_render(cmp_result):
    md, html, payload = to_markdown(cmp_result), to_html(cmp_result), to_json(cmp_result)
    assert "到店自取" in md and "官方实算" in md
    assert "<svg" not in html and "麦麦三路比价报告" in html
    assert "演示" in html or "demo" in html.lower()
    assert payload.count('"total_yuan"') == 3


def test_trace_records_every_call(cmp_result):
    tools = {s["tool"] for s in cmp_result.trace.steps}
    assert {"now-time-info", "query-nearby-stores", "calculate-price"} <= tools
    # 券不可用的调用也要留痕，这是"诚实降级"的证据链
    assert any(not s["ok"] for s in cmp_result.trace.steps)


def test_schema_guard_requires_city_and_keyword():
    catalog = ToolCatalog(client=None, replay=ReplayCassette(CASSETTE))
    with pytest.raises(SchemaMismatch):
        catalog.call("query-nearby-stores", city="上海")


def test_unmatched_item_is_reported_not_invented():
    catalog = ToolCatalog(client=None, replay=ReplayCassette(CASSETTE))
    engine = Comparator(catalog, money_unit="cents")
    res = engine.compare("上海", "南京西路", "麦辣鸡腿堡 + 板烧鸡腿堡")
    assert res.warnings, "菜单里没有的商品必须给出告警，而不是编一个价格"
