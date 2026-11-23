# -*- coding: utf-8 -*-
"""📊 策略回测页签（v0.9.0）

设计目标（信号事件独立回测 / Signal-Level Event Study）：
  - **不模拟资金账户**、无并发持仓限制——回测区间内全市场触发的每一个
    策略信号都被视为一次独立交易事件，用于输出客观的概率学统计。
  - **入场**：信号触发当日为决策日 T，以次日开盘价 Open_{t+1} 买入；
    次日一字涨停剔除。
  - **出场**（全部由 UI 通用参数控制，不绑定任何特定均线/指标）：
      ① 目标止盈 TP%：持仓期最高价 ≥ 买入价 × (1 + TP%)
      ② 硬止损 SL%：  持仓期最低价 ≤ 买入价 × (1 − SL%)
      ③ 最长跟踪 N 天：按第 N 天收盘价平仓
      ④ 移动跟踪止盈：浮盈达到 trigger% 后，从最高点回撤 trailing_pct% 触发止盈

输出 KPI：
  总信号数 / 胜率 / 盈亏比 / 平均收益 / 平均持股周期
  前瞻收益矩阵 T+1/T+3/T+5/T+10/T+20（平均涨跌幅 + 胜率）

UI 结构：
  - 顶部参数控制条：策略下拉、回测范围、出场参数、保存胜率按钮
  - KPI 卡片（胜率/盈亏比/总信号/平均收益/平均持股）
  - 前瞻收益柱状图（pyqtgraph）
  - 逐笔交易明细表（PandasModel）
  - 进度条 + 异步 QThread
"""
from __future__ import annotations

import logging
import time
import traceback
from typing import Any, Dict, List, Optional

import pandas as pd
from PyQt5.QtCore import (QDate, Qt, QThread, pyqtSignal, pyqtSlot)
from PyQt5.QtGui import QBrush, QColor, QFont
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QDialog,
                             QDoubleSpinBox, QFormLayout, QFrame, QGridLayout,
                             QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QListWidget, QListWidgetItem,
                             QMessageBox, QProgressBar, QPushButton,
                             QScrollArea, QSpinBox, QSplitter, QTableView,
                             QTextEdit, QVBoxLayout, QWidget)

from scanner import CachedFeed
from ui.models import PandasModel

log = logging.getLogger("backtest_tab")


# --------------------------------------------------------------------------
# KPI 卡片
# --------------------------------------------------------------------------
class KPICard(QFrame):
    """单个 KPI 指标卡：大字标题 + 大字数值 + 副标题。"""

    def __init__(self, title: str, value: str = "-",
                 color: str = "#333", sub: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("kpiCard")
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "#kpiCard { border: 1px solid #ddd; border-radius: 6px;"
            " background: #fafafa; padding: 8px; }"
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 6, 10, 6)
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("color:#666; font-size:11px;")
        root.addWidget(self.title_label)
        self.value_label = QLabel(value)
        f = QFont()
        f.setPointSize(20)
        f.setBold(True)
        self.value_label.setFont(f)
        self.value_label.setStyleSheet(f"color:{color};")
        root.addWidget(self.value_label)
        self.sub_label = QLabel(sub)
        self.sub_label.setStyleSheet("color:#888; font-size:10px;")
        root.addWidget(self.sub_label)
        root.addStretch(1)

    def set_value(self, value: str, color: Optional[str] = None,
                  sub: Optional[str] = None):
        self.value_label.setText(value)
        if color is not None:
            self.value_label.setStyleSheet(f"color:{color};")


