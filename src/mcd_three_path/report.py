"""把比价结果渲染成终端表格 / Markdown / 单页 HTML 报告。"""

from __future__ import annotations

import html
import json
from datetime import datetime
from typing import List

from .model import Channel, yuan
from .compare import Comparison

_ORDER = {Channel.DRIVE_THRU: "得来速", Channel.IN_STORE: "到店自取", Channel.DELIVERY: "麦乐送"}


def _delta_line(cmp: Comparison) -> str:
    best, worst = cmp.best, cmp.worst
    if not best or not worst or best.total_cents == worst.total_cents:
        return "三路到手价无差异（或可用渠道不足两路）。"
    save = worst.total_cents - best.total_cents
    pct = save / worst.total_cents * 100
    return (f"最优路径 **{best.channel.cn}** {yuan(best.total_cents)}，"
            f"比最贵的 {worst.channel.cn} {yuan(worst.total_cents)} "
            f"**省 {yuan(save)}（{pct:.1f}% off）**。")


def to_terminal(cmp: Comparison) -> str:
    lines = [f"点单：{cmp.item_text}"]
    if cmp.server_time:
        lines.append(f"服务端时间：{cmp.server_time}（{cmp.time_bucket}）")
    lines.append("")
    lines.append(f"{'渠道':<8}{'到手价':>10}{'商品':>10}{'配送费':>8}{'优惠':>8}  口径 / 门店 / 备注")
    for q in cmp.quotes:
        if q.available:
            lines.append(
                f"{q.channel.cn:<8}{yuan(q.total_cents):>10}{yuan(q.goods_cents):>10}"
                f"{yuan(q.fee_cents):>8}{yuan(q.discount_cents):>8}  "
                f"{q.badge}｜{q.store.name if q.store else '—'}"
                + (f"｜{q.coupon.label}" if q.coupon else "")
            )
        else:
            lines.append(f"{q.channel.cn:<8}{'—':>10}  不可用：{q.unavailable_reason}")
    lines.append("")
    lines.append(_delta_line(cmp))
    for w in cmp.warnings:
        lines.append(f"⚠️  {w}")
    for q in cmp.quotes:
        for n in q.notes:
            lines.append(f"· {q.channel.cn}：{n}")
    return "\n".join(lines)


def to_markdown(cmp: Comparison) -> str:
    rows = []
    for q in cmp.quotes:
        rows.append(
            f"| {q.channel.cn} | "
            + (f"{yuan(q.total_cents)}" if q.available else f"不可用（{q.unavailable_reason}）")
            + f" | {yuan(q.goods_cents)} | {yuan(q.fee_cents)} | {yuan(q.discount_cents)} "
            + f"| {q.badge} | {html.escape(q.store.name) if q.store else '—'} "
            + f"| {q.coupon.label if q.coupon else '—'} |"
        )
    best = cmp.best
    md = [
        f"# 麦麦三路比价报告 · {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"- **点单**：{cmp.item_text}",
        f"- **服务端时间**：{cmp.server_time or '—'}（{cmp.time_bucket or '—'}）",
        f"- **结论**：{_delta_line(cmp)}",
        f"- **推荐取餐方式**：{best.channel.cn}（{best.store.name if best and best.store else '—'}）"
        + (f"，挂券「{best.coupon.label}」" if best and best.coupon else ""),
        "",
        "| 渠道 | 到手价 | 商品金额 | 配送费 | 优惠 | 口径 | 门店 | 使用券 |",
        "|---|---:|---:|---:|---:|---|---|---|",
        *rows,
    ]
    if cmp.warnings:
        md += ["", "## 需要注意", *[f"- ⚠️ {w}" for w in cmp.warnings]]
    un = [i for i in cmp.items if not i.resolved]
    if un:
        md += ["", "## 未匹配商品", *[f"- {i.raw}（在门店菜单里没找到足够接近的餐品，已按诚实降级处理）"
                                      for i in un]]
    md += [
        "",
        "> 口径说明：`官方实算` = 直接采用麦当劳 MCP `calculate-price` 返回值（金额单位为分）；",
        "> `本地推算` = 官方算价失败时的单品累加基线，**不含配送费与优惠，仅供参照，不作为结论**。",
        f"> 本报告由参赛者独立开发的开源工具生成，非麦当劳官方产品；供应与价格以官方渠道实时结果为准。",
    ]
    return "\n".join(md)


