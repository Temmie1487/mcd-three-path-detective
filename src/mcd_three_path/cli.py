"""命令行入口。

    # 真实模式（Token 只走环境变量）
    export MCD_MCP_TOKEN=你的Token
    python -m mcd_three_path probe
    python -m mcd_three_path compare --city 深圳 --keyword 科苑南路 \\
        --items "巨无霸 + 麦辣鸡翅2块 + 2份中薯条 + 可乐(中)" --html report.html

    # 离线模式（无需 Token，三行命令就能看到完整产物，用录制文件复现）
    python -m mcd_three_path compare --replay fixtures/demo-cassette.json \\
        --city 上海 --keyword 南京西路 --items "巨无霸 + 中薯条" --markdown demo.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .client import McpClient, McpError, load_token, load_url, redact
from .compare import Comparator
from .model import Channel
from .report import to_html, to_json, to_markdown, to_terminal
from .tools import ReplayCassette, ToolCatalog


def _channels(csv: str) -> List[Channel]:
    alias = {
        "in": Channel.IN_STORE, "pickup": Channel.IN_STORE, "到店": Channel.IN_STORE,
        "del": Channel.DELIVERY, "delivery": Channel.DELIVERY, "外送": Channel.DELIVERY,
        "dt": Channel.DRIVE_THRU, "drive": Channel.DRIVE_THRU, "得来速": Channel.DRIVE_THRU,
    }
    out: List[Channel] = []
    for part in (csv or "in,del,dt").split(","):
        p = part.strip().lower()
        if not p:
            continue
        if p not in alias:
            raise SystemExit(f"未知渠道 `{part}`，可选：in/delivery/dt 或 到店/外送/得来速")
        out.append(alias[p])
    return out or Channel.all()


def _catalog(args: argparse.Namespace) -> Tuple[ToolCatalog, Optional[Dict[str, Any]]]:
    """返回 (catalog, cassette)。带 --record 时顺便装配录制器（自动脱敏）。"""
    if args.replay:
        return ToolCatalog(client=None, replay=ReplayCassette(args.replay)), None
    token = args.token or load_token()
    client = McpClient(url=args.url or load_url(), token=token)
    cassette: Optional[Dict[str, Any]] = None
    recorder_path = getattr(args, "record", None)
    if recorder_path:
        cassette = {
            "meta": {"source": "real", "recorded_at": datetime.now().isoformat(timespec="seconds"),
                     "note": "真实录制，已脱敏；可用于无 Token 复现。"},
            "tools": [], "tapes": [],
        }

        def _sink(name, arguments, res, _c=cassette):
            _c["tapes"].append({"tool": name, "match": arguments, "response": res})

        client.recorder = _sink
    return ToolCatalog(client=client), cassette


def _save_cassette(catalog: ToolCatalog, cassette: Optional[Dict[str, Any]], path: str) -> None:
    if cassette is None:
        return
    cassette["tools"] = list(catalog.load().values())
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(cassette, fp, ensure_ascii=False, indent=2)
    print(f"[已录制 {len(cassette['tapes'])} 次调用（脱敏）] {path}")


def cmd_probe(args: argparse.Namespace) -> int:
    catalog, cassette = _catalog(args)
    tools = catalog.load()
    print(f"接入地址：{args.url or load_url()}")
    print(f"工具数量：{len(tools)}")
    wanted = ["now-time-info", "query-nearby-stores", "delivery-query-addresses",
              "delivery-query-stores", "query-meals", "query-store-coupons",
              "query-my-coupons", "calculate-price", "available-coupons"]
    print("比价链路依赖：")
    for w in wanted:
        mark = "✅" if w in tools else "❌"
        print(f"  {mark} {w}")
    if not args.replay:
        ts, bucket = Comparator(catalog).server_now()
        print(f"服务端时间：{ts or '—'}（{bucket or '—'}）")
    if args.schema:
        for name in args.schema.split(","):
            print(f"\n--- {name} inputSchema ---")
            print(json.dumps(catalog.schema(name), ensure_ascii=False, indent=2)[:3000])
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    if not args.items:
        raise SystemExit("请用 --items 描述要点的东西，例如 --items \"巨无霸 + 中薯条x2\"")
    catalog, cassette = _catalog(args)
    engine = Comparator(catalog, money_unit=args.money_unit,
                        max_coupon_probes=args.max_coupon_probes, verbose=args.verbose)
    cmp = engine.compare(args.city, args.keyword, args.items,
                         channels=_channels(args.channels), force_store=args.store)
    if args.record:
        _save_cassette(catalog, cassette, args.record)

    print(to_terminal(cmp))
    md = to_markdown(cmp)
    if args.markdown:
        _write(args.markdown, md)
        print(f"\n[已写出 Markdown] {args.markdown}")
    if args.html:
        _write(args.html, to_html(cmp))
        print(f"[已写出 HTML 报告] {args.html}")
    if args.json_out:
        payload = to_json(cmp)
        _write(args.json_out, payload)
        print(f"[已写出 JSON] {args.json_out}")
    if args.print_json:
        print(to_json(cmp))
    if args.trace:
        print("\n=== MCP 调用轨迹 ===")
        print(cmp.trace.dump())
    unavailable = [q for q in cmp.quotes if not q.available]
    return 0 if cmp.best else 2


def cmd_skills(args: argparse.Namespace) -> int:
    """打印可直接贴给任意 MCP Client 的技能提示词（本项目的 Skill 形态）。"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "SKILL.md")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fp:
            print(fp.read())
        return 0
    print("仓库内未找到 SKILL.md", file=sys.stderr)
    return 1


