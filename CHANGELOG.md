# Changelog

本项目的所有重要变更都记录在此文件。
格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-10-16

### 新增
- 券可用性五类判定（NOT_STARTED / EXPIRED / WRONG_STORE / THRESHOLD_NOT_MET / ITEM_MISMATCH），
  并在未达门槛时给出「还差多少钱」。
- 凑单求解：0/1 背包 DP 求「最小子集和 >= 门槛」的精确解。
- 四套方案对比与择优：原价直选 / 单券最优 / 多券叠加 / 拆单最优。
- 拆单采用多排序启发式（减免额、减免率、门槛升序）各跑一遍取最优。
- 零依赖 MCP 客户端（urllib + JSON-RPC 2.0 + Streamable HTTP + SSE）。
- 运行时 schema 发现：tools/list 取 inputSchema，不硬编码上游字段名。
- 括号配平扫描，从「说明文字 + 内嵌 JSON」的返回里提取原始响应。
- 金额全程整数「分」，杜绝浮点误差。
- 「固定价券」判定与省额现算（如「9.9 元中杯冰美式」）。
- 离线样例数据 + 49 项单元测试（仅用标准库 unittest）。
- 命令行子命令：demo / tools / menu / diagnose / plan。

### 说明
- 未标组的券默认视为互斥（宁可少算，不虚报节省额）；可用 --assume-stackable 放宽。
- 写操作（领券 / 下单）一律需要用户显式确认。

[0.1.0]: https://github.com/zjh-1024/mcd-coupon-desk/releases/tag/v0.1.0