def to_html(cmp: Comparison) -> str:
    quotes = sorted(cmp.quotes, key=lambda q: (q.total_cents is None, q.total_cents or 0))
    max_cents = max((q.total_cents or 0 for q in cmp.quotes if q.available), default=0) or 1
    bars = ""
    for q in quotes:
        if not q.available:
            bars += (f'<div class="row dead"><span class="ch">{_ORDER[q.channel]}</span>'
                     f'<div class="track"><div class="bar dead"></div></div>'
                     f'<span class="val">不可用 · {html.escape(q.unavailable_reason or "")}</span></div>')
            continue
        width = round((q.total_cents or 0) / max_cents * 100, 1)
        cls = "best" if q is cmp.best else ""
        extra = []
        if q.fee_cents:
            extra.append(f"含配送 {yuan(q.fee_cents)}")
        if q.discount_cents:
            extra.append(f"优惠 {yuan(q.discount_cents)}")
        bars += (f'<div class="row"><span class="ch">{_ORDER[q.channel]}</span>'
                 f'<div class="track"><div class="bar {cls}" style="width:{width}%"></div></div>'
                 f'<span class="val">{yuan(q.total_cents)} '
                 f'<em>{q.badge}{" · " + " / ".join(extra) if extra else ""}</em></span></div>')

    items = "、".join(f"{i.resolved_name or i.raw}×{i.qty}" if i.resolved else f"{i.raw}⚠️"
                      for i in cmp.items)
    warn = "".join(f"<li>{html.escape(w)}</li>" for w in cmp.warnings)
    note = "".join(f"<li>{_ORDER[q.channel]}：{html.escape(n)}</li>"
                   for q in cmp.quotes for n in q.notes)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>麦麦三路比价报告</title>
<style>
:root{{--m:#DA291C;--ink:#1a1a1a;--mut:#6b6b6b;--bg:#fff8f2}}
*{{box-sizing:border-box}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;padding:28px;
background:var(--bg);color:var(--ink)}}
h1{{margin:0 0 4px;font-size:22px}}
.sub{{color:var(--mut);font-size:13px;margin-bottom:18px}}
.card{{background:#fff;border-radius:14px;padding:20px;box-shadow:0 2px 10px rgba(0,0,0,.06);margin-bottom:16px}}
.verdict{{font-size:19px;font-weight:700;color:var(--m)}}
.row{{display:flex;align-items:center;gap:12px;margin:10px 0}}
.ch{{width:74px;font-size:14px;font-weight:600}}
.track{{flex:1;height:22px;background:#f2ece6;border-radius:11px;overflow:hidden}}
.bar{{height:100%;background:#c9a227;border-radius:11px}}
.bar.best{{background:var(--m)}}
.bar.dead{{background:#ddd;width:8%!}}
.val{{width:230px;font-variant-numeric:tabular-nums;font-size:14px;text-align:right}}
.val em{{display:block;font-style:normal;color:var(--mut);font-size:12px}}
.dead .val{{color:var(--mut);text-align:left;width:auto;font-size:12px}}
ul{{margin:6px 0;padding-left:18px;font-size:13px;color:var(--mut)}}
footer{{font-size:12px;color:var(--mut);line-height:1.7}}
code{{background:#f6f1ea;padding:1px 5px;border-radius:4px}}
</style></head><body>
<h1>麦麦三路比价报告</h1>
<div class="sub">{datetime.now():%Y-%m-%d %H:%M} · 服务端时间 {cmp.server_time or '—'}（{cmp.time_bucket or '—'}）</div>
<div class="card"><div class="verdict">{_delta_line(cmp).replace('**','')}</div>
<div style="margin-top:8px;font-size:14px;color:var(--mut)">点单：{html.escape(items)}</div>
{bars}</div>
<div class="card"><b style="font-size:14px">口径与提醒</b><ul>{warn}{note}</ul>
<div class="row"><span class="ch">明细</span></div>
<pre style="font-size:12px;overflow:auto">{html.escape(to_markdown(cmp))}</pre></div>
<footer>金额单位分→元换算；<code>官方实算</code> 取自麦当劳 MCP <code>calculate-price</code>，
<code>本地推算</code> 不含配送费与优惠，仅供参照。本工具为大赛参赛作品，由参赛者独立开发，
非麦当劳官方产品；输出仅供参考，餐品供应与价格以麦当劳官方渠道实时结果为准。</footer>
</body></html>"""


def to_json(cmp: Comparison) -> str:
    payload = {
        "item_text": cmp.item_text,
        "server_time": cmp.server_time,
        "time_bucket": cmp.time_bucket,
        "warnings": cmp.warnings,
        "best": cmp.best.channel.value if cmp.best else None,
        "quotes": [
            {
                "channel": q.channel.value,
                "store": q.store.name if q.store else None,
                "store_id": q.store.store_id if q.store else None,
                "total_yuan": None if q.total_cents is None else q.total_cents / 100,
                "goods_yuan": None if q.goods_cents is None else q.goods_cents / 100,
                "fee_yuan": None if q.fee_cents is None else q.fee_cents / 100,
                "discount_yuan": None if q.discount_cents is None else q.discount_cents / 100,
                "source": q.source,
                "coupon": q.coupon.label if q.coupon else None,
                "unavailable_reason": q.unavailable_reason,
                "notes": q.notes,
                "items": [{"name": i.resolved_name or i.raw, "qty": i.qty, "code": i.code,
                           "unit_price_yuan": None if i.price_cents is None else i.price_cents / 100}
                          for i in q.items],
            }
            for q in cmp.quotes
        ],
        "mcp_calls": cmp.trace.steps,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
