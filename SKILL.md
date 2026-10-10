---
name: mcd-coupon-desk
description: 麦当劳点餐优惠计算。当用户问「怎么点最便宜」「我这些券哪些能用」「凑单还是拆单」时使用。基于麦当劳 MCP 的真实菜单与券包，用确定性算法给出券可用性判定、四套方案对比与最优下单建议。
---

# 麦麦券务台

把麦当劳点餐的「钱」算清楚：**券能不能用 · 怎么摆最省 · 现在买还是等**。

## 何时使用

用户提出以下任一诉求时启用：

- 「我想吃 XX，怎么点最省」
- 「我这堆券哪些今天能用 / 为什么用不了」
- 「一起点划算还是分两单划算」
- 「现在买还是等活动」

## 前置条件

需要麦当劳 MCP Token（<https://open.mcd.cn/mcp>），并已配置 `mcd-mcp` 连接器。
**没有 Token 时**，仍可用离线样例演示：`python mcd-coupon-desk.py demo`。

## 工作流

1. **确认需求** —— 从用户话里提取：想吃什么、几个人、门店或地址、预算上限。
   信息不全时一次只问一个问题，不臆造参数。

2. **取数（调用麦当劳 MCP）**
   - `now-time-info` 取当前时间
   - `query-nearby-stores` 定位 `storeCode`（用户已给门店编码则跳过）
   - `query-meals` 拉门店在售菜单
   - `query-my-coupons` + `query-store-coupons` 合并出完整券包

3. **本地计算（不调用 MCP）**
   ```bash
   python mcd-coupon-desk.py plan --want "巨无霸,中薯条" --store <storeCode>
   ```
   算法可复核：券可用性五类判定、凑单背包 DP 精确解、四方案择优。

4. **官方复核** —— 用 `calculate-price` 校验最优方案金额，与本地结果比对。
   不一致时**以官方结果为准**，并如实告知用户。

5. **输出报告** —— 必须包含：
   - 四套方案对比（原价 / 单券 / 叠加 / 拆单）与最优方案
   - **每张不可用券的具体原因**（差多少钱、哪个门店、哪类商品），不允许只写「不可用」
   - 金额口径说明

6. **写操作需显式确认** —— 领券（`auto-bind-coupons`）与下单（`create-order`）
   必须获得用户明确同意后才执行，不做静默调用。

## 边界与红线

- **不臆测叠加规则**。未标组的券默认视为互斥。若用户认为自己有权叠加，
  提示可用 `--assume-stackable` 重算，并说明这会高估节省额。
- **不估算配送费**。外卖场景的配送费与起送价交由官方计价工具处理。
- **不做跨品牌比价**。只比较麦当劳自身的渠道与券组合，不与其他品牌产品对比。
- **不编造数据**。所有餐品、价格、券信息均来自官方接口；接口不可用时如实报错，
  不允许用相似名称或历史数据顶替。
- **金额全程整数分**。禁止引入浮点累加。

## 常用命令

```bash
python mcd-coupon-desk.py demo                     # 离线演示，无需 Token
python mcd-coupon-desk.py tools                    # 打印真实工具清单与入参 schema
python mcd-coupon-desk.py menu --store SH0001      # 查看菜单与券包
python mcd-coupon-desk.py diagnose --want "巨无霸,中薯条" --store SH0001
python mcd-coupon-desk.py plan --want "巨无霸,麦辣鸡腿堡,中薯条,可乐" --store SH0001
python -m unittest discover -s tests -v            # 运行测试
```
