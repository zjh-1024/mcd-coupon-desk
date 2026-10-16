"""命令行入口。

运行模式
--------
  gui       打开图形界面（可自由输入想吃的餐品，无需 Token）
  demo      用内置样例数据在终端跑通全流程（无需 Token、无需联网）
  me        登录麦当劳账号，查看真实积分 / 券 / 推荐（只读，需 Token）
  tools     连接麦当劳 MCP，打印真实工具清单与入参 schema
  diagnose  对当前购物车做券包体检
  plan      求解最省方案
  menu      查看菜单与券包

设计取向：**默认离线可跑**。任何人 clone 下来执行 `demo` 或 `gui` 就能看到
完整效果，不必先申请 Token —— 这既是评审友好，也是这个项目能被复现的前提。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .advisor import advise
from .catalog import McpSource, OfflineSource
from .mcp_client import McpAuthError, McpClient, McpError
from .models import SCOPE_STORE, Catalog, Coupon, Line
from .report import (
    pad,
    render_account,
    render_advice,
    render_solution,
    render_verdicts,
)
from .solver import solve

DEFAULT_SAMPLE = Path(__file__).resolve().parents[2] / "examples" / "sample_catalog.json"


# ---------------------------------------------------------------- 购物车解析


def build_cart(catalog: Catalog, want: str) -> tuple[list[Line], list[str]]:
    """把「巨无霸,中薯条,可乐」这类需求解析成购物车。

    支持 `名称×数量` 写法，例如 `巨无霸x2,可乐`。匹配不上的关键词会被原样返回，
    由调用方决定是报错还是提示 —— 不静默丢弃用户要点的东西。
    """
    lines: list[Line] = []
    missing: list[str] = []

    for token in [t.strip() for t in want.replace("，", ",").split(",") if t.strip()]:
        qty = 1
        name = token
        for sep in ("×", "x", "X", "*"):
            if sep in token:
                head, _, tail = token.partition(sep)
                if tail.strip().isdigit():
                    name, qty = head.strip(), int(tail.strip())
                break

        hits = catalog.search(name)
        if not hits:
            missing.append(token)
            continue
        lines.append(Line(hits[0], qty))

    return lines, missing


def _print_catalog(catalog: Catalog) -> None:
    print(f"菜单（{len(catalog.items)} 项）")
    for it in catalog.items[:300]:
        print(f"  {pad(it.code, 12)} {pad(it.name, 26)} {pad(it.price_yuan, 9, 'right')}  {it.category}")


def _print_coupons(coupons: list[Coupon]) -> None:
    print(f"券包（{len(coupons)} 张）")
    for c in coupons:
        scope = "门店券" if c.scope == SCOPE_STORE else "账号券"
        print(f"  {pad(c.name, 24)} {pad(c.mode_text, 22)} {pad(scope, 8)} 有效期至 {c.valid_to or '不限'}")


# ---------------------------------------------------------------- 数据源装配


def load_source(args) -> tuple[object, str]:
    """返回 (数据源, 门店标签)。数据源实例上都会带 `store_code`。"""
    if args.offline or getattr(args, "force_offline", False):
        path = Path(args.catalog) if args.catalog else DEFAULT_SAMPLE
        if not path.exists():
            raise SystemExit(f"找不到样例数据：{path}")
        source = OfflineSource(path)
        return source, f"{source.menu().store_name}（离线样例）"

    client = McpClient(endpoint=args.endpoint, token=args.token)
    client.initialize()
    source = McpSource(client, order_type=args.order_type, be_type=args.be_type)

    store_code = args.store or ""
    if store_code:
        source.store_code = store_code
    else:
        city = args.city
        if not city:
            raise SystemExit(
                "在线模式需要指定门店：用 --store <门店编码>，"
                "或用 --city <城市> 搜索门店（实测 city 与 keyword 必须同时提供）"
            )
        store_code = source.find_store_code(city, args.keyword or city)

    for store in source.stores:
        if str(store.get("storeCode", "")) == store_code:
            return source, f"{store.get('storeName', '')}（{store_code}）"
    return source, f"门店 {store_code}"


# ---------------------------------------------------------------- 子命令


def _run_plan(args) -> int:
    """`demo` 与 `plan` 共用：拉数据 → 出方案。

    两者的唯一差别是数据源（demo 强制离线），因此共用同一条代码路径，
    避免出现「离线能跑、上线就崩」的分叉。
    """
    source, label = load_source(args)
    catalog = source.menu(source.store_code or None)
    lines, missing = build_cart(catalog, args.want)
    if missing:
        print(f"菜单里没有：{'、'.join(missing)}（已跳过）", file=sys.stderr)
    if not lines:
        print("购物车为空，请用 --want 指定想吃的餐品。", file=sys.stderr)
        return 2

    coupons = source.coupons()
    skipped = getattr(source, "skipped_account_coupons", 0)
    if skipped:
        print(
            f"注：另有 {skipped} 张账号券缺少「适用商品」信息，未纳入计算"
            f"（宁可少算，不虚报节省额）。",
            file=sys.stderr,
        )
    solution = solve(
        lines, coupons, source.store_code or "", assume_stackable=args.assume_stackable
    )
    print(render_solution(solution, label, lines))
    return 0


def cmd_demo(args) -> int:
    return _run_plan(args)


def cmd_gui(args) -> int:
    """打开图形界面。

    图形界面用的是 Python 标准库自带的 tkinter，因此不需要任何额外安装。
    这里把 tkinter 的导入**放在函数内部**，有两个好处：
      1. 没装 tkinter 的环境（少数精简版 Linux 发行版）只会在用到 gui 时报错，
         不会拖累 demo / plan 这些纯命令行路径；
      2. 报错信息可以翻译成「请改用命令行」的明确指引，而不是一句 ImportError。
    """
    try:
        from .gui import launch
    except Exception as exc:  # noqa: BLE001 - 包含 ImportError 与显示环境缺失
        print(f"无法启动图形界面：{exc}", file=sys.stderr)
        print("请改用命令行：python mcd-coupon-desk.py demo", file=sys.stderr)
        print("（图形界面依赖 Python 自带的 tkinter，官方安装包默认已包含）", file=sys.stderr)
        return 5

    sample = Path(args.catalog) if args.catalog else DEFAULT_SAMPLE
    if not sample.exists():
        print(f"找不到样例数据：{sample}", file=sys.stderr)
        return 2
    token = args.token or os.environ.get("MCD_MCP_TOKEN", "")
    return launch(sample, token=token, endpoint=args.endpoint)


def cmd_me(args) -> int:
    """登录麦当劳账号，打印真实账号信息与推荐（全程只读）。

    关于「登录」：麦当劳没有向第三方开放账号密码登录，官方唯一凭据是
    MCP Token（<https://open.mcd.cn/mcp> 申请，天然绑定到你的账号）。
    因此「登录」= 用 Token 握手成功并能取回本账号数据，**不存在也不需要密码**。

    为什么只读：这里只调用 query-my-account / query-my-coupons /
    mall-points-products 三个查询类工具。领券、下单等写操作一律不在此路径中，
    避免用户在看信息的时候被顺手改了账号。
    """
    client = McpClient(endpoint=args.endpoint, token=args.token)
    client.initialize()
    source = McpSource(client, order_type=args.order_type, be_type=args.be_type)

    store_code = args.store or ""
    if not store_code and args.city:
        store_code = source.find_store_code(args.city, args.keyword or args.city)
    if store_code:
        source.store_code = store_code

    account = source.my_account()
    coupons = source.my_coupons()

    products: list = []
    try:
        products = source.points_products()
    except McpError as exc:
        # 积分商城取不到不该让整条命令失败 —— 账号与券的信息仍然有价值
        print(f"注：积分商城数据暂时取不到（{exc}），本次不给积分兑换建议。", file=sys.stderr)

    print(render_account(account, coupons))
    print()
    print(render_advice(advise(account, coupons, products)))
    return 0


def cmd_tools(args) -> int:
    client = McpClient(endpoint=args.endpoint, token=args.token)
    info = client.initialize()
    specs = client.list_tools(refresh=True)

    if getattr(args, "json", False) or getattr(args, "out", ""):
        # 机器可读输出：便于把真实 schema 落盘留档，收敛字段映射
        payload = {
            "serverInfo": info,
            "tools": [
                {"name": s.name, "description": s.description, "inputSchema": s.input_schema}
                for s in specs
            ],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        out_path = getattr(args, "out", "")
        if out_path:
            target = Path(out_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            print(f"已写入 {target}（{len(specs)} 个工具）")
            return 0
        print(text)
        return 0

    server = (info.get("serverInfo") or {}).get("name", "未知服务端")
    print(f"已连接：{server}")
    print(f"协议版本：{info.get('protocolVersion', '未知')}")
    print()
    for spec in specs:
        print(f"● {spec.name}")
        if spec.description:
            print(f"    {spec.description}")
        if spec.properties:
            for key, meta in spec.properties.items():
                required = "必填" if key in spec.required else "选填"
                kind = (meta or {}).get("type", "any")
                print(f"    - {key} ({kind}, {required})")
        print()
    return 0


def cmd_diagnose(args) -> int:
    source, label = load_source(args)
    catalog = source.menu(source.store_code or None)
    lines, missing = build_cart(catalog, args.want)
    if missing:
        print(f"菜单里没有：{'、'.join(missing)}", file=sys.stderr)
    if not lines:
        print("购物车为空，请用 --want 指定想吃的餐品。", file=sys.stderr)
        return 2

    coupons = source.coupons()
    solution = solve(lines, coupons, source.store_code, assume_stackable=args.assume_stackable)
    print(f"门店：{label}")
    print()
    print(render_verdicts(solution.verdicts))
    return 0


def cmd_plan(args) -> int:
    return _run_plan(args)


def cmd_menu(args) -> int:
    source, label = load_source(args)
    catalog = source.menu(source.store_code or None)
    print(f"门店：{label}")
    print()
    _print_catalog(catalog)
    coupons = source.coupons()
    print()
    if coupons:
        _print_coupons(coupons)
    else:
        print("券包为空：账号当前没有可用券。可先用 auto-bind-coupons 领取麦麦省券（写操作，需确认）。")
    skipped = getattr(source, "skipped_account_coupons", 0)
    if skipped:
        print(f"另有 {skipped} 张账号券缺少「适用商品」信息，仅展示不参与计算。")
    return 0


# ---------------------------------------------------------------- 参数解析


def _build_common(suppress: bool) -> argparse.ArgumentParser:
    """公共选项容器。

    suppress=True 的副本挂在各子命令上，默认值设为 SUPPRESS ——
    这样写成 `cmd --store X` 与 `--store X cmd` 都能生效，
    子命令上的副本不会用默认值覆盖前面已经解析出的结果。
    """
    keep = argparse.SUPPRESS
    common = argparse.ArgumentParser(add_help=False)

    common.add_argument("--offline", action="store_true",
                        default=keep if suppress else False,
                        help="使用本地样例数据，不需要 Token（demo 默认开启）")
    common.add_argument("--catalog", default=keep if suppress else "", help="离线样例 JSON 路径")
    common.add_argument("--endpoint", default=keep if suppress else "https://mcp.mcd.cn",
                        help="MCP 服务地址")
    common.add_argument("--token", default=keep if suppress else None,
                        help="MCP Token（默认读环境变量 MCD_MCP_TOKEN）")
    common.add_argument("--store", default=keep if suppress else "",
                        help="门店编码（7 位纯数字，如 1410419）")
    common.add_argument("--city", default=keep if suppress else "",
                        help="搜索门店用的城市名（须与 --keyword 同时提供）")
    common.add_argument("--keyword", default=keep if suppress else "",
                        help="搜索门店用的关键词（默认与 --city 相同）")
    common.add_argument("--order-type", type=int, default=keep if suppress else 1, choices=(1, 2),
                        help="1-到店（含自取/得来速）  2-外送（含麦乐送/团餐）")
    common.add_argument("--be-type", type=int, default=keep if suppress else 1, choices=(1, 2, 5, 6),
                        help="1-到店自取  2-麦乐送到家  5-得来速  6-企业团餐")
    common.add_argument("--assume-stackable", action="store_true",
                        default=keep if suppress else False,
                        help="假设未标组的券可以互相叠加（默认按互斥处理，更保守）")
    return common


def build_parser() -> argparse.ArgumentParser:
    top_common = _build_common(suppress=False)
    sub_common = _build_common(suppress=True)

    parser = argparse.ArgumentParser(
        prog="mcd-coupon-desk",
        description="麦麦券务台 —— 券能不能用 · 怎么摆最省 · 现在买还是等",
        parents=[top_common],
    )
    sub = parser.add_subparsers(dest="command")

    p_gui = sub.add_parser("gui", parents=[sub_common],
                           help="打开图形界面（可自由输入餐品，无需 Token）")
    p_gui.set_defaults(func=cmd_gui, force_offline=True)

    p_demo = sub.add_parser("demo", parents=[sub_common], help="用样例数据跑通全流程（无需 Token）")
    p_demo.add_argument("--want", default="巨无霸,麦辣鸡腿堡,中薯条,可口可乐", help="想吃的餐品，逗号分隔")
    # 注意：这里**不能**写 offline=True。
    # argparse 的 parents 会在多个子命令之间共享同一个 Action 对象，
    # 而 set_defaults 会直接改写该对象的 default，导致所有子命令都变成离线模式。
    p_demo.set_defaults(func=cmd_demo, force_offline=True)

    p_tools = sub.add_parser("tools", parents=[sub_common], help="打印麦当劳 MCP 的真实工具清单与入参")
    p_tools.add_argument("--json", action="store_true", help="以 JSON 输出原始 schema，便于落盘留档")
    p_tools.add_argument("--out", default="", help="把 schema 直接写入指定文件（UTF-8），避免终端编码问题")
    p_tools.set_defaults(func=cmd_tools)

    p_me = sub.add_parser("me", parents=[sub_common],
                          help="登录麦当劳账号，查看真实积分 / 券 / 推荐（只读，需 Token）")
    p_me.set_defaults(func=cmd_me)

    p_diag = sub.add_parser("diagnose", parents=[sub_common], help="券包体检：哪些能用、哪些为什么不能用")
    p_diag.add_argument("--want", default="巨无霸,麦辣鸡腿堡,中薯条,可口可乐")
    p_diag.set_defaults(func=cmd_diagnose)

    p_plan = sub.add_parser("plan", parents=[sub_common], help="求解最省方案")
    p_plan.add_argument("--want", default="巨无霸,麦辣鸡腿堡,中薯条,可口可乐")
    p_plan.set_defaults(func=cmd_plan)

    p_menu = sub.add_parser("menu", parents=[sub_common], help="查看菜单与券包")
    p_menu.set_defaults(func=cmd_menu)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except McpAuthError as exc:
        print(f"鉴权失败：{exc}", file=sys.stderr)
        return 3
    except McpError as exc:
        print(f"调用失败：{exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
