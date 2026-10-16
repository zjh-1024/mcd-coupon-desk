"""图形界面（tkinter，Python 标准库自带）。

为什么是 tkinter
----------------
本项目对外的硬约束是「零第三方依赖」：clone 下来就能跑，不需要 `pip install`。
图形界面同理 —— tkinter 随 CPython 官方安装包（Windows / macOS）一起分发，
装好 Python 就有，既不引入新依赖，也不需要额外打包工具。

界面结构
--------
  [数据源栏]  离线样例 / 在线 MCP（Token、下单场景、门店）
  [左]  菜单：可搜索、双击即加入购物车
  [右]  购物车：改数量、移除、清空
  [中]  快速添加：直接敲「巨无霸x2,中薯条」这类文本
  [下]  结果区四个页签：方案对比 / 最优明细 / 券包体检 / 完整文本报告

两个刻意的设计
--------------
1. **界面不复制任何业务逻辑**，全部调用 `solver` / `report` / `catalog`
   这几个已在 CLI 与单元测试里验证过的模块 —— 图形界面只是换了个壳，
   因此 CLI 与 GUI 不可能算出两个不同的答案。
2. **保留「完整文本报告」页签**，把 CLI 的原始输出一字不改地摆出来，
   界面上的每个数字都能被追溯，避免图形界面变成新的黑箱。

线程约定
--------
Tk 不是线程安全的：**所有控件操作都必须回到主线程**。
在线加载菜单 / 券包这类会阻塞的操作放在后台线程，但后台线程**不直接碰 Tk**，
而是把结果塞进队列；主线程用一个 80ms 的定时轮询把结果取出来渲染。
（不能在后台线程里调 `root.after` —— 那会抛「main thread is not in main loop」，
因为 `after` 内部需要向 Tcl 注册命令。）
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from .advisor import advise
from .catalog import (
    BE_TYPE_DELIVERY,
    BE_TYPE_DRIVE_THRU,
    BE_TYPE_GROUP_MEAL,
    BE_TYPE_STORE,
    ORDER_TYPE_DELIVERY,
    ORDER_TYPE_STORE,
    McpSource,
    OfflineSource,
)
from .cli import build_cart
from .mcp_client import DEFAULT_ENDPOINT, McpClient, McpError
from .models import (
    SCOPE_STORE,
    Catalog,
    Coupon,
    Item,
    Line,
    PointsAccount,
    RewardProduct,
)
from .report import render_plan_detail, render_solution, render_summary, yuan
from .solver import Solution, solve

# ---------------------------------------------------------------- 品牌配色
# 与 README / 文档里的视觉保持一致（麦当劳红 + 黄）。
RED = "#DA291C"
YELLOW = "#FFC72C"
INK = "#16181D"
MUTED = "#6B7280"
BG = "#F5F5F7"
CARD = "#FFFFFF"
LINE = "#E6E6E9"
GREEN = "#0F9D58"

# 下单场景：展示名 → (orderType, beType)。枚举值取自服务端 inputSchema（实测）。
SCENARIOS: dict[str, tuple[int, int]] = {
    "到店自取": (ORDER_TYPE_STORE, BE_TYPE_STORE),
    "得来速": (ORDER_TYPE_STORE, BE_TYPE_DRIVE_THRU),
    "麦乐送": (ORDER_TYPE_DELIVERY, BE_TYPE_DELIVERY),
    "企业团餐": (ORDER_TYPE_DELIVERY, BE_TYPE_GROUP_MEAL),
}

# 一个「满配」的示例购物车，用于一键演示（对应 README 首屏那组数）。
EXAMPLE_WANT = "巨无霸,麦辣鸡腿堡,中薯条,可口可乐"


def _enable_dpi_awareness() -> None:
    """Windows 高分屏下让界面不糊。

    Tk 默认不是 DPI 感知的，在 125%/150% 缩放的机器上会被系统拉伸成模糊的位图。
    这里显式声明感知等级；非 Windows 平台静默跳过。
    """
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - 纯美化项，失败不影响功能
        pass


def _pick_family(root: tk.Misc) -> str:
    """挑一个系统里存在的中文字体，避免出现方块字。"""
    families = set(tkfont.families(root))
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC",
                 "Noto Sans CJK SC", "Source Han Sans SC", "Segoe UI"):
        if name in families:
            return name
    return "TkDefaultFont"


class CouponDeskApp:
    """麦麦券务台图形界面。"""

    def __init__(
        self,
        root: tk.Tk,
        sample_path: Path,
        token: str = "",
        endpoint: str = DEFAULT_ENDPOINT,
    ) -> None:
        self.root = root
        self.sample_path = Path(sample_path)
        self.endpoint = endpoint

        self.source: OfflineSource | McpSource | None = None
        self.catalog: Catalog | None = None
        self.coupons: list[Coupon] = []
        self.store_label = ""
        self.cart: list[Line] = []
        self.solution: Solution | None = None
        self.account: PointsAccount | None = None
        self.my_coupons: list[Coupon] = []
        self.rewards: list[RewardProduct] = []

        # 「我的账号」独立窗口的控件引用（窗口关闭后置回 None）
        self.account_win: tk.Toplevel | None = None
        self.account_book: ttk.Notebook | None = None
        self.account_login_btn: ttk.Button | None = None
        self.advice_tree: ttk.Treeview | None = None
        self.mine_tree: ttk.Treeview | None = None
        self.mall_tree: ttk.Treeview | None = None

        self._busy = False
        self._results: queue.Queue = queue.Queue()

        self._family = _pick_family(root)
        self._build_style()
        self._build_ui(token)
        self._load_offline()          # 默认离线：无需 Token 即可看到完整效果
        self.root.after(80, self._poll_results)

    # ------------------------------------------------------------ 样式

    def _build_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")   # clam 才允许自定义大部分颜色
        except tk.TclError:
            pass

        base = (self._family, 10)
        style.configure(".", font=base, background=BG, foreground=INK)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD, relief="flat")
        style.configure("TLabel", background=BG, foreground=INK)
        style.configure("Hint.TLabel", background=BG, foreground=MUTED,
                        font=(self._family, 9))
        style.configure("H2.TLabel", background=BG, foreground=INK,
                        font=(self._family, 11, "bold"))

        style.configure("TLabelframe", background=BG, bordercolor=LINE,
                        relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=BG, foreground=INK,
                        font=(self._family, 10, "bold"))

        style.configure("TButton", font=base, padding=(10, 5))
        style.configure("Accent.TButton", font=(self._family, 11, "bold"),
                        padding=(16, 8), foreground="#FFFFFF", background=RED,
                        borderwidth=0, focuscolor=RED)
        style.map("Accent.TButton",
                  background=[("active", "#B71C12"), ("disabled", "#E9A6A1")],
                  foreground=[("disabled", "#FFFFFF")])
        # 登录按钮：品牌红但尺寸收一号，避免与主操作按钮抢视觉重心
        style.configure("Brand.TButton", font=(self._family, 10, "bold"),
                        padding=(12, 5), foreground="#FFFFFF", background=RED,
                        borderwidth=0, focuscolor=RED)
        style.map("Brand.TButton",
                  background=[("active", "#B71C12"), ("disabled", "#E9A6A1")],
                  foreground=[("disabled", "#FFFFFF")])
        style.configure("TEntry", padding=4)
        style.configure("TCombobox", padding=3)

        style.configure("Treeview", font=base, rowheight=26,
                        background=CARD, fieldbackground=CARD,
                        bordercolor=LINE, borderwidth=1)
        style.configure("Treeview.Heading", font=(self._family, 10, "bold"),
                        background="#EFEFF2", foreground=INK, padding=(6, 5))
        style.map("Treeview.Heading", background=[("active", "#E4E4E9")])
        style.map("Treeview", background=[("selected", "#FBE3E1")],
                  foreground=[("selected", INK)])

        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", font=(self._family, 10), padding=(16, 7))
        style.map("TNotebook.Tab",
                  background=[("selected", CARD)],
                  foreground=[("selected", RED)])

        style.configure("TProgressbar", background=YELLOW, troughcolor=LINE,
                        borderwidth=0, thickness=6)

    # ------------------------------------------------------------ 界面骨架

    def _build_ui(self, token: str) -> None:
        self.root.title("麦麦券务台 · 券能不能用 · 怎么摆最省")
        self.root.configure(bg=BG)

        # 按屏幕尺寸自适应：默认尽量占满可用高度，把纵向空间留给结果区
        # （菜单/购物车区是固定高度，结果区才是需要滚动阅读的地方）。
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = max(1060, min(1400, screen_w - 160))
        height = max(800, min(1010, screen_h - 90))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(1040, 760)
        # 上方「菜单 + 购物车」占约 1/4 高，其余全部让给下面的页签
        self._body_height = max(220, int(height * 0.24))

        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        self._build_header(outer, token)
        self._build_body(outer)
        self._build_actions(outer)
        self._build_results(outer)
        self._build_statusbar(outer)

        self.root.bind("<F5>", lambda _e: self.compute())

    def _build_header(self, parent: ttk.Frame, token: str) -> None:
        hero = tk.Frame(parent, bg=RED)
        hero.pack(fill="x")

        left = tk.Frame(hero, bg=RED)
        left.pack(side="left", padx=22, pady=16)
        tk.Label(left, text="麦麦券务台", bg=RED, fg="#FFFFFF",
                 font=(self._family, 19, "bold")).pack(anchor="w")
        tk.Label(left, text="券能不能用 · 怎么摆最省 —— 把钱这件事做成一道确定性计算题",
                 bg=RED, fg="#FFE9E6", font=(self._family, 10)).pack(anchor="w", pady=(2, 0))

        right = tk.Frame(hero, bg=RED)
        right.pack(side="right", padx=22, pady=16)
        for text in ("零第三方依赖", "金额全程整数（分）"):
            chip = tk.Label(right, text=text, bg=YELLOW, fg="#4A3200",
                            font=(self._family, 9, "bold"), padx=10, pady=3)
            chip.pack(side="left", padx=(6, 0))

        # ---- 数据源栏 ----
        # 刻意拆成两行：挤在一行时「门店：…」会被挤出可视区（实测过）。
        bar = ttk.Frame(parent, padding=(0, 12, 0, 0))
        bar.pack(fill="x")

        row1 = ttk.Frame(bar)
        row1.pack(fill="x")

        self.mode_var = tk.StringVar(value="offline")
        ttk.Radiobutton(row1, text="离线样例（无需 Token）", value="offline",
                        variable=self.mode_var, command=self._on_mode_change
                        ).pack(side="left", padx=(0, 14))
        ttk.Radiobutton(row1, text="在线 MCP（真实门店）", value="online",
                        variable=self.mode_var, command=self._on_mode_change
                        ).pack(side="left", padx=(0, 18))

        ttk.Label(row1, text="Token").pack(side="left")
        self.token_var = tk.StringVar(value=token)
        self.token_entry = ttk.Entry(row1, textvariable=self.token_var, width=26, show="•")
        self.token_entry.pack(side="left", padx=(5, 14))

        ttk.Label(row1, text="场景").pack(side="left")
        self.scenario_var = tk.StringVar(value="到店自取")
        self.scenario_box = ttk.Combobox(row1, textvariable=self.scenario_var,
                                         values=list(SCENARIOS), state="readonly", width=10)
        self.scenario_box.pack(side="left", padx=(5, 14))

        self.load_btn = ttk.Button(row1, text="加载真实菜单", command=self._load_online)
        self.load_btn.pack(side="left", padx=(0, 8))

        self.login_btn = ttk.Button(row1, text="我的账号", style="Brand.TButton",
                                    command=self._open_account_window)
        self.login_btn.pack(side="left")
        ttk.Label(row1, text="积分 · 我的券 · 推荐", style="Hint.TLabel"
                  ).pack(side="left", padx=(6, 0))

        row2 = ttk.Frame(bar)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Label(row2, text="门店编码").pack(side="left")
        self.store_var = tk.StringVar()
        self.store_entry = ttk.Entry(row2, textvariable=self.store_var, width=12)
        self.store_entry.pack(side="left", padx=(5, 14))
        ttk.Label(row2, text="城市").pack(side="left")
        self.city_var = tk.StringVar()
        self.city_entry = ttk.Entry(row2, textvariable=self.city_var, width=10)
        self.city_entry.pack(side="left", padx=(5, 18))

        self.store_label_var = tk.StringVar(value="门店：—")
        ttk.Label(row2, textvariable=self.store_label_var, style="Hint.TLabel"
                  ).pack(side="left", padx=(0, 18))

        self.account_var = tk.StringVar(value="账号：未登录")
        ttk.Label(row2, textvariable=self.account_var, style="Hint.TLabel"
                  ).pack(side="left")
        self._on_mode_change()

    def _build_body(self, parent: ttk.Frame) -> None:
        # 菜单/购物车区固定高度：把纵向空间尽量留给下面的结果区
        # （否则两个都 expand，结果表格会被压到只剩两三行）。
        body = ttk.Frame(parent, height=self._body_height)
        body.pack(fill="x")
        body.pack_propagate(False)

        panes = ttk.PanedWindow(body, orient="horizontal")
        panes.pack(fill="both", expand=True)

        # ---------------- 菜单 ----------------
        menu_box = ttk.Labelframe(panes, text=" 菜单（双击加入购物车） ", padding=8)
        panes.add(menu_box, weight=3)

        search_row = ttk.Frame(menu_box)
        search_row.pack(fill="x", pady=(0, 6))
        ttk.Label(search_row, text="搜索").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._fill_menu())
        ttk.Entry(search_row, textvariable=self.search_var).pack(
            side="left", fill="x", expand=True, padx=(6, 0))

        menu_wrap = ttk.Frame(menu_box)
        menu_wrap.pack(fill="both", expand=True)
        self.menu_tree = ttk.Treeview(
            menu_wrap, columns=("name", "price", "category"),
            show="headings", selectmode="browse")
        for col, text, width, anchor in (
            ("name", "餐品", 200, "w"),
            ("price", "单价", 80, "e"),
            ("category", "分类", 80, "center"),
        ):
            self.menu_tree.heading(col, text=text)
            self.menu_tree.column(col, width=width, anchor=anchor,
                                  stretch=(col == "name"))
        mbar = ttk.Scrollbar(menu_wrap, orient="vertical", command=self.menu_tree.yview)
        self.menu_tree.configure(yscrollcommand=mbar.set)
        self.menu_tree.pack(side="left", fill="both", expand=True)
        mbar.pack(side="right", fill="y")
        self.menu_tree.bind("<Double-1>", lambda _e: self._add_from_menu())

        add_row = ttk.Frame(menu_box)
        add_row.pack(fill="x", pady=(6, 0))
        ttk.Button(add_row, text="加入购物车", command=self._add_from_menu).pack(side="left")
        ttk.Label(add_row, text="也可以用下面的「快速添加」敲一行文本",
                  style="Hint.TLabel").pack(side="left", padx=10)

        # ---------------- 购物车 ----------------
        cart_box = ttk.Labelframe(panes, text=" 购物车 ", padding=8)
        panes.add(cart_box, weight=2)

        cart_wrap = ttk.Frame(cart_box)
        cart_wrap.pack(fill="both", expand=True)
        self.cart_tree = ttk.Treeview(
            cart_wrap, columns=("name", "qty", "amount"),
            show="headings", selectmode="browse")
        for col, text, width, anchor in (
            ("name", "餐品", 160, "w"),
            ("qty", "数量", 60, "center"),
            ("amount", "小计", 90, "e"),
        ):
            self.cart_tree.heading(col, text=text)
            self.cart_tree.column(col, width=width, anchor=anchor,
                                  stretch=(col == "name"))
        cbar = ttk.Scrollbar(cart_wrap, orient="vertical", command=self.cart_tree.yview)
        self.cart_tree.configure(yscrollcommand=cbar.set)
        self.cart_tree.pack(side="left", fill="both", expand=True)
        cbar.pack(side="right", fill="y")

        ops = ttk.Frame(cart_box)
        ops.pack(fill="x", pady=(6, 0))
        ttk.Button(ops, text="＋", width=3, command=lambda: self._bump(1)).pack(side="left")
        ttk.Button(ops, text="－", width=3, command=lambda: self._bump(-1)).pack(side="left", padx=4)
        ttk.Button(ops, text="移除", command=self._remove_selected).pack(side="left", padx=(6, 4))
        ttk.Button(ops, text="清空", command=self._clear_cart).pack(side="left")
        ttk.Button(ops, text="载入示例", command=self._load_example).pack(side="right")

        self.cart_sum_var = tk.StringVar(value="原价合计 ¥0.00")
        ttk.Label(cart_box, textvariable=self.cart_sum_var, style="H2.TLabel"
                  ).pack(anchor="e", pady=(8, 0))

    def _build_actions(self, parent: ttk.Frame) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(12, 0))

        ttk.Label(row, text="快速添加").pack(side="left")
        self.want_var = tk.StringVar()
        self.want_entry = ttk.Entry(row, textvariable=self.want_var)
        self.want_entry.pack(side="left", fill="x", expand=True, padx=(6, 8))
        self.want_entry.bind("<Return>", lambda _e: self._add_from_text())
        ttk.Button(row, text="添加", command=self._add_from_text).pack(side="left", padx=(0, 8))

        self.stack_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="假设未标组的券可以叠加",
                        variable=self.stack_var).pack(side="left", padx=(6, 12))

        self.compute_btn = ttk.Button(row, text="计算最省方案", style="Accent.TButton",
                                      command=self.compute)
        self.compute_btn.pack(side="left")

        ttk.Label(parent, text="写法示例：巨无霸x2,中薯条,可乐　（也支持中文逗号）",
                  style="Hint.TLabel").pack(anchor="w", pady=(4, 0))

    def _build_results(self, parent: ttk.Frame) -> None:
        book = ttk.Notebook(parent)
        book.pack(fill="both", expand=True, pady=(10, 0))
        self.book = book

        # 页签 1：方案对比
        tab1 = ttk.Frame(book, padding=8)
        book.add(tab1, text="方案对比")
        self.plan_tree = ttk.Treeview(
            tab1, columns=("label", "orders", "subtotal", "saving", "total", "pct"),
            show="headings", selectmode="browse")
        for col, text, width, anchor in (
            ("label", "方案", 150, "w"),
            ("orders", "订单数", 70, "center"),
            ("subtotal", "小计", 110, "e"),
            ("saving", "节省", 110, "e"),
            ("total", "到手价", 120, "e"),
            ("pct", "节省率", 90, "e"),
        ):
            self.plan_tree.heading(col, text=text)
            self.plan_tree.column(col, width=width, anchor=anchor,
                                  stretch=(col == "label"))
        self.plan_tree.tag_configure("best", background="#FFF3EF", foreground=RED)
        self.plan_tree.pack(fill="both", expand=True)
        self.plan_tree.bind("<<TreeviewSelect>>", self._on_plan_select)
        self.plan_tree.bind("<Double-1>", self._on_plan_select)

        # 页签 2：最优明细
        tab2 = ttk.Frame(book, padding=8)
        book.add(tab2, text="最优明细")
        self.detail_text = self._make_text(tab2)

        # 页签 3：券包体检
        tab3 = ttk.Frame(book, padding=8)
        book.add(tab3, text="券包体检")
        self.verdict_tree = ttk.Treeview(
            tab3, columns=("status", "name", "detail", "extra"),
            show="headings", selectmode="browse")
        for col, text, width, anchor in (
            ("status", "判定", 80, "center"),
            ("name", "优惠券", 220, "w"),
            ("detail", "节省 / 原因", 260, "w"),
            ("extra", "门槛 · 渠道", 160, "w"),
        ):
            self.verdict_tree.heading(col, text=text)
            self.verdict_tree.column(col, width=width, anchor=anchor,
                                     stretch=(col in ("name", "detail")))
        self.verdict_tree.tag_configure("ok", foreground=GREEN)
        self.verdict_tree.tag_configure("no", foreground=MUTED)
        self.verdict_tree.pack(fill="both", expand=True)

        # 页签 4：完整文本报告（与 CLI 输出逐字一致，便于核对）
        tab4 = ttk.Frame(book, padding=8)
        book.add(tab4, text="完整文本报告")
        self.report_text = self._make_text(tab4, mono=True)

    def _make_text(self, parent: ttk.Frame, mono: bool = False) -> tk.Text:
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        family = "Consolas" if mono else self._family
        widget = tk.Text(wrap, wrap="word", bg=CARD, fg=INK, relief="flat",
                         font=(family, 10), padx=12, pady=10,
                         highlightthickness=0, borderwidth=0, spacing1=1, spacing3=2)
        bar = ttk.Scrollbar(wrap, orient="vertical", command=widget.yview)
        widget.configure(yscrollcommand=bar.set, state="disabled")
        widget.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        return widget

    def _build_statusbar(self, parent: ttk.Frame) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(10, 0))
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.status_var, style="Hint.TLabel").pack(side="left")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=160)
        self.progress.pack(side="right")
        self.progress.pack_forget()

    # ------------------------------------------------------------ 通用小工具

    def _set_text(self, widget: tk.Text, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.compute_btn.configure(state=state)
        self.load_btn.configure(state=state)
        # 「我的账号」按钮是导航（打开窗口），加载中也可以点，不锁定；
        # 账号窗口自己的「登录 / 刷新」才需要在忙时禁用（窗口可能没开）。
        if self.account_login_btn is not None and self.account_login_btn.winfo_exists():
            self.account_login_btn.configure(state=state)
        if busy:
            if message:
                self._set_status(message)
            self.progress.pack(side="right")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()
            # 解除禁用后要按当前数据源模式重算可用状态，
            # 否则离线模式下「加载/登录」会被重新点亮。
            self._on_mode_change()

    def _run_async(self, work, on_done) -> None:
        """把可能阻塞的活儿丢到后台线程，结果由主线程轮询取回。

        分工刻意划清：
          · 后台线程只做「纯计算 / 网络 IO」，算完把结果丢进队列，**不碰任何控件**；
          · 主线程（`_poll_results`）负责出队并渲染。
        这样既不会卡住界面，也彻底避开 Tk 的线程亲和性问题。
        """
        if self._busy:
            return
        self._set_busy(True, "处理中…")

        def runner() -> None:
            try:
                result, error = work(), None
            except Exception as exc:  # noqa: BLE001 - 统一转成界面提示
                result, error = None, exc
            self._results.put((result, error, on_done))

        threading.Thread(target=runner, daemon=True).start()

    def _poll_results(self) -> None:
        """主线程侧的队列轮询（每 80ms 一次）。"""
        try:
            while True:
                result, error, on_done = self._results.get_nowait()
                self._finish(result, error, on_done)
        except queue.Empty:
            pass
        finally:
            try:
                self.root.after(80, self._poll_results)
            except tk.TclError:
                pass          # 窗口已关闭，停止轮询

    def _finish(self, result, error, on_done) -> None:
        self._set_busy(False)
        if error is not None:
            self._set_status(f"失败：{error}")
            messagebox.showerror("出错了", str(error), parent=self.root)
            return
        on_done(result)

    # ------------------------------------------------------------ 数据源

    def _on_mode_change(self) -> None:
        """离线模式下把在线专用的输入框置灰，避免误导。

        「我的账号」按钮**不**随之禁用 —— 它是打开窗口的导航动作，
        打开后窗口里的「登录 / 刷新」会自己提示当前模式。
        """
        online = self.mode_var.get() == "online"
        for widget in (self.token_entry, self.scenario_box, self.store_entry,
                       self.city_entry, self.load_btn):
            widget.configure(state="normal" if online else "disabled")
        if online and self.scenario_box.get() in SCENARIOS:
            self.scenario_box.configure(state="readonly")
        # 账号窗口若已打开，同步它的按钮可用性
        if self.account_login_btn is not None and self.account_login_btn.winfo_exists():
            self.account_login_btn.configure(state="normal" if online else "disabled")

    def _load_offline(self) -> None:
        try:
            source = OfflineSource(self.sample_path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("样例数据读取失败",
                                 f"{self.sample_path}\n\n{exc}", parent=self.root)
            self._set_status("样例数据读取失败")
            return
        self.source = source
        self.catalog = source.menu()
        self.coupons = source.coupons()
        self.store_label = f"{self.catalog.store_name}（离线样例）"
        self.store_label_var.set(f"门店：{self.store_label}")
        self._fill_menu()
        self._set_status(
            f"已载入离线样例：{len(self.catalog.items)} 个餐品 · {len(self.coupons)} 张券。"
            f"双击左侧餐品，或在下方「快速添加」输入想吃的。"
        )

    def _load_online(self) -> None:
        if self.mode_var.get() != "online":
            self._set_status("当前是离线模式；若要连真实门店，请先选中「在线 MCP」。")
            return

        token = self.token_var.get().strip()
        if not token:
            messagebox.showwarning("缺少 Token",
                                   "在线模式需要 MCP Token。\n"
                                   "申请：https://open.mcd.cn/mcp\n"
                                   "也可以选择「离线样例」，无需 Token。",
                                   parent=self.root)
            return

        order_type, be_type = SCENARIOS.get(self.scenario_var.get(), (ORDER_TYPE_STORE, BE_TYPE_STORE))
        store = self.store_var.get().strip()
        city = self.city_var.get().strip()

        def work():
            client = McpClient(endpoint=self.endpoint, token=token)
            client.initialize()
            source = McpSource(client, order_type=order_type, be_type=be_type)
            if store:
                source.store_code = store
            elif city:
                source.find_store_code(city, city)
            else:
                raise RuntimeError(
                    "在线模式需要门店：填「门店编码」（7 位数字），或在「城市」里填城市名自动搜索。"
                )
            catalog = source.menu(source.store_code)
            coupons = source.coupons()
            return source, catalog, coupons

        def done(result) -> None:
            source, catalog, coupons = result
            self.source, self.catalog, self.coupons = source, catalog, coupons
            name = catalog.store_name or f"门店 {source.store_code}"
            self.store_label = f"{name}（{source.store_code}）"
            self.store_label_var.set(f"门店：{self.store_label}")
            self._fill_menu()
            skipped = getattr(source, "skipped_account_coupons", 0)
            extra = f"；另有 {skipped} 张账号券缺少适用商品信息，未纳入计算" if skipped else ""
            self._set_status(f"已连接真实门店：{len(catalog.items)} 个餐品 · {len(coupons)} 张券{extra}")

        self._run_async(work, done)

    # ------------------------------------------------------------ 菜单与购物车

    def _fill_menu(self) -> None:
        if self.catalog is None:
            return
        keyword = self.search_var.get().strip()
        self.menu_tree.delete(*self.menu_tree.get_children())
        for item in self.catalog.items:
            if keyword and keyword not in item.name and keyword != item.code:
                continue
            self.menu_tree.insert("", "end", iid=item.code,
                                  values=(item.name, item.price_yuan, item.category))

    def _item_by_code(self, code: str) -> Item | None:
        return self.catalog.find(code) if self.catalog else None

    def _add(self, item: Item, qty: int = 1) -> None:
        for line in self.cart:
            if line.item.code == item.code:
                line.qty += qty
                break
        else:
            self.cart.append(Line(item, qty))
        self._refresh_cart()

    def _add_from_menu(self) -> None:
        selection = self.menu_tree.selection()
        if not selection:
            self._set_status("先在左侧菜单里选一个餐品（或双击它）。")
            return
        item = self._item_by_code(selection[0])
        if item is None:
            return
        self._add(item)
        self._set_status(f"已加入：{item.name} ×1")

    def _add_from_text(self) -> None:
        text = self.want_var.get().strip()
        if not text:
            return
        if self.catalog is None:
            self._set_status("菜单还没载入。")
            return
        lines, missing = build_cart(self.catalog, text)
        for line in lines:
            self._add(line.item, line.qty)
        self.want_var.set("")
        if missing:
            self._set_status(f"已加入 {len(lines)} 项；菜单里没有：{'、'.join(missing)}")
        else:
            self._set_status(f"已加入 {len(lines)} 项。")
        self.want_entry.focus_set()

    def _selected_cart_code(self) -> str | None:
        selection = self.cart_tree.selection()
        return selection[0] if selection else None

    def _bump(self, delta: int) -> None:
        code = self._selected_cart_code()
        if not code:
            self._set_status("先在右侧购物车里选中一行。")
            return
        for line in self.cart:
            if line.item.code == code:
                line.qty += delta
                if line.qty <= 0:
                    self.cart.remove(line)
                break
        self._refresh_cart()

    def _remove_selected(self) -> None:
        code = self._selected_cart_code()
        if not code:
            self._set_status("先在右侧购物车里选中一行。")
            return
        self.cart = [ln for ln in self.cart if ln.item.code != code]
        self._refresh_cart()

    def _clear_cart(self) -> None:
        self.cart = []
        self._refresh_cart()
        self._set_status("购物车已清空。")

    def _load_example(self) -> None:
        if self.catalog is None:
            return
        self.cart = []
        lines, missing = build_cart(self.catalog, EXAMPLE_WANT)
        for line in lines:
            self._add(line.item, line.qty)
        note = f"（菜单里没有：{'、'.join(missing)}）" if missing else ""
        self._set_status(f"已载入示例购物车{note}。点「计算最省方案」看看结果。")
        self.compute()

    def _refresh_cart(self) -> None:
        self.cart_tree.delete(*self.cart_tree.get_children())
        total = 0
        for line in self.cart:
            total += line.amount_cents
            self.cart_tree.insert("", "end", iid=line.item.code,
                                  values=(line.item.name, line.qty,
                                          f"¥{line.amount_cents / 100:.2f}"))
        self.cart_sum_var.set(f"原价合计 {yuan(total)}")

    # ------------------------------------------------------------ 账号窗口

    def _open_account_window(self, render: bool = True) -> None:
        """打开「我的账号」独立窗口。

        刻意**不**把账号、积分、积分商城塞进主界面：主界面只回答「怎么点最省」，
        账号是另一类任务、信息量也大。挤在同一个窗口里会让两边都变成
        两三行高的滚动条 —— 拆成两个窗口，各自都能占满高度。

        `render=False` 供 `_render_account` 内部调用，避免两者互相递归。
        """
        win = self.account_win
        if win is not None and win.winfo_exists():
            win.deiconify()
            win.lift()
            win.focus_set()
            return

        win = tk.Toplevel(self.root)
        self.account_win = win
        win.title("我的麦当劳账号 · 麦麦券务台")
        win.configure(bg=BG)
        width = max(900, min(1260, self.root.winfo_screenwidth() - 320))
        height = max(620, min(920, self.root.winfo_screenheight() - 180))
        win.geometry(f"{width}x{height}")
        win.minsize(820, 560)

        outer = ttk.Frame(win, padding=14)
        outer.pack(fill="both", expand=True)

        head = ttk.Frame(outer)
        head.pack(fill="x")
        self.account_sum_var = tk.StringVar(
            value="尚未登录。点右侧「登录 / 刷新」，用 MCP Token 取回账号信息。")
        ttk.Label(head, textvariable=self.account_sum_var, style="H2.TLabel",
                  justify="left", wraplength=max(520, width - 280)).pack(side="left", anchor="w")
        self.account_login_btn = ttk.Button(head, text="登录 / 刷新", style="Brand.TButton",
                                            command=self._login)
        self.account_login_btn.pack(side="right")

        ttk.Label(
            outer,
            text="登录用的是 MCP Token —— 它就是你的账号凭据（麦当劳未开放第三方密码登录）。"
                 "全程只读：只查积分、券与积分商城，不会改动你的账号。",
            style="Hint.TLabel", wraplength=max(700, width - 60), justify="left",
        ).pack(anchor="w", pady=(6, 10))

        book = ttk.Notebook(outer)
        book.pack(fill="both", expand=True)
        self.account_book = book

        # ---- 页签 1：给我的建议 ----
        tab_advice = ttk.Frame(book, padding=8)
        book.add(tab_advice, text="给我的建议")
        self.advice_tree = ttk.Treeview(
            tab_advice, columns=("level", "title", "detail"),
            show="headings", selectmode="browse")
        for col, text, width_, anchor in (
            ("level", "级别", 70, "center"),
            ("title", "建议", 340, "w"),
            ("detail", "说明", 700, "w"),
        ):
            self.advice_tree.heading(col, text=text)
            self.advice_tree.column(col, width=width_, anchor=anchor, stretch=(col != "level"))
        self.advice_tree.tag_configure("urgent", foreground=RED,
                                       font=(self._family, 10, "bold"))
        self.advice_tree.tag_configure("tip", foreground="#A85E00")
        self.advice_tree.tag_configure("info", foreground=MUTED)
        self.advice_tree.pack(fill="both", expand=True)

        # ---- 页签 2：我拥有的券 ----
        tab_mine = ttk.Frame(book, padding=8)
        book.add(tab_mine, text="我拥有的券")
        self.mine_tree = ttk.Treeview(
            tab_mine, columns=("name", "face", "until", "period", "badges"),
            show="headings", selectmode="browse")
        for col, text, width_, anchor in (
            ("name", "券名", 240, "w"),
            ("face", "券面", 90, "e"),
            ("until", "有效至", 160, "center"),
            ("period", "可用时段", 190, "center"),
            ("badges", "标记", 230, "w"),
        ):
            self.mine_tree.heading(col, text=text)
            self.mine_tree.column(col, width=width_, anchor=anchor,
                                  stretch=(col in ("name", "badges")))
        self.mine_tree.pack(fill="both", expand=True)

        # ---- 页签 3：积分商城 ----
        tab_mall = ttk.Frame(book, padding=8)
        book.add(tab_mall, text="积分商城")
        self.mall_sum_var = tk.StringVar(value="登录后按你的积分余额标出可兑换的商品。")
        ttk.Label(tab_mall, textvariable=self.mall_sum_var, style="Hint.TLabel",
                  justify="left").pack(anchor="w", pady=(0, 8))
        self.mall_tree = ttk.Treeview(
            tab_mall, columns=("name", "points", "category", "down", "status"),
            show="headings", selectmode="browse")
        for col, text, width_, anchor in (
            ("name", "商品", 320, "w"),
            ("points", "所需积分", 100, "e"),
            ("category", "类目", 110, "center"),
            ("down", "兑换截止", 180, "center"),
            ("status", "状态", 80, "center"),
        ):
            self.mall_tree.heading(col, text=text)
            self.mall_tree.column(col, width=width_, anchor=anchor, stretch=(col == "name"))
        self.mall_tree.tag_configure("ok", foreground=GREEN)
        self.mall_tree.tag_configure("no", foreground=MUTED)
        self.mall_tree.pack(fill="both", expand=True)

        def close_window() -> None:
            # 清掉失效引用，下次点「我的账号」时重建
            win.destroy()
            self.account_win = None
            self.account_book = None
            self.account_login_btn = None
            self.advice_tree = self.mine_tree = self.mall_tree = None

        win.protocol("WM_DELETE_WINDOW", close_window)
        if render and self.account is not None:
            self._render_account()
        elif not render:
            pass
        else:
            self._on_mode_change()

    # ------------------------------------------------------------ 登录与账号

    def _login(self) -> None:
        """登录麦当劳账号，拉取真实积分 / 券并给出建议。

        关于「登录」：麦当劳**没有**向第三方开放账号密码登录，
        官方唯一凭据是 MCP Token（<https://open.mcd.cn/mcp> 申请，天然绑定账号）。
        因此这里的登录 = 用 Token 完成握手并成功取回本账号数据，
        **不需要也不应该输入密码**。

        全程只读：只调 query-my-account / query-my-coupons / mall-points-products
        三个查询工具；领券、下单等写操作不在这条路径上，
        以免用户在看信息时被顺手改了账号。
        """
        # 先把账号窗口开出来，让用户看到加载过程与错误提示
        self._open_account_window()

        if self.mode_var.get() != "online":
            messagebox.showinfo(
                "需要在线模式",
                "登录麦当劳账号需要 Token，请先选择「在线 MCP（真实门店）」。",
                parent=self.root)
            return

        token = self.token_var.get().strip()
        if not token:
            messagebox.showwarning(
                "缺少 Token",
                "登录需要 MCP Token（它就是你的账号凭据，不是密码）。\n"
                "申请地址：https://open.mcd.cn/mcp\n\n"
                "也可以继续用「离线样例」，无需 Token。",
                parent=self.root)
            return

        order_type, be_type = SCENARIOS.get(self.scenario_var.get(),
                                            (ORDER_TYPE_STORE, BE_TYPE_STORE))

        def work():
            client = McpClient(endpoint=self.endpoint, token=token)
            client.initialize()
            source = McpSource(client, order_type=order_type, be_type=be_type)
            account = source.my_account()
            coupons = source.my_coupons()
            try:
                rewards = source.points_products()
            except McpError:
                rewards = []      # 积分商城取不到不该让整次登录失败
            return account, coupons, rewards

        def done(result) -> None:
            account, coupons, rewards = result
            self.account, self.my_coupons, self.rewards = account, coupons, rewards
            self._render_account()
            advice_count = len(self.advice_tree.get_children())
            self._set_status(
                f"已登录：账号 {account.masked_id} · 可用 {account.available} 分 · "
                f"券 {len(coupons)} 张 · 生成 {advice_count} 条建议。"
            )

        self._run_async(work, done)

    def _render_account(self) -> None:
        account = self.account
        if account is None:
            return
        # 窗口可能还没开或刚被关掉 —— 统一走这里保证控件存在
        self._open_account_window(render=False)
        now = datetime.now()

        summary = (
            f"账号 {account.masked_id} · {account.currency or '积分'}　|　"
            f"可用 {account.available} 分　累计 {account.accumulative} 分　"
            f"已用 {account.used} 分"
        )
        if account.expiring_this_month or account.expiring_next_month:
            summary += (
                f"　|　本月将过期 {account.expiring_this_month} 分　"
                f"下月将过期 {account.expiring_next_month} 分"
            )
        self.account_sum_var.set(summary)
        self.account_var.set(f"账号：已登录 {account.masked_id} · 可用 {account.available} 分")

        # --- 建议 ---
        self.advice_tree.delete(*self.advice_tree.get_children())
        for idx, item in enumerate(advise(account, self.my_coupons, self.rewards, now)):
            self.advice_tree.insert(
                "", "end", iid=f"a{idx}", tags=(item.level,),
                values=(item.level_text, item.title, item.detail),
            )

        # --- 我的券 ---
        self.mine_tree.delete(*self.mine_tree.get_children())
        for idx, coupon in enumerate(self.my_coupons):
            self.mine_tree.insert(
                "", "end", iid=f"c{idx}",
                values=(
                    coupon.name,
                    coupon.fixed_price_yuan if coupon.is_fixed_price else "—",
                    coupon.valid_to or "不限",
                    coupon.period.describe(),
                    "、".join(coupon.badges) or "—",
                ),
            )

        # --- 积分商城 ---
        self.mall_tree.delete(*self.mall_tree.get_children())
        affordable = 0
        for idx, reward in enumerate(self.rewards):
            state = reward.state_text(now)
            usable = state == "可兑换" and reward.affordable(account.available)
            if usable:
                affordable += 1
            self.mall_tree.insert(
                "", "end", iid=f"r{idx}", tags=("ok" if usable else "no",),
                values=(reward.name, f"{reward.points} 分" if reward.points else "仅需现金",
                        reward.category, reward.down_time or "不限", state),
            )
        self.mall_sum_var.set(
            f"可用 {account.available} 分　可兑换商品 {affordable} 件"
            f"（共 {len(self.rewards)} 件在售，绿色为当前积分可兑换）"
        )

        self.account_book.select(0)      # 默认停在「给我的建议」
        self.account_win.deiconify()
        self.account_win.lift()

    # ------------------------------------------------------------ 求解与渲染

    def compute(self) -> None:
        if not self.cart:
            self._set_status("购物车是空的：双击左侧餐品，或在「快速添加」里输入想吃的。")
            return
        source = self.source
        if source is None:
            self._set_status("数据源还没准备好。")
            return

        lines = list(self.cart)
        stackable = bool(self.stack_var.get())
        store_code = getattr(source, "store_code", "") or ""

        def work():
            coupons = source.coupons()
            return coupons, solve(lines, coupons, store_code, assume_stackable=stackable)

        def done(result) -> None:
            self.coupons, solution = result
            self._render_solution(solution, lines)

        self._run_async(work, done)

    def _render_solution(self, solution: Solution, lines: list[Line]) -> None:
        self.solution = solution

        # --- 方案对比 ---
        self.plan_tree.delete(*self.plan_tree.get_children())
        for plan in solution.plans:
            tag = "best" if plan is solution.best else ""
            label = plan.label + ("（最优）" if plan is solution.best else "")
            self.plan_tree.insert(
                "", "end", iid=plan.label, tags=(tag,) if tag else (),
                values=(label, len(plan.orders), yuan(plan.subtotal_cents),
                        yuan(plan.saving_cents), yuan(plan.total_cents),
                        f"{plan.saving_pct:.1f}%"),
            )
        if self.plan_tree.get_children():
            first = self.plan_tree.get_children()[0]
            self.plan_tree.selection_set(first)

        self._render_detail(solution)
        self._render_verdicts(solution)

        full = render_solution(solution, self.store_label, lines)
        self._set_text(self.report_text, full)

        self.book.select(0)
        self._set_status(
            f"最优：{solution.best.label} → 到手价 {yuan(solution.best.total_cents)}，"
            f"比原价省 {yuan(solution.saving_vs_baseline_cents)}"
            f"（{solution.saving_vs_baseline_pct:.1f}%）。"
        )

    def _render_detail(self, solution: Solution) -> None:
        text = render_summary(solution) + "\n\n" + render_plan_detail(solution.best)
        if solution.best.notes:
            text += "\n"
        self._set_text(self.detail_text, text)

    def _render_verdicts(self, solution: Solution) -> None:
        self.verdict_tree.delete(*self.verdict_tree.get_children())
        for idx, verdict in enumerate(solution.verdicts):
            coupon = verdict.coupon
            if verdict.usable:
                detail = f"省 {yuan(verdict.saving_cents)}"
                tag = "ok"
            else:
                detail = verdict.reason
                if verdict.shortfall_cents:
                    detail += f"（还差 {yuan(verdict.shortfall_cents)}）"
                tag = "no"
            scope = "门店券" if coupon.scope == SCOPE_STORE else "账号券"
            extra = f"{coupon.mode_text} · {scope}"
            self.verdict_tree.insert(
                "", "end", iid=f"v{idx}", tags=(tag,),
                values=("可用" if verdict.usable else "不可用",
                        coupon.name, detail, extra),
            )

    def _on_plan_select(self, _event=None) -> None:
        """选中方案对比里的某一行 → 明细页签跟着切换成那个方案。"""
        solution = self.solution
        if solution is None:
            return
        selection = self.plan_tree.selection()
        if not selection:
            return
        chosen = next((p for p in solution.plans if p.label == selection[0]), None)
        if chosen is None:
            return
        text = render_summary(solution) + "\n\n" + render_plan_detail(chosen)
        self._set_text(self.detail_text, text)


def launch(
    sample_path: str | Path,
    token: str = "",
    endpoint: str = DEFAULT_ENDPOINT,
) -> int:
    """启动图形界面（阻塞直到窗口关闭）。"""
    _enable_dpi_awareness()
    root = tk.Tk()
    CouponDeskApp(root, Path(sample_path), token=token, endpoint=endpoint)
    root.mainloop()
    return 0


__all__ = ["CouponDeskApp", "launch"]
