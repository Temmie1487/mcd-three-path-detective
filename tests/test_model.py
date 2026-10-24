import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from mcd_three_path.model import to_cents, yuan, parse_items, match_score  # noqa: E402


def test_int_is_cents_by_default():
    assert to_cents(11450) == 11450
    assert yuan(11450) == "¥114.50"


def test_string_is_yuan():
    assert to_cents("114.5") == 11450
    assert to_cents("¥114.50") == 11450


def test_explicit_unit_override():
    assert to_cents(114, "yuan") == 11400
    assert to_cents(114.6, "cents") == 115  # 分口径下取整


def test_none_and_bool_are_ignored():
    assert to_cents(None) is None
    assert to_cents(False) is None
    assert to_cents("无") is None


def test_parse_items_with_chinese_quantity():
    items = parse_items("巨无霸 + 2份中薯条 + 麦辣鸡翅2块x2")
    names = {i.note for i in items}
    assert any("巨无霸" in n for n in names)
    assert any("中薯条" in n for n in names)
    assert any(i.qty >= 2 for i in items)


def test_match_score_priority():
    assert match_score("中薯条", "中薯条") == 1.0
    assert match_score("巨无霸", "双吉巨无霸") > match_score("巨无霸", "麦乐鸡")
