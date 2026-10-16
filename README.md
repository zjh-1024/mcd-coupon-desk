# 麦麦券务台 · mcd-coupon-desk

[![License: MIT](https://img.shields.io/badge/License-MIT-FFC72C?style=flat-square)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-mcp.mcd.cn-DA291C?style=flat-square)](https://mcp.mcd.cn)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Dependencies](https://img.shields.io/badge/dependencies-none-3FB950?style=flat-square)](#30-秒上手不需要-token)
[![Tests](https://img.shields.io/badge/tests-53%20passing-3FB950?style=flat-square)](#运行测试)
[![Token](https://img.shields.io/badge/token-not%20required-blue?style=flat-square)](#30-秒上手不需要-token)
[![CI](https://github.com/zjh-1024/mcd-coupon-desk/actions/workflows/ci.yml/badge.svg)](https://github.com/zjh-1024/mcd-coupon-desk/actions/workflows/ci.yml)

**把麦当劳点餐里「钱」这件事，做成一道确定性计算题。**

> 券能不能用 · 怎么摆最省 · 现在买还是等

| 🎟️ 券可用性判定 | 🧮 凑单求解 | 💸 拆单收益 | 🔑 使用门槛 |
|:---:|:---:|:---:|:---:|
| 五类不可用原因 + 还差多少钱 | 0/1 背包 DP 精确解 | 样例省 ¥35.00（49.3%） | 无需 Token |

<img src="assets/demo-verdict.svg" alt="同一次点餐的四种下单方案对比：拆单最优到手 ¥36.00，原价 ¥71.00" width="100%">

麦当劳的优惠信息分散在门店菜单、账号券包、门店券包和活动日历里。用户真正卡住的
不是「有没有券」，而是三个具体问题：

1. **我手上这几张券，今天、在这家店，到底哪几张能用？** —— 剩下那些又是为什么不给用？
2. **能用的话，怎么摆最省？** —— 一张单用一张，还是拆成两单各用一张更划算？
3. **现在买是不是最便宜的时候？** —— 还是等个两天活动更值？

本项目把这三个问题全部交给**确定性算法**，输出可复核、可追溯的答案。

**不猜、不排序**：每一分钱都能被复算 —— 哪张券为什么能用 / 不能用、凑到门槛最少要加几件、拆成两单能多省多少。

> 本作品为「麦当劳程序员创意开发大赛」参赛项目，非麦当劳官方产品。
> 餐品信息、价格与优惠规则以麦当劳官方渠道的实时结果为准。

---

## 30 秒上手（不需要 Token）

```bash
git clone https://github.com/zjh-1024/mcd-coupon-desk.git
cd mcd-coupon-desk
python mcd-coupon-desk.py demo
```

只用 Python 3.10+ 标准库，**不需要 pip install，不需要申请任何密钥**。
`demo` 会读 `examples/sample_catalog.json` 里的样例门店数据，把完整流程跑一遍。

### 真实运行输出

```console
$ python mcd-coupon-desk.py demo

结论
──────────────────────────────────────────────────────────
按「拆单最优」下单，到手价 ¥36.00，比原价省 ¥35.00（49.3%）。

购物车
──────────────────────────────────────────────────────────
巨无霸 ×1 + 麦辣鸡腿堡 ×1 + 中薯条 ×1 + 可口可乐中杯 ×1
门店：上海南京东路餐厅（离线样例）

方案对比
──────────────────────────────────────────────────────────
方案          订单数        小计        节省       到手价
拆单最优           2      ¥71.00      ¥35.00       ¥36.00  ← 最优
多券叠加           1      ¥71.00      ¥30.00       ¥41.00
单券最优           1      ¥71.00      ¥25.00       ¥46.00
原价直选           1      ¥71.00       ¥0.00       ¥71.00

最优方案明细 · 拆单最优
──────────────────────────────────────────────────────────
第 1 单　中薯条 ×1 + 麦辣鸡腿堡 ×1
    用券　门店专享 满35减18（−¥18.00）
    小计 ¥36.50　节省 ¥18.00　实付 ¥18.50

第 2 单　可口可乐中杯 ×1 + 巨无霸 ×1
    用券　满30减12（−¥12.00）
    用券　饮品立减5元（−¥5.00）
    小计 ¥34.50　节省 ¥17.00　实付 ¥17.50
※ 拆成 2 笔订单，分别使用：门店专享 满35减18、满30减12、饮品立减5元

券包体检
──────────────────────────────────────────────────────────
共 7 张 · 可用 4 张 · 不可用 3 张

可用（按节省额降序）
  · 满50减25　省 ¥25.00
      门槛 ¥50.00　渠道 账号券
  · 门店专享 满35减18　省 ¥18.00
      门槛 ¥35.00　渠道 门店券
  · 满30减12　省 ¥12.00
      门槛 ¥30.00　渠道 账号券
  · 饮品立减5元　省 ¥5.00

不可用（附具体原因）
  · 满80减40　未达使用门槛
  · 早餐套餐立减6　购物车中无适用商品
  · 老券 满20减8　已过期（2026-09-30 截止）

──────────────────────────────────────────────────────────
金额口径：全部为「分」的整数运算；外卖配送费不由本地估算，
最终价格请以官方 calculate-price 与订单页为准。
```

（以上为样例数据下的演示结果，不代表任何真实售价。）

---

## 它是怎么算的

整个项目刻意**不把数值交给大模型**。模型负责听懂「我想吃巨无霸和薯条」，
数值与约束全部沉到确定性代码里，因此每一个金额都能被复算。

### 一、券可用性判定：把「不能用」拆成五类具体原因

`src/mcd_coupon_desk/coupons.py`

判定顺序按「用户改不动的原因优先」排列 —— 有效期、门店归属是先决条件，
门槛和商品匹配则可以通过加购来满足。先报不可改变的原因，用户才不会白折腾。

| 原因码 | 含义 | 用户能做什么 |
|---|---|---|
| `NOT_STARTED` | 尚未开始 | 等它生效 |
| `EXPIRED` | 已过期 | 无 |
| `WRONG_STORE` | 本店不可用 | 换门店 |
| `THRESHOLD_NOT_MET` | 未达门槛（**并给出还差多少钱**） | 按差额加购 |
| `ITEM_MISMATCH` | 无适用商品 | 加对应品类 |

### 二、凑单是精确解，不是贪心近似

`solver.minimal_subset_reaching()`

「为了用上满 35 减 18，该加哪几件最省？」本质是**最小子集和 ≥ 门槛**问题。
本项目用一次 0/1 背包 DP 求精确解 —— 差别在于能证明「这就是最省的凑单方式」，
而不是给一个看起来差不多的凑法。

### 三、拆单：为什么能比合并下单更省

券的门槛是**按单**判定的，同一张券在一笔订单里只能用一次。
把商品分成两笔，两张「满减券」就都能生效。

「先给哪张券分配商品」属于集合划分问题（指数级），本项目不做完整枚举，
而是换三种排序各跑一遍取最优，兜底而非唯一起点：

| 排序策略 | 直觉 |
|---|---|
| 减免额降序 | 优先吃大券 |
| 减免率降序（减免 ÷ 门槛） | 优先留出余量给后续小券 |
| 门槛升序 | 优先满足容易达成的券 |

「选哪几件去凑这张券」这一子问题仍由上面的 DP 精确求解。

### 四、叠加：宁可少算，不可虚报

券的 `group` 字段表示互斥组。**未标组的券默认视为互斥**，因为高估节省额比低估更糟 ——
用户会按错的方案去下单。确实允许叠加时用 `--assume-stackable` 放宽。

### 五、金额全程整数

所有金额以「分」的整数存储与运算。浮点在比价场景会产生 `0.1 + 0.2` 这类误差，
足以算错最优解。`tests/test_solver.py::TestMoneyUnits` 专门守这条线。

---

## 接入麦当劳 MCP

申请 Token：<https://open.mcd.cn/mcp> → 手机号登录 → 控制台 → 激活 → 复制

```bash
export MCD_MCP_TOKEN="你的Token"      # 不要写进任何文件

python mcd-coupon-desk.py --store 1410419 menu               # 查看真实菜单与券包
python mcd-coupon-desk.py --store 1410419 diagnose --want "巨无霸,薯条,可乐"
python mcd-coupon-desk.py --store 1410419 plan --want "巨无霸,薯条,可乐"
python mcd-coupon-desk.py tools                              # 打印真实工具清单与入参
```

也可以用城市搜索门店（实测 `city` 与 `keyword` 必须同时提供）：

```bash
python mcd-coupon-desk.py --city 南昌 --keyword 南昌 menu
```

> **实测状态**：菜单、门店、价格、优惠券四条链路均已用真实接口验证通过 ——
> 2026-10-10 对服务端 `mcd-mcp v1.0.0` 实测：南昌门店 `1410419` 返回 110 项真实餐品；
> 真实券「9.9元中杯冰美式」作用于真实商品「麦咖啡™美式 ¥11.90」，
> 算出到手 **¥9.90**，与券面价吻合。已知边界（如券面价只能从券名解析）见
> [MCP_INTEGRATION.md](MCP_INTEGRATION.md) 第六节。

在 WorkBuddy / Cherry Studio / Cursor 等 MCP 客户端里，把 `mcp-config.example.json`
的内容粘进配置，替换 `${MCD_MCP_TOKEN}` 为真实 Token 即可。详见 [MCP_INTEGRATION.md](MCP_INTEGRATION.md)。

---

## 目标用户

- **学生 / 通勤族**：预算固定，希望每一顿都点到最优解，不想在菜单前反复比价
- **券包囤积者**：账号里躺着十几张券，但说不清哪些今天能用、哪些快过期
- **多人拼单的发起者**：需要判断「一起点」还是「分两单」更划算
- **营养与省钱双重约束的人**：已有项目处理热量约束，本项目负责把成本压到最低

---

## 项目结构

```
mcd-coupon-desk/
├── mcd-coupon-desk.py          零安装启动器
├── mcp-config.example.json     脱敏的 MCP 配置示例（仅环境变量占位符）
├── SKILL.md                    Agent 技能定义
├── MCP_INTEGRATION.md          MCP 接入说明：用了哪些 Tool、怎么串、业务价值
├── CONTEST_DECLARATION.md      参赛声明（官方原文，未作任何修改）
├── workbuddy.md                开发过程记录
├── README_EN.md                英文版说明
├── CHANGELOG.md                变更记录
├── .github/workflows/ci.yml    GitHub Actions CI（Python 3.10-3.13 矩阵）
├── src/mcd_coupon_desk/
│   ├── models.py               领域模型（金额 = 整数分）
│   ├── coupons.py              券可用性判定
│   ├── solver.py               四类方案求解（含精确凑单 DP）
│   ├── catalog.py              数据源适配（离线样例 / 麦当劳 MCP）
│   ├── textparse.py            解析 MCP 返回的「说明文字 + 内嵌 JSON」
│   ├── mcp_client.py           零依赖 Streamable HTTP MCP 客户端
│   ├── report.py               结果渲染
│   └── cli.py                  命令行入口
├── tests/
│   ├── test_solver.py           核心算法测试
│   ├── test_coupon_mapping.py   券名与券面解析测试
│   └── test_textparse.py        返回文本解析测试
├── assets/
│   ├── demo-verdict.svg         演示结果图（README 首屏）
│   └── demo-verdict.png         同上，位图版（掘金文章配图）
└── examples/sample_catalog.json 离线样例数据
```

---

## 运行测试

```bash
python -m unittest discover -s tests -v
```

**53 项测试**，覆盖金额单位无损换算、凑单精确性、五类券不可用原因、四套方案的正确性、
「未标组默认互斥」的保守假设、MCP 返回文本的解析（含说明文字里的花括号陷阱与空结果识别）、
以及「固定价券」的省额计算与券名解析，以及拆单可行性（每张券必须落在真正能用它的订单上，
并由暴力枚举 oracle 对拍兜底）。

---

## 安全与合规

- 仓库内**不含任何真实 Token、密钥或账号凭证**；`mcp-config.example.json` 仅使用
  环境变量占位符 `${MCD_MCP_TOKEN}`。
- 涉及**写操作**（一键领券、下单）一律需要用户显式确认后才调用，不做静默执行。
- 本项目**全程只读取官方接口数据**做本地计算，不转发、不上传、不缓存用户数据到任何服务端。
- 金额口径：本地计算为估算与建议，最终价格以官方 `calculate-price` 与订单页为准；
  外卖配送费不由本地估算。

---

## 许可

MIT，见 [LICENSE](LICENSE)。

> 如果这个思路对你有用，点个 **Star** 支持一下 —— 本项目正在参加麦当劳程序员创意开发大赛，
> Star 数决定排名。