def _write(path: str, content: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(content)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mcd-three-path",
        description="麦麦三路比价侦探：同一份餐在到店自取 / 麦乐送 / 得来速的真实到手价对比。",
        epilog="Token 只从环境变量 MCD_MCP_TOKEN 读取，绝不写入文件或提交到仓库。",
    )
    p.add_argument("--replay", metavar="FILE", help="离线回放录制文件（无需 Token）")
    p.add_argument("--url", help="MCP 接入地址，默认 https://mcp.mcd.cn")
    p.add_argument("--token", help="一般不要用命令行传 Token，建议用环境变量")
    p.add_argument("--verbose", action="store_true")

    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("probe", help="连通性自检 + 工具清单 + schema")
    sp.add_argument("--schema", help="逗号分隔的工具名，打印其 inputSchema")
    sp.set_defaults(func=cmd_probe)

    sc = sub.add_parser("compare", help="三路比价（核心命令）")
    sc.add_argument("--city", default="", help="城市名，如 深圳")
    sc.add_argument("--keyword", default="", help="地址/门店关键词，如 科苑南路")
    sc.add_argument("--items", required=True, help="点单内容，如 \"巨无霸 + 2份中薯条\"")
    sc.add_argument("--channels", default="in,del,dt", help="要对比的渠道，默认三路全开")
    sc.add_argument("--store", help="只用指定门店（id 或名称片段）")
    sc.add_argument("--money-unit", choices=["auto", "cents", "yuan"], default="auto")
    sc.add_argument("--max-coupon-probes", type=int, default=6, help="最多试挂几张券")
    sc.add_argument("--markdown", help="输出 Markdown 报告路径")
    sc.add_argument("--html", help="输出单页 HTML 报告路径")
    sc.add_argument("--json", dest="json_out", help="输出 JSON 结果路径")
    sc.add_argument("--print-json", action="store_true")
    sc.add_argument("--trace", action="store_true", help="打印 MCP 调用轨迹")
    sc.add_argument("--record", metavar="FILE", help="把本次真实调用脱敏录制下来，便于离线复现")
    sc.set_defaults(func=cmd_compare)

    sk = sub.add_parser("skill", help="打印 SKILL.md（贴进任意 MCP 客户端即可用）")
    sk.set_defaults(func=cmd_skills)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except McpError as exc:
        print(f"MCP 调用失败：{redact(str(exc))}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