# --------------------------------------------------------------------------
# 柱状图（pyqtgraph 前瞻收益矩阵）
# --------------------------------------------------------------------------
class ForwardReturnChart(QWidget):
    """前瞻多周期收益柱状图（T+1/T+3/T+5/T+10/T+20 平均收益 + 胜率折线）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plot = None
        self._win_line = None
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        try:
            import pyqtgraph as pg
            pg.setConfigOption("background", "w")
            pg.setConfigOption("foreground", "#444")
            self._plot = pg.PlotWidget(title="前瞻收益矩阵（T+N 日相对买入价）")
            self._plot.setLabel("left", "平均收益 %")
            self._plot.setLabel("bottom", "持有周期（交易日）")
            self._plot.showGrid(x=False, y=True, alpha=0.3)
            layout.addWidget(self._plot)
            self._win_line = pg.InfiniteLine(angle=0, pen=pg.mkPen("#888", width=1,
                                                                  style=2))
            self._win_line.setPos(0)
            self._plot.addItem(self._win_line)
        except Exception as e:  # noqa: BLE001
            log.warning("pyqtgraph 不可用，前瞻图降级为文本：%s", e)
            self._plot = None
            self._text = QLabel("（前瞻收益图加载失败：缺少 pyqtgraph）")
            self._text.setAlignment(Qt.AlignCenter)
            self._text.setStyleSheet("color:#888; padding:20px;")
            layout.addWidget(self._text)

    def update_data(self, forward_returns: Dict[str, Dict[str, float]]):
        """forward_returns: {'1':{'pct_avg':..,'win_rate':..,'count':..}, ...}"""
        if not forward_returns:
            return
        periods: List[int] = []
        pcts: List[float] = []
        wins: List[float] = []
        for k in sorted(forward_returns.keys(), key=lambda x: int(x) if x.isdigit() else 0):
            v = forward_returns[k]
            if v.get("count", 0) <= 0:
                continue
            periods.append(int(k))
            pcts.append(float(v.get("pct_avg", 0.0)))
            wins.append(float(v.get("win_rate", 0.0)))
        if not periods:
            return

        if self._plot is None:
            # 降级为文本
            lines = [f"T+{p}天｜收益 {pp:+.2f}%｜胜率 {w:.1f}%"
                     for p, pp, w in zip(periods, pcts, wins)]
            self._text.setText("\n".join(lines))
            return

        self._plot.clear()
        try:
            import pyqtgraph as pg
            self._win_line = pg.InfiniteLine(angle=0, pen=pg.mkPen("#888", width=1,
                                                                  style=2))
            self._win_line.setPos(0)
            self._plot.addItem(self._win_line)

            x = list(range(len(periods)))
            labels = [f"T+{p}" for p in periods]
            # 柱状图：正绿、负红
            colors = ["#089981" if v >= 0 else "#e64340" for v in pcts]
            bar = pg.BarGraphItem(x=x, height=pcts, width=0.6, brush=colors)
            self._plot.addItem(bar)
            ax = self._plot.getAxis("bottom")
            ax.setTicks([list(zip(x, labels))])

            # 折线：胜率（右轴）
            win_axis = pg.AxisItem("right")
            win_axis.setLabel("胜率 %", color="#3a6df0")
            self._plot.plotItem.scene().addItem(win_axis)
            win_axis.setStyle(showValues=False)
            win_axis.linkToView(self._plot.plotItem.getViewBox())
            win_axis.setRange(0, 100)
            line = pg.PlotDataItem(x, wins, pen=pg.mkPen("#3a6df0", width=2),
                                   symbol="o", symbolSize=8,
                                   symbolBrush="#3a6df0")
            self._plot.plotItem.getViewBox().addItem(line)
            line.setParentItem(self._plot.plotItem)

            # 数值标签
            for xi, pi, wi in zip(x, pcts, wins):
                self._plot.plotItem.textItem = pg.TextItem(
                    f"{pi:+.2f}%", color="#333", anchor=(0.5, 1.1 if pi >= 0 else -0.1))
                self._plot.plotItem.textItem.setPos(xi, pi)
                self._plot.addItem(self._plot.plotItem.textItem)
        except Exception as e:  # noqa: BLE001
            log.warning("前瞻图绘制失败：%s", e)


# --------------------------------------------------------------------------
# 异步回测工作线程
# --------------------------------------------------------------------------
class BacktestWorker(QThread):
    """在后台线程跑回测；通过信号把进度 / 结果 / 异常发回主线程。"""

    progress = pyqtSignal(int, int, str)
    finished_ok = pyqtSignal(dict)
    failed = pyqtSignal(str)
    log_line = pyqtSignal(str)

    def __init__(self, strategy_func, stock_data_dict: Dict[str, pd.DataFrame],
                 config: Dict[str, Any], parent=None):
        super().__init__(parent)
        self.strategy_func = strategy_func
        self.stock_data_dict = stock_data_dict
        self.config = config
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _is_cancelled(self):
        return self._cancel

    def run(self):
        try:
            from core.signal_backtester import backtest_strategy_signals

            def _cb(i, total, name):
                if self._cancel:
                    return
                self.progress.emit(int(i), int(total), name)

            self.log_line.emit("🚀 回测线程启动…")
            t0 = time.time()
            result = backtest_strategy_signals(
                self.strategy_func, self.stock_data_dict, self.config,
                progress_cb=_cb, cancel_cb=self._is_cancelled,
            )
            elapsed = time.time() - t0
            self.log_line.emit(
                f"✅ 回测完成，耗时 {elapsed:.1f}s"
                + ("（已被取消）" if result.get("cancelled") else ""))
            self.finished_ok.emit(result)
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc()
            log.exception("回测失败")
            self.failed.emit(f"{e}\n{tb}")


# --------------------------------------------------------------------------
# 主 Tab
# --------------------------------------------------------------------------
class BacktestTab(QWidget):
    """📊 策略回测页签。"""

    def __init__(self, worker, db, on_strategy_run_backtest=None, parent=None):
        super().__init__(parent)
        self.worker = worker
        self.db = db
        self.on_strategy_run_backtest = on_strategy_run_backtest
        self._last_result: Optional[Dict[str, Any]] = None
        self._last_strategy_id: Optional[int] = None
        self._last_strategy_name: str = ""
        self._bt_thread: Optional[BacktestWorker] = None

        # 确保策略注册表已初始化
        try:
            from core.strategy_adapter import (refresh_registry, list_strategy_names,
                                                register_from_db_rows)
            refresh_registry()
            # 把数据库里的策略也注册进来（公式类）
            db_strats = self.db.list_strategies(enabled_only=False)
            register_from_db_rows(db_strats, override=False)
        except Exception as e:  # noqa: BLE001
            log.warning("策略注册表初始化失败: %s", e)

        self._build_ui()
        self._refresh_strategy_combo()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        # 顶部说明横幅（v0.9.0 修订：明确「全 A 股 + 全历史」语义）
        desc_label = QLabel(
            "💡 信号级事件回测：默认对<全 A 股>跑策略找历史信号，"
            "把每个信号视为一次独立交易事件，次日开盘买入，按通用出场规则出场。"
            "回测结果反映策略本身的客观胜率/盈亏比，无资金/仓位约束。"
        )
        desc_label.setStyleSheet(
            "color:#444; background:#f0f7ff; border:1px solid #d0ddf0;"
            " border-radius:4px; padding:6px 10px; font-size:11px;"
        )
        desc_label.setWordWrap(True)
        root.addWidget(desc_label)

        # 顶部参数控制条
        control_box = QGroupBox("◆ 回测参数")
        control_box.setStyleSheet(
            "QGroupBox { font-weight: bold; border: 1px solid #ddd; border-radius: 6px;"
            " margin-top: 8px; padding-top: 12px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 10px;"
            " padding: 0 4px; color: #444; }"
        )
        cb = QHBoxLayout(control_box)
        cb.setContentsMargins(10, 8, 10, 8)

        # 策略下拉
        cb.addWidget(QLabel("策略："))
        self.strategy_combo = QComboBox()
        self.strategy_combo.setMinimumWidth(220)
        self.strategy_combo.setToolTip("选择要回测的策略（系统内所有公式类策略）")
        cb.addWidget(self.strategy_combo)

        # 刷新按钮
        refresh_btn = QPushButton("🔄 刷新策略列表")
        refresh_btn.setToolTip("从策略管理页拉取最新策略列表（包含用户自定义）")
        refresh_btn.clicked.connect(self._refresh_strategy_combo)
        cb.addWidget(refresh_btn)

        cb.addSpacing(12)
        cb.addWidget(QLabel("回测范围："))
        self.range_combo = QComboBox()
        # v0.9.0 修订：把「全市场（推荐）」作为默认选项——对全 A 股所有可用 K 线
        # 跑策略找历史信号，不设时间窗口过滤。
        # 其他选项说明：
        #   - 「全市场（推荐）」：全 A 股（约 5000 只）+ 全量历史 K 线，不限时间
        #   - 「近 1/2/3 年」：全 A 股 + 仅取 N 年内 K 线，用于评估「近期表现」
        #   - 「观察池历史」：仅对当前观察池里的票做回测（用于个别重点验证）
        self.range_combo.addItem("📈 全市场（推荐）", "all")
        self.range_combo.addItem("近 1 年", "1y")
        self.range_combo.addItem("近 2 年", "2y")
        self.range_combo.addItem("近 3 年", "3y")
        self.range_combo.addItem("观察池历史", "watch")
        self.range_combo.setToolTip(
            "📈 全市场（推荐）：对全 A 股（约 5000 只）所有可用 K 线跑策略找历史信号\n"
            "近 1/2/3 年：全 A 股 + 仅取 N 年内 K 线\n"
            "观察池历史：仅对当前观察池里的票做回测\n\n"
            "回测逻辑 = 每一只票 → 跑策略找历史信号 → 每个信号视为独立事件 → "
            "次日开盘买入 → 按通用出场规则出场")
        cb.addWidget(self.range_combo)

        cb.addSpacing(12)
        cb.addWidget(QLabel("目标止盈%："))
        self.tp_spin = QDoubleSpinBox()
        self.tp_spin.setRange(1.0, 100.0)
        self.tp_spin.setDecimals(1)
        self.tp_spin.setValue(10.0)
        self.tp_spin.setSuffix(" %")
        cb.addWidget(self.tp_spin)

        cb.addWidget(QLabel("硬止损%："))
        self.sl_spin = QDoubleSpinBox()
        self.sl_spin.setRange(0.5, 50.0)
        self.sl_spin.setDecimals(1)
        self.sl_spin.setValue(4.0)
        self.sl_spin.setSuffix(" %")
        cb.addWidget(self.sl_spin)

        cb.addWidget(QLabel("最长持股："))
        self.max_days_spin = QSpinBox()
        self.max_days_spin.setRange(1, 60)
        self.max_days_spin.setValue(10)
        self.max_days_spin.setSuffix(" 天")
        cb.addWidget(self.max_days_spin)

        cb.addWidget(QLabel("跟踪止盈："))
        self.trailing_check = QCheckBox("启用")
        self.trailing_check.setChecked(True)
        self.trailing_check.setToolTip("浮盈达 +8% 后若从最高点回撤 ≥3% 触发止盈")
        cb.addWidget(self.trailing_check)

        cb.addStretch(1)
        root.addWidget(control_box)

        # 操作按钮 + 进度条
        op_bar = QHBoxLayout()
        self.run_btn = QPushButton("🚀 开始策略回测")
        self.run_btn.setStyleSheet(
            "QPushButton { background:#3a6df0; color:white; font-weight:bold;"
            " padding:6px 18px; border-radius:4px; }"
            "QPushButton:hover { background:#5a8dfa; }"
            "QPushButton:disabled { background:#bbb; }"
        )
        self.run_btn.clicked.connect(self._on_run)
        op_bar.addWidget(self.run_btn)

        self.cancel_btn = QPushButton("停止")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._on_cancel)
        op_bar.addWidget(self.cancel_btn)

        op_bar.addSpacing(12)
        self.save_btn = QPushButton("💾 保存胜率至策略档案")
        self.save_btn.setToolTip(
            "将本次回测的胜率、盈亏比等数据写入策略管理页的策略列表标签")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._on_save_stats)
        op_bar.addWidget(self.save_btn)

        op_bar.addStretch(1)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(280)
        self.progress.setFormat("%v / %m  (%p%)")
        op_bar.addWidget(self.progress)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color:#666; font-size:11px;")
        op_bar.addWidget(self.status_label)
        root.addLayout(op_bar)

        # KPI 卡片
        kpi_bar = QHBoxLayout()
        self.kpi_total = KPICard("总信号数", "—", "#333")
        self.kpi_win = KPICard("核心胜率", "—", "#089981", sub="盈利/总信号")
        self.kpi_pl = KPICard("盈亏比", "—", "#3a6df0", sub="平均盈利 / |平均亏损|")
        self.kpi_avg = KPICard("平均收益", "—", "#e64340", sub="所有信号平均盈亏%")
        self.kpi_hold = KPICard("平均持股", "—", "#333", sub="交易日")
        for c in (self.kpi_total, self.kpi_win, self.kpi_pl, self.kpi_avg, self.kpi_hold):
            kpi_bar.addWidget(c)
        root.addLayout(kpi_bar)

        # 下半区：前瞻图 + 逐笔明细
        splitter = QSplitter(Qt.Vertical)
        splitter.setHandleWidth(6)

        # 前瞻图
        chart_box = QGroupBox("◆ 前瞻多周期收益")
        chart_box.setStyleSheet(
            "QGroupBox { font-weight: bold; border: 1px solid #ddd; border-radius: 6px;"
            " margin-top: 8px; padding: 12px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 10px;"
            " padding: 0 4px; color: #444; }"
        )
        cv = QVBoxLayout(chart_box)
        cv.setContentsMargins(8, 8, 8, 8)
        self.forward_chart = ForwardReturnChart()
        cv.addWidget(self.forward_chart)
        splitter.addWidget(chart_box)

        # 交易明细
        trade_box = QGroupBox("◆ 逐笔交易明细")
        trade_box.setStyleSheet(
            "QGroupBox { font-weight: bold; border: 1px solid #ddd; border-radius: 6px;"
            " margin-top: 8px; padding: 12px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 10px;"
            " padding: 0 4px; color: #444; }"
        )
        tv = QVBoxLayout(trade_box)
        tv.setContentsMargins(8, 8, 8, 8)
        # 过滤行
        fbar = QHBoxLayout()
        fbar.addWidget(QLabel("代码过滤："))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("可输入代码或名称前缀过滤（留空 = 全部）")
        self.filter_edit.setMaximumWidth(220)
        self.filter_edit.textChanged.connect(self._apply_filter)
        fbar.addWidget(self.filter_edit)
        fbar.addWidget(QLabel("出场原因："))
        self.exit_combo = QComboBox()
        self.exit_combo.addItem("全部", None)
        for r in ("止盈", "止损", "到期平仓", "移动止盈"):
            self.exit_combo.addItem(r, r)
        self.exit_combo.currentIndexChanged.connect(self._apply_filter)
        fbar.addWidget(self.exit_combo)
        fbar.addStretch(1)
        self.trade_summary_label = QLabel("")
        self.trade_summary_label.setStyleSheet("color:#666; font-size:11px;")
        fbar.addWidget(self.trade_summary_label)
        tv.addLayout(fbar)

        self.trade_table = QTableView()
        self.trade_table.setModel(PandasModel(None, color_cols=("盈亏%", "峰值盈亏%")))
        self.trade_table.setAlternatingRowColors(True)
        self.trade_table.setSelectionBehavior(QTableView.SelectRows)
        self.trade_table.setSortingEnabled(True)
        self.trade_table.horizontalHeader().setStretchLastSection(True)
        self.trade_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        tv.addWidget(self.trade_table)
        splitter.addWidget(trade_box)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([260, 420])
        root.addWidget(splitter, stretch=1)

    # ------------------------------------------------------------------ API
    def refresh_strategies(self):
        """外部通知策略列表变化时调用。"""
        self._refresh_strategy_combo()

    def set_selected_strategy(self, strategy_id: Optional[int], strategy_name: str = ""):
        """策略管理页点击「🔍 快速回测」时调用：定位到对应策略。

        - strategy_id 优先（数据库 id）
        - strategy_name 兜底（按名称匹配）
        """
        try:
            if strategy_id is not None:
                for i in range(self.strategy_combo.count()):
                    data = self.strategy_combo.itemData(i)
                    if data == ("id", int(strategy_id)):
                        self.strategy_combo.setCurrentIndex(i)
                        return
            if strategy_name:
                for i in range(self.strategy_combo.count()):
                    if self.strategy_combo.itemText(i) == strategy_name:
                        self.strategy_combo.setCurrentIndex(i)
                        return
        except Exception as e:  # noqa: BLE001
            log.warning("set_selected_strategy 失败: %s", e)

    # ------------------------------------------------------------------ 数据
    def _refresh_strategy_combo(self):
        """从数据库 + 注册表构建策略下拉。

        数据用 ("source", key) 编码：
          ("id", strategy_id) → 数据库里的策略（公式类）
          ("reg", name)       → 注册表中的内置策略（即使 db 没保存）
        """
        cur = self.strategy_combo.currentData()
        self.strategy_combo.clear()

        items: List[tuple] = []  # (label, ("source", key))
        # 数据库里的策略
        for s in self.db.list_strategies(enabled_only=False):
            if s.get("kind") == "dna":
                continue  # DNA 策略不支持纯公式回测
            label = f"[{s.get('code', '?')}] {s.get('name', '')} (数据库)"
            items.append((label, ("id", int(s["id"]))))

        # 注册表中的内置策略（数据库里还没有的）
        try:
            from core.strategy_adapter import list_strategy_names, refresh_registry
            refresh_registry()
            db_names = {s.get("name", "") for s in self.db.list_strategies(enabled_only=False)}
            for name in list_strategy_names():
                if name in db_names:
                    continue  # 已经在数据库里
                items.append((f"{name} (内置)", ("reg", name)))
        except Exception as e:  # noqa: BLE001
            log.warning("加载内置策略失败: %s", e)

        if not items:
            self.strategy_combo.addItem("（暂无可回测策略）", None)
            self.run_btn.setEnabled(False)
            return

        self.run_btn.setEnabled(True)
        for label, data in items:
            self.strategy_combo.addItem(label, data)

        # 尝试恢复之前选择
        if cur is not None:
            for i in range(self.strategy_combo.count()):
                if self.strategy_combo.itemData(i) == cur:
                    self.strategy_combo.setCurrentIndex(i)
                    break

    def _get_selected_strategy(self):
        """根据下拉返回 (strategy_func, strategy_id, strategy_name)。

        - 数据库策略：直接用 formula 解析
        - 内置策略：先从注册表取 callable
        """
        data = self.strategy_combo.currentData()
        if data is None:
            return None, None, ""
        try:
            if data[0] == "id":
                row = self.db.get_strategy(int(data[1]))
                if not row:
                    return None, None, ""
                if row.get("kind") == "dna":
                    QMessageBox.warning(self, "提示", "DNA 策略暂不支持纯公式回测")
                    return None, None, ""
                from core.strategy_adapter import build_func_from_db_row
                func = build_func_from_db_row(row)
                if func is None:
                    QMessageBox.warning(self, "提示",
                                        f"策略「{row.get('name')}」无可用公式")
                    return None, None, ""
                return func, int(row["id"]), row.get("name", "")
            elif data[0] == "reg":
                from core.strategy_adapter import get_strategy_func
                func = get_strategy_func(data[1])
                if func is None:
                    return None, None, ""
                # 内置策略在数据库里没 id：用名称关联
                return func, None, data[1]
        except Exception as e:  # noqa: BLE001
            log.warning("获取策略失败: %s", e)
        return None, None, ""

    def _build_stock_data(self, range_key: str) -> Dict[str, pd.DataFrame]:
        """按回测范围构造 {code: DataFrame} 字典。

        - all（默认 / 全市场）：取全 A 股所有可用 K 线（不设时间窗口）
        - 1y/2y/3y：取全 A 股 + 限定起止时间
        - watch：仅取当前观察池里的票（个别重点验证）
        """
        import datetime as _dt

        # 选股列表
        if range_key == "watch":
            watch = self.db.list_watch(status=None)
            if not watch:
                QMessageBox.warning(self, "提示", "观察池为空，无法回测")
                return {}
            codes = sorted({(str(w.get("code", "")), int(w.get("market") or 0),
                              w.get("name", "")) for w in watch})
            stock_df = pd.DataFrame(codes, columns=["code", "market", "name"])
            start_date = None
        else:
            # all / 1y / 2y / 3y：取全 A 股（worker.get_stock_df() = 全市场列表）
            stock_df = self.worker.get_stock_df() if self.worker else None
            if stock_df is None or stock_df.empty:
                stock_df = pd.DataFrame(self.db.load_stock_list())
            if stock_df is None or stock_df.empty:
                return {}
            # 计算日期下界
            today = _dt.date.today()
            if range_key == "1y":
                start_date = (today - _dt.timedelta(days=365)).isoformat()
            elif range_key == "2y":
                start_date = (today - _dt.timedelta(days=730)).isoformat()
            elif range_key == "3y":
                start_date = (today - _dt.timedelta(days=365 * 3)).isoformat()
            else:  # "all" 或未知 → 不设时间窗口
                start_date = None

        # 用 CachedFeed 拉数据（先走 SQLite 缓存，未命中再网络）
        feed = CachedFeed(self.worker.feed, self.db) if self.worker else None
        if feed is None:
            return {}

        result: Dict[str, pd.DataFrame] = {}
        n = len(stock_df)
        for i, row in stock_df.iterrows():
            try:
                code = str(row.get("code", ""))
                market = int(row.get("market") or 0)
                if not code:
                    continue
                # 全市场模式：拉够 ~3 年 K 线（780 ≈ 3 年交易日），保证指标预热
                df = feed.get_daily_bars(code, market=market, count=800)
                if df is None or df.empty or len(df) < 30:
                    continue
                if start_date:
                    df = df[df["date"] >= start_date].reset_index(drop=True)
                    if len(df) < 30:
                        continue
                df = df.copy()
                if "name" not in df.columns:
                    df["name"] = row.get("name", "")
                else:
                    df["name"] = row.get("name", "") or df["name"].iloc[0]
                result[code] = df
            except Exception as e:  # noqa: BLE001
                log.debug("拉取 %s 失败: %s", row.get("code"), e)
            self.status_label.setText(f"📦 加载数据：{i + 1}/{n}")
        self.status_label.setText(f"📦 数据就绪：{len(result)} 只票")
        return result

    # ------------------------------------------------------------------ 操作
    def _on_run(self):
        if self._bt_thread is not None and self._bt_thread.isRunning():
            QMessageBox.information(self, "提示", "已有回测任务在运行，请等待完成")
            return

        func, strategy_id, strategy_name = self._get_selected_strategy()
        if func is None:
            QMessageBox.warning(self, "提示", "请先选择有效的策略")
            return

        range_key = self.range_combo.currentData() or "all"

        # 构建 config
        trailing_pct = 0.03 if self.trailing_check.isChecked() else None
        config = {
            "tp_pct": float(self.tp_spin.value()) / 100.0,
            "sl_pct": float(self.sl_spin.value()) / 100.0,
            "max_days": int(self.max_days_spin.value()),
            "trailing_pct": trailing_pct,
            "trailing_trigger": 0.08 if trailing_pct else None,
            "forward_periods": [1, 3, 5, 10, 20],
            "skip_limit_up": True,
        }

        # 加载数据（同步加载，进度条即时刷新）
        self.run_btn.setEnabled(False)
        self.save_btn.setEnabled(False)
        self.progress.setRange(0, 0)  # 不确定进度（数据加载阶段）
        self.status_label.setText("📦 加载日K数据…")
        QApplication_processEvents_safe()

        try:
            stock_data = self._build_stock_data(range_key)
        except Exception as e:  # noqa: BLE001
            log.exception("加载数据失败")
            QMessageBox.critical(self, "加载失败", str(e))
            self.run_btn.setEnabled(True)
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            return

        if not stock_data:
            QMessageBox.warning(self, "提示",
                                "无可用数据：日K缓存或观察池为空，请先同步行情数据")
            self.run_btn.setEnabled(True)
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            return

        # 启动后台线程
        self._last_strategy_id = strategy_id
        self._last_strategy_name = strategy_name
        self._bt_thread = BacktestWorker(func, stock_data, config, parent=self)
        self._bt_thread.progress.connect(self._on_progress)
        self._bt_thread.finished_ok.connect(self._on_result)
        self._bt_thread.failed.connect(self._on_failed)
        self._bt_thread.log_line.connect(self.status_label.setText)
        self.progress.setRange(0, len(stock_data))
        self.progress.setValue(0)
        self.cancel_btn.setEnabled(True)
        self._bt_thread.start()

    def _on_cancel(self):
        if self._bt_thread is not None:
            self._bt_thread.cancel()
            self.status_label.setText("⏹ 正在取消…")

    @pyqtSlot(int, int, str)
    def _on_progress(self, i, total, name):
        self.progress.setMaximum(max(1, int(total)))
        self.progress.setValue(int(i))
        if name:
            self.status_label.setText(f"⚙️ 回测中 {i}/{total}: {name}")

    @pyqtSlot(dict)
    def _on_result(self, result: Dict[str, Any]):
        self._last_result = result
        self._update_kpi(result)
        self._update_table(result)
        self.forward_chart.update_data(result.get("forward_returns", {}))
        summary = result.get("stats", {})
        self.trade_summary_label.setText(
            f"总 {summary.get('total_signals', 0)}｜"
            f"盈 {summary.get('wins', 0)}｜亏 {summary.get('losses', 0)}｜"
            f"出场：" + " ｜ ".join(
                f"{k} {v}" for k, v in (result.get("exit_reason_dist") or {}).items()))
        self.status_label.setText("✅ 回测完成")
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.save_btn.setEnabled(True)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self._bt_thread = None

    @pyqtSlot(str)
    def _on_failed(self, msg: str):
        log.error("回测失败: %s", msg)
        QMessageBox.critical(self, "回测失败", msg[:2000])
        self.status_label.setText("❌ 回测失败")
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self._bt_thread = None

    def _update_kpi(self, result: Dict[str, Any]):
        s = result.get("stats", {})
        total = int(s.get("total_signals", 0))
        win_rate = float(s.get("win_rate", 0.0))
        pl = float(s.get("profit_loss_ratio", 0.0))
        avg_ret = float(s.get("avg_ret", 0.0))

        self.kpi_total.set_value(f"{total}")
        wr_color = "#089981" if win_rate >= 50 else "#e64340"
        self.kpi_win.set_value(f"{win_rate:.1f}%", wr_color,
                               sub=f"盈利 {s.get('wins', 0)} / 亏损 {s.get('losses', 0)}")
        self.kpi_pl.set_value(f"{pl:.2f}")
        ar_color = "#e64340" if avg_ret >= 0 else "#089981"
        self.kpi_avg.set_value(f"{avg_ret:+.2f}%", ar_color,
                               sub=f"最佳 {s.get('best_ret', 0):+.2f}% ｜"
                                   f"最差 {s.get('worst_ret', 0):+.2f}%")
        self.kpi_hold.set_value(f"{s.get('avg_holding_days', 0):.1f} 天")

    def _update_table(self, result: Dict[str, Any]):
        from core.signal_backtester import trades_to_dataframe
        df = trades_to_dataframe(result.get("trades", []))
        self._all_trades_df = df
        self._apply_filter()

    def _apply_filter(self):
        df = getattr(self, "_all_trades_df", None)
        if df is None or df.empty:
            self.trade_table.model().setDataFrame(pd.DataFrame())
            return
        mask = pd.Series(True, index=df.index)
        kw = self.filter_edit.text().strip()
        if kw:
            mask &= (df["代码"].astype(str).str.contains(kw, case=False)
                     | df["名称"].astype(str).str.contains(kw, case=False))
        reason = self.exit_combo.currentData()
        if reason:
            mask &= df["出场原因"] == reason
        view = df[mask].reset_index(drop=True)
        self.trade_table.model().setDataFrame(view)

    # ------------------------------------------------------------------ 保存胜率
    def _on_save_stats(self):
        if not self._last_result:
            QMessageBox.information(self, "提示", "暂无回测结果可保存")
            return
        if self._last_strategy_id is None:
            # 内置策略（不在数据库）：提示用户先在策略管理中导入
            ret = QMessageBox.question(
                self, "内置策略",
                f"策略「{self._last_strategy_name}」是系统内置策略，尚未导入到"
                "策略管理页的策略档案中。是否现在创建一条同名策略以便保存胜率？")
            if ret != QMessageBox.Yes:
                return
            try:
                from core.strategy_adapter import get_strategy_func, REGISTERED_STRATEGIES
                from strategy_presets import PRESET_STRATEGIES
                preset = next((p for p in PRESET_STRATEGIES
                               if p.get("name") == self._last_strategy_name), None)
                if preset is None:
                    QMessageBox.warning(self, "提示",
                                        f"找不到「{self._last_strategy_name}」的内置公式")
                    return
                sid = self.db.add_strategy(
                    name=preset["name"], kind=preset.get("kind", "formula"),
                    formula=preset.get("formula", ""),
                    scan_mode=preset.get("scan_mode", "batch"),
                    enabled=1, description=preset.get("description", ""))
                self._last_strategy_id = int(sid)
                self._refresh_strategy_combo()
                QMessageBox.information(self, "已创建",
                                        f"已创建策略档案 id={sid}，下面保存胜率。")
            except Exception as e:  # noqa: BLE001
                log.exception("创建策略档案失败")
                QMessageBox.critical(self, "创建失败", str(e))
                return

        stats = self._last_result.get("stats", {})
        try:
            self.db.save_backtest_stats(
                strategy_id=int(self._last_strategy_id),
                win_rate=stats.get("win_rate"),
                profit_loss_ratio=stats.get("profit_loss_ratio"),
                total_signals=stats.get("total_signals"),
                avg_ret=stats.get("avg_ret"),
                avg_holding_days=stats.get("avg_holding_days"),
                forward_returns=self._last_result.get("forward_returns", {}),
                config=self._last_result.get("config", {}),
                raw={
                    "stats": stats,
                    "forward_returns": self._last_result.get("forward_returns", {}),
                    "exit_reason_dist": self._last_result.get("exit_reason_dist", {}),
                    "strategy_name": self._last_strategy_name,
                    "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
            QMessageBox.information(
                self, "已保存",
                f"已将策略「{self._last_strategy_name}」的最新胜率 "
                f"{stats.get('win_rate', 0):.1f}% / 盈亏比 "
                f"{stats.get('profit_loss_ratio', 0):.2f} 写入策略档案。\n"
                "策略管理页列表将以「胜率 X%」标签展示。")
        except Exception as e:  # noqa: BLE001
            log.exception("保存回测统计失败")
            QMessageBox.critical(self, "保存失败", str(e))


# --------------------------------------------------------------------------
# 工具：跨线程刷新 UI
# --------------------------------------------------------------------------
def QApplication_processEvents_safe():
    try:
        from PyQt5.QtWidgets import QApplication
        QApplication.processEvents()
    except Exception:  # noqa: BLE001
        pass
