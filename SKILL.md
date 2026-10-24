---
name: mcd-three-path-detective
description: 麦麦三路比价侦探——用户给出点单内容后，基于麦当劳官方 MCP 分别计算「到店自取 / 麦乐送 / 得来速」三条取餐路径的真实到手价（含配送费与可用券），输出最优路径与差额。当用户问"这份餐在外面买还是外送更划算""哪个渠道更便宜""到店和麦乐送价格一样吗""帮我用掉能用的券点最省的"时使用。只读查询与价格试算，不领券、不下单、不兑换积分。
---

# 麦麦三路比价侦探（Skill）

一个把「在哪买更划算」变成一条命令的 Skill：同一份点单，三条取餐路径，逐路官方实算。

## 触发条件

用户表达里出现以下任一意图即启用：

- 渠道选择：「到店拿还是点麦乐送」「得来速会不会更贵」「自取有优惠吗」
- 到手价核算：「我这单实际要付多少」「含配送费一共多少钱」「用哪张券最省」
- 渠道差异好奇：「为什么外送比店里贵」「同一份餐三个渠道价格一样吗」

不适用：纯粹问热量/营养（交给营养类工具）、要求直接下单支付（本 Skill 明确不做写操作）。

## 前置条件

1. MCP Client 已按 [`mcp-config.example.json`](mcp-config.example.json) 接入麦当劳 MCP：
   `https://mcp.mcd.cn` + `Authorization: Bearer ${MCD_MCP_TOKEN}`。
2. Token 只存在于环境变量，任何输出与日志都不得回显它。

## 需要的输入

| 槽位 | 缺失时怎么办 |
|---|---|
| 点单内容（餐品名 + 数量） | 必须问用户，不可自行编造餐品 |
| 城市 | 用户没说就问；不要从 IP/历史推测 |
| 地址或门店关键词 | 必须问（到店与外送定位不同门店） |
| 是否限定渠道 | 默认三路全开 |

## 工具编排（必须按此顺序，只读）

```
now-time-info                    → 餐段判定（早餐/午餐/下午茶/晚餐/夜宵）
query-nearby-stores(city,keyword)→ 到店自取 + 得来速候选门店（识别得来速标记）
delivery-query-addresses         → 可配送地址
delivery-query-stores            → 外送可配送门店
query-meals(storeId)             → 各门店实时菜单，解析餐品编码与单点价
query-my-coupons                 → 已持有券
query-store-coupons(storeId)     → 本店可用券
available-coupons                → 可领券数量（仅提示，不领取）
calculate-price × (渠道 × 券)     → 官方实算 goodsAmount/deliveryFee/discountAmount/totalPay
```

## 硬性约束（违反即输出不可信）

1. **金额单位是分**：`11450` 表示 ¥114.50，全程整数运算，展示层才换算。
2. **券必须 `couponId` + `couponCode` 成对**：缺一会被拒（`600010`）；信息不全的券直接跳过并说明。
3. **成对也可能不可用**（`600012`，时段/门店/SKU 白名单）：回退到该渠道无券价并写明原因，
   **禁止**用其他渠道的价格顶替，也禁止用估算价冒充实算价。
4. **`city` 与 `keyword` 必须同传**给 `query-nearby-stores`，否则返回空结果。
5. **不调用任何有副作用的工具**：`auto-bind-coupons`、`create-order`、`cancel-order`、
   `mall-create-order`、`party-order-create`、`draw-lottery`、`delivery-create-address` 一律禁止。
6. 控制请求量：单次比价不超过 ~30 次调用（官方限流 600 次/分钟），试挂券数量默认上限 6 张。

## 输出契约

先给结论，再给依据：

```
最优：<渠道> ¥<到手价>（<门店>）｜比最贵的 <渠道> 省 ¥<差额>（<百分比>）

| 渠道 | 到手价 | 商品 | 配送费 | 优惠 | 口径 | 使用券 |
|---|---:|---:|---:|---:|---|---|

不可用/需注意：<逐条列出真实原因>
```

- 「口径」只能取 **官方实算**（来自 `calculate-price`）或 **本地推算**（官方算价失败时的单品累加，
  必须显式声明不含配送费与优惠、不作为结论）。
- 结尾固定一句免责声明：本工具由开发者独立开发，非麦当劳官方产品；价格与供应以官方渠道实时结果为准。

## 直接用命令行跑

```bash
export MCD_MCP_TOKEN=你的Token
python -m mcd_three_path compare --city 上海 --keyword 南京西路 \
  --items "巨无霸 + 2份中薯条 + 可乐(中)" --html report.html --trace
```

没有 Token 时可用仓库自带的**演示录制**（`meta.source = "demo"`，非官方实时价）离线复现形态：

```bash
python -m mcd_three_path --replay fixtures/demo-cassette.json compare \
  --city 上海 --keyword 南京西路 --items "巨无霸 + 中薯条"
```
