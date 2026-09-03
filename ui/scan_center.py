# -*- coding: utf-8 -*-
"""选股模块（v0.8.9 大改：拆分三子页）。

架构：
    - 顶部工具条（运行按钮 / 停止 / 自动扫描 / 间隔 / 候选数 / 信号日期 / 当日总结）
    - 进度条
    - QTabWidget 三子页：
        ① 本次一层（◆ 本次扫描 · 一层粗筛命中）
        ② 本次二层（◆ 本次扫描 · 二层DNA精筛打分榜）
        ③ 命中历史（全部层 + 策略/日期/层筛选）

设计：
    - "本次" = 今天的 scan_history（按 trade_date/scan_time 过滤），
      按 layer 自动分流到对应子页。
    - "命中历史" = 全部历史，支持策略/日期/层多维度筛选。
    - 行情日历承担长期按日期归档（已有功能，本模块不重复）。
"""
from __future__ import annotations

import datetime
from typing import List, Optional

from PyQt5.QtCore import QDate, QTimer
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QDialog, QFileDialog,
                             QFrame, QHBoxLayout, QLabel, QMessageBox,
                             QPlainTextEdit, QProgressBar, QPushButton, QSpinBox,
                             QTableView, QTabWidget, QVBoxLayout, QWidget)

import logging

from data_feed import is_trading_time
from ui.models import PandasModel

log = logging.getLogger("scan_center")


# ===================================================================
#  列定义
# ===================================================================
COARSE_COLUMNS = ["id", "strategy_name", "signal_date", "code", "name",
                  "price", "pct", "scan_time", "reason", "layer"]
# 二层DNA精筛展示列（v0.7.4：综合得分/骨架相似度/买点定级/微观诊断）
DNA_COLUMNS = ["code", "name", "signal_date", "综合得分", "骨架相似度",
               "买点定级", "最佳模具", "price", "pct", "微观诊断"]


class ScanCenter(QWidget):
    """选股模块（v0.8.9：三子页重构）。"""

    def __init__(self, worker, db, on_watch_changed=None, parent=None):
        super().__init__(parent)
        self.worker = worker
        self.db = db
        self.on_watch_changed = on_watch_changed
        self._last_intraday = None
        self._quotes_df = None
        self._quotes_time = None
        self._pending_intraday = False
        self._latest_coarse = []  # 最近一次粗筛结果（精筛候选）
        self._build_ui()
        self._load_layer1_today()
        self._load_layer2_today()
        self._load_history()
        self._refresh_dna_strategies()

        self.worker.scan_progress.connect(self._on_progress)
        self.worker.scan_done.connect(self._on_done)
        self.worker.scan_error.connect(self._on_error)
        self.worker.market_quotes_ready.connect(self._on_market_quotes)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._auto_intraday)
        self.timer.start(30000)

    # ===================================================================
    #  UI 构造
    # ===================================================================
    def _build_ui(self):
        root = QVBoxLayout(self)

        # ---------- 顶部工具条（v0.8.9：保持不变，信号日期控制扫描信号日） ----------
        bar = QHBoxLayout()
        self.batch_btn = QPushButton("盘前/盘后全市场扫描")
        self.batch_btn.setToolTip("运行所有「盘前盘后全市场」节奏的启用策略")
        self.batch_btn.clicked.connect(lambda: self._run_auto("batch"))
        bar.addWidget(self.batch_btn)
        self.intraday_btn = QPushButton("盘中重点范围扫描")
        self.intraday_btn.setToolTip("运行所有「盘中重点」节奏的启用策略")
        self.intraday_btn.clicked.connect(lambda: self._run_auto("intraday"))
        bar.addWidget(self.intraday_btn)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.worker.cancel_scan)
        bar.addWidget(self.stop_btn)
        bar.addSpacing(16)
        self.auto_check = QCheckBox("盘中自动扫描")
        self.auto_check.setChecked(True)
        bar.addWidget(self.auto_check)
        bar.addWidget(QLabel("间隔(分):"))
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 60)
        self.interval_spin.setValue(5)
        bar.addWidget(self.interval_spin)
        bar.addWidget(QLabel("候选数:"))
        self.top_n = QSpinBox()
        self.top_n.setRange(20, 1000)
        self.top_n.setValue(150)
        bar.addWidget(self.top_n)
        bar.addSpacing(12)
        bar.addWidget(QLabel("信号日期:"))
        self.scan_start = QDateEdit()
        self.scan_end = QDateEdit()
        dmin, dmax = self.db.get_bars_date_range()
        for e in (self.scan_start, self.scan_end):
            e.setCalendarPopup(True)
            e.setDisplayFormat("yyyy-MM-dd")
            # v0.8.20: 日期控件 max 改用今天 (允许选还没缓存 K 线的日期)
            #           min 用 db 缓存的最早日期 (避免选太早没数据)
            if dmin:
                e.setMinimumDate(QDate.fromString(dmin, "yyyy-MM-dd"))
            e.setMaximumDate(QDate.currentDate().addDays(7))  # 允许选未来一周(预留)
            e.setMinimumDate(QDate.currentDate().addYears(-5))  # 下限放宽到 5 年前
        # 默认 end_date = 今天 (而不是缓存最大日期), 让用户跑今天的扫描
        self.scan_start.setDate(QDate.currentDate().addMonths(-1))
        self.scan_end.setDate(QDate.currentDate())
        self.scan_start.setToolTip("信号日期范围起点（默认=一个月前；可手动调整）")
        self.scan_end.setToolTip("信号日期范围终点（默认=今天；选历史日期可对照验证）")
        bar.addWidget(self.scan_start)
        bar.addWidget(QLabel("~"))
        bar.addWidget(self.scan_end)
        bar.addStretch(1)
        summary_btn = QPushButton("📋 当日总结")
        summary_btn.clicked.connect(self._daily_summary)
        bar.addWidget(summary_btn)
        root.addLayout(bar)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        # ---------- v0.8.9 三子页 ----------
        self.tabs = QTabWidget()
        self.tab_layer1 = QWidget()
        self.tab_layer2 = QWidget()
        self.tab_history = QWidget()
        self.tabs.addTab(self.tab_layer1, "本次一层")
        self.tabs.addTab(self.tab_layer2, "本次二层")
        self.tabs.addTab(self.tab_history, "命中历史")
        root.addWidget(self.tabs, 1)

        self._build_tab_layer1()
        self._build_tab_layer2()
        self._build_tab_history()

    # ---------- 子页 1：本次一层 ----------
    def _build_tab_layer1(self):
        v = QVBoxLayout(self.tab_layer1)
        v.addWidget(QLabel("◆ 本次扫描 · 一层粗筛命中（今天的 layer=1 命中）"))
        self.table_layer1 = QTableView()
        self.table_layer1.setModel(PandasModel(None, color_cols=("pct",)))
        self.table_layer1.setAlternatingRowColors(True)
        self.table_layer1.doubleClicked.connect(lambda _i: self._view_chart_from(self.table_layer1))
        v.addWidget(self.table_layer1)

    # ---------- 子页 2：本次二层 ----------
    def _build_tab_layer2(self):
        v = QVBoxLayout(self.tab_layer2)
        # 顶部精筛控制条
        dbar = QHBoxLayout()
        dbar.addWidget(QLabel("◆ 本次扫描 · 二层DNA精筛 | 精筛策略:"))
        self.dna_strategy_combo = QComboBox()
        self.dna_strategy_combo.currentIndexChanged.connect(self._on_dna_strategy_changed)
        dbar.addWidget(self.dna_strategy_combo)
        self.dna_cfg_label = QLabel("")
        self.dna_cfg_label.setStyleSheet("color:#666;")
        dbar.addWidget(self.dna_cfg_label)
        dbar.addStretch(1)
        self.dna_run_btn = QPushButton("▶ 对最近粗筛结果精筛")
        self.dna_run_btn.setToolTip("对最近一次一层粗筛的命中列表运行DNA比对（不重跑粗筛）")
        self.dna_run_btn.clicked.connect(self._dna_run_coarse)
        dbar.addWidget(self.dna_run_btn)
        self.dna_market_btn = QPushButton("🌐 全市场DNA直测")
        self.dna_market_btn.setToolTip("对全市场 A 股直接运行DNA比对（快速模式）")
        self.dna_market_btn.clicked.connect(self._dna_run_market)
        dbar.addWidget(self.dna_market_btn)
        v.addLayout(dbar)

        self.dna_hint = QLabel("")
        self.dna_hint.setStyleSheet("color:#b03a2e;")
        v.addWidget(self.dna_hint)

        v.addWidget(QLabel("◆ 打分排行榜（双击行看K线；自动进池并标注DNA分数/买点定级）"))
        self.table_layer2 = QTableView()
        self.table_layer2.setModel(PandasModel(None, color_cols=("pct",)))
        self.table_layer2.setAlternatingRowColors(True)
        self.table_layer2.doubleClicked.connect(lambda _i: self._view_chart_from(self.table_layer2))
        v.addWidget(self.table_layer2)

    # ---------- 子页 3：命中历史 ----------
    def _build_tab_history(self):
        v = QVBoxLayout(self.tab_history)

        hbar = QHBoxLayout()
        hbar.addWidget(QLabel("◆ 命中历史（全部层）"))
        hbar.addWidget(QLabel("策略:"))
        self.hist_strategy_combo = QComboBox()
        self.hist_strategy_combo.addItem("全部", None)
        self.hist_strategy_combo.currentIndexChanged.connect(self._load_history)
        hbar.addWidget(self.hist_strategy_combo)
        hbar.addWidget(QLabel("日期:"))
        self.hist_date_combo = QComboBox()
        self.hist_date_combo.setEditable(True)
        self.hist_date_combo.addItem(datetime.date.today().isoformat())
        self.hist_date_combo.addItem("")
        self.hist_date_combo.setCurrentIndex(0)
        self.hist_date_combo.currentTextChanged.connect(self._load_history)
        hbar.addWidget(self.hist_date_combo)
        hbar.addWidget(QLabel("层:"))
        self.hist_layer_combo = QComboBox()
        self.hist_layer_combo.addItem("全部", None)
        self.hist_layer_combo.addItem("一层粗筛", 1)
        self.hist_layer_combo.addItem("二层精筛", 2)
        self.hist_layer_combo.currentIndexChanged.connect(self._load_history)
        hbar.addWidget(self.hist_layer_combo)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._load_history)
        hbar.addWidget(refresh_btn)
        hbar.addStretch(1)
        v.addLayout(hbar)

        self.table_history = QTableView()
        self.table_history.setModel(PandasModel(None, color_cols=("pct",)))
        self.table_history.setAlternatingRowColors(True)
        self.table_history.doubleClicked.connect(lambda _i: self._view_chart_from(self.table_history))
        v.addWidget(self.table_history)

    # ===================================================================
    #  行情快照 / 扫描执行（保留 v0.8.5 行为）
    # ===================================================================
    SNAPSHOT_MAX_AGE = 90

    def _snapshot_fresh(self):
        if self._quotes_df is None or self._quotes_time is None:
            return False
        return (datetime.datetime.now() - self._quotes_time).total_seconds() < self.SNAPSHOT_MAX_AGE

    def _request_snapshot(self):
        stock_df = self.worker.get_stock_df()
        if stock_df is None or stock_df.empty:
            return False
        codes = [(r.market, r.code) for r in stock_df.itertuples()]
        self.worker.request_market_quotes(codes, save=True)
        return True

    def _on_market_quotes(self, df):
        self._quotes_df = df
        self._quotes_time = datetime.datetime.now()
        if self._pending_intraday:
            self._pending_intraday = False
            self.statusBarHint("行情快照已就绪，自动继续盘中扫描…")
            self._run_auto("intraday")

    def statusBarHint(self, text):
        win = self.window()
        if hasattr(win, "statusBar"):
            win.statusBar().showMessage(text, 5000)

    def _strategies_of_mode(self, mode):
        return [s for s in self.db.list_strategies(enabled_only=True)
                if s.get("kind") != "indicator" and s.get("scan_mode") == mode]

    def _run_auto(self, mode):
        """批量扫描：遍历所有匹配节奏的启用策略（v0.8.9 恢复 request_auto_scan 正确调用）。"""
        strategies = self._strategies_of_mode(mode)
        if not strategies:
            QMessageBox.information(
                self, "提示",
                "没有启用且节奏为「盘前盘后全市场」的策略" if mode == "batch"
                else "没有启用且节奏为「盘中重点」的策略\n\n请在「策略管理」页添加或修改策略节奏")
            return
        stock_df = self.worker.get_stock_df()
        if stock_df is None or stock_df.empty:
            QMessageBox.information(self, "提示", "股票列表尚未加载完成，请稍候")
            return
        start_date = self.scan_start.date().toString("yyyy-MM-dd")
        end_date = self.scan_end.date().toString("yyyy-MM-dd")
        if start_date > end_date:
            QMessageBox.warning(self, "提示", "开始日期不能晚于结束日期")
            return
        if mode == "intraday":
            # 盘中候选依赖行情快照：过期则先拉快照，就绪后自动补跑
            if not self._snapshot_fresh():
                if self._request_snapshot():
                    self._pending_intraday = True
                    self.statusBarHint("正在刷新行情快照，稍后自动开始盘中扫描…")
                return
            candidates = self._intraday_candidates(stock_df)
            self._set_running(True)
            ok = self.worker.request_auto_scan(
                strategies, "intraday", candidates, quote_filter=None,
                max_stocks=len(candidates), parallel=4,
                start_date=start_date, end_date=end_date)
        else:
            self._set_running(True)
            ok = self.worker.request_auto_scan(
                strategies, "batch", stock_df, quote_filter=None,
                max_stocks=len(stock_df), parallel=4,
                start_date=start_date, end_date=end_date)
        if not ok:
            self._set_running(False)
            QMessageBox.information(self, "提示", "已有扫描在运行，请等待其完成后再试")

    def _intraday_candidates(self, stock_df):
        """盘中候选 = 观察池内股票 ∪ 实时行情涨幅榜前 N（快照）。"""
        quotes = self._quotes_df
        codes = set()
        if quotes is not None and not quotes.empty:
            top = quotes.sort_values("pct", ascending=False).head(self.top_n.value())
            codes.update(top["code"].tolist())
        for w in self.db.list_watch():
            if w["status"] in ("待审核", "观察中", "已开仓"):
                codes.add(str(w["code"]))
        return stock_df[stock_df["code"].isin(codes)].reset_index(drop=True)

    def _set_running(self, running):
        self.batch_btn.setEnabled(not running)
        self.intraday_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.progress.setVisible(running)
        if running:
            self.progress.setRange(0, 0)

    def _on_progress(self, i, total, name):
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(i)
        self.statusBarHint(f"扫描中 [{i}/{total}] {name or ''}")

    def _on_done(self, info):
        self._set_running(False)
        if not isinstance(info, dict):
            return
        # 一层/二层都刷新对应子页
        self._load_layer1_today()
        self._load_layer2_today()
        self._load_history()
        layer = info.get("layer")
        if layer == 2:
            if info.get("new_hits"):
                if self.on_watch_changed:
                    self.on_watch_changed()
                QMessageBox.information(
                    self, "DNA精筛完成",
                    f"精筛策略「{info.get('strategy_name', '')}」："
                    f"通过 {len(info.get('results', []))} 只，"
                    f"新信号 {len(info.get('new_hits', []))} 只（已自动加入观察池待审核）")
            else:
                self.statusBarHint(f"DNA精筛「{info.get('strategy_name', '')}」完成："
                                    f"通过 {len(info.get('results', []))} 只")
            # 切到二层子页便于查看
            self.tabs.setCurrentWidget(self.tab_layer2)
            return
        # 一层完成
        self._latest_coarse = info.get("results", [])
        # v0.8.10：盘前/盘后全市场扫描 → 自动同步到通达信（每天每个策略一个板块）
        if info.get("mode") == "batch" and self._latest_coarse:
            self._auto_sync_to_tdx(info)
        if info.get("new_hits"):
            if self.on_watch_changed:
                self.on_watch_changed()
            QMessageBox.information(
                self, "扫描完成",
                f"策略「{info.get('strategy_name', '')}」：命中 {len(info.get('results', []))} 只，"
                f"新信号 {len(info.get('new_hits', []))} 只（已自动加入观察池待审核）")

    def _auto_sync_to_tdx(self, info):
        """v0.8.10：盘前/盘后扫描完成后, 把命中代码按策略同步到通达信自定义板块。

        板块名 = `MMDD<策略名>`(v0.8.12, 去 · 节省 GBK 字节), 例 `831B1十张图`。
        日期 = 用户选的 end_date (signal_date), 保证与 watch_pool.add_date /
               行情日历归档日期一致。
        例外: TDX 没装 / 路径错误 / 自动同步关闭 → 静默跳过(仅状态栏提示)。
        """
        from core.auto_tdx_sync import auto_sync_codes_to_tdx
        import datetime as _dt
        results = info.get("results") or []
        codes = [str(r.code) for r in results if getattr(r, "code", None)]
        strategy_name = info.get("strategy_name") or "未命名"
        # v0.8.12：用 signal_date 作为板块名日期(用户选的 end_date)
        signal_date = None
        for r in results:
            sd = getattr(r, "signal_date", None)
            if sd:
                signal_date = str(sd)[:10]
                break
        if not signal_date:
            signal_date = self.scan_end.date().toString("yyyy-MM-dd") or _dt.date.today().isoformat()
        ok, msg = auto_sync_codes_to_tdx(self.db, strategy_name, codes, date=signal_date)
        # 第一行只摘第一行放状态栏(短), 完整消息在 hint 字段(可悬浮)
        head = msg.splitlines()[0] if msg else ""
        if ok:
            self.statusBarHint(head)
            log.info("TDX 自动同步成功: %s (共 %d 只)", strategy_name, len(codes))
        else:
            self.statusBarHint(f"TDX 同步未执行/失败：{head}")
            log.info("TDX 自动同步跳过/失败: %s — %s", strategy_name, msg)

    def _on_error(self, msg):
        self._set_running(False)
        QMessageBox.critical(self, "扫描失败", msg)

    # ===================================================================
    #  子页 1: 本次一层 数据加载
    # ===================================================================
    def _load_layer1_today(self):
        """本次（今日）一层粗筛命中."""
        today = datetime.date.today().isoformat()
        rows = self.db.list_scan_history(day=today, limit=500)
        rows = [r for r in rows if r.get("layer", 1) == 1]
        rows = self._enrich_with_strategy_layer(rows)
        df = self._to_df(rows, COARSE_COLUMNS)
        self.table_layer1.model().setDataFrame(df)
        self._update_tab_label(0, "本次一层", len(rows))

    # ===================================================================
    #  子页 2: 本次二层 数据加载
    # ===================================================================
    def _load_layer2_today(self):
        """本次（今日）二层DNA精筛命中，按综合分降序."""
        today = datetime.date.today().isoformat()
        rows = self.db.list_scan_history(day=today, limit=500)
        rows = [r for r in rows if r.get("layer", 1) == 2]
        rows = self._enrich_with_strategy_layer(rows)
        # 按综合得分降序（同分按骨架相似度降序）
        def sort_key(r):
            fs = r.get("final_score")
            if fs is None:
                fs = r.get("dna_score") or 0.0
            dt = r.get("dtw_score")
            if dt is None:
                dt = r.get("dna_score") or 0.0
            return (-float(fs), -float(dt))
        rows = sorted(rows, key=sort_key)
        # 构造展示列
        import pandas as pd
        items = []
        for i, r in enumerate(rows, 1):
            items.append({
                "序号": i,
                "code": r.get("code"),
                "name": r.get("name"),
                "signal_date": r.get("signal_date") or r.get("trade_date") or "",
                "综合得分": r.get("final_score") if r.get("final_score") is not None
                            else r.get("dna_score"),
                "骨架相似度": r.get("dtw_score") if r.get("dtw_score") is not None
                            else r.get("dna_score"),
                "买点定级": r.get("buy_level") or "",
                "最佳模具": r.get("matched_template") or r.get("dna_template") or "",
                "price": r.get("price"),
                "pct": r.get("pct"),
                "微观诊断": r.get("signals") or r.get("reason") or "",
                "层级": r.get("_layer_label", ""),
            })
        df = pd.DataFrame(items)
        self.table_layer2.model().setDataFrame(df)
        self._update_tab_label(1, "本次二层", len(items))

    # ===================================================================
    #  子页 3: 命中历史 数据加载
    # ===================================================================
    def _load_history(self):
        strategy_id = self.hist_strategy_combo.currentData()
        day = self.hist_date_combo.currentText().strip() or None
        rows = self.db.list_scan_history(strategy_id=strategy_id, day=day, limit=1000)
        layer_filter = self.hist_layer_combo.currentData()
        if layer_filter is not None:
            rows = [r for r in rows if r.get("layer", 1) == layer_filter]
        rows = self._enrich_with_strategy_layer(rows)
        df = self._to_df(rows, COARSE_COLUMNS)
        self.table_history.model().setDataFrame(df)
        self._update_tab_label(2, "命中历史", len(rows))

    # ===================================================================
    #  通用辅助
    # ===================================================================
    def _enrich_with_strategy_layer(self, rows):
        """为每行附加 layer 中文标签（粗筛/精筛）。"""
        out = []
        for r in rows:
            r2 = dict(r)
            r2["layer"] = "粗筛" if r.get("layer", 1) == 1 else "精筛"
            out.append(r2)
        return out

    def _to_df(self, rows, cols):
        import pandas as pd
        df = pd.DataFrame(rows)
        keep = [c for c in cols if c in (df.columns if not df.empty else [])]
        return df[keep] if not df.empty else df

    def _update_tab_label(self, idx, base, count):
        suffix = f" ({count})" if count else ""
        self.tabs.setTabText(idx, base + suffix)

    def _view_chart_from(self, table):
        idx = table.currentIndex()
        df = table.model().dataframe()
        if not (idx.isValid() and 0 <= idx.row() < len(df)):
            return
        row = df.iloc[idx.row()]
        from data_feed import infer_market
        from ui.chart_widget import ChartDialog
        code = str(row.get("code") or row.get("代码") or "")
        if not code:
            return
        name = str(row.get("name") or row.get("名称") or "")
        ChartDialog(code, name, infer_market(code), self.db, self).exec_()

    # ===================================================================
    #  DNA 精筛（下区，与 v0.8.5 同：选中策略 + 两个按钮）
    # ===================================================================
    def _refresh_dna_strategies(self):
        cur = self.dna_strategy_combo.currentData()
        self.dna_strategy_combo.blockSignals(True)
        self.dna_strategy_combo.clear()
        strategies = [s for s in self.db.list_strategies(enabled_only=True)
                      if s.get("kind") == "dna"]
        for s in strategies:
            label = s["name"]
            if s.get("parent_strategy_id"):
                parent = self.db.get_strategy(s["parent_strategy_id"])
                if parent:
                    label += f"（从属:{parent['name']}）"
            self.dna_strategy_combo.addItem(label, s["id"])
        self.dna_strategy_combo.blockSignals(False)
        if cur is not None:
            idx = self.dna_strategy_combo.findData(cur)
            if idx >= 0:
                self.dna_strategy_combo.setCurrentIndex(idx)
        if not strategies:
            self.dna_hint.setText(
                "⚠️ 模具库/精筛策略为空：请先在「🎯 策略管理」页制作黄金模具（🧬 DNA模具），"
                "再新增「DNA精筛」类型策略")
        else:
            self.dna_hint.setText("")
        self._on_dna_strategy_changed()

    def _on_dna_strategy_changed(self):
        sid = self.dna_strategy_combo.currentData()
        if sid is None:
            self.dna_cfg_label.setText("（未选择精筛策略）")
            self.dna_run_btn.setEnabled(False)
            self.dna_market_btn.setEnabled(False)
            return
        st = self.db.get_strategy(sid)
        if not st:
            return
        import json
        try:
            cfg = json.loads(st.get("formula") or "{}")
        except ValueError:
            cfg = {}
        templates = cfg.get("templates", [])
        threshold = cfg.get("threshold", 0)
        self.dna_cfg_label.setText(f"模具 {len(templates)} 个 | 阈值 {threshold}分")
        self.dna_run_btn.setEnabled(bool(templates))
        self.dna_market_btn.setEnabled(bool(templates))

    def _dna_selected_strategy(self):
        sid = self.dna_strategy_combo.currentData()
        if sid is None:
            return None
        st = self.db.get_strategy(sid)
        if st is None:
            QMessageBox.information(self, "提示", "请先选择精筛策略")
        return st

    def _dna_run_coarse(self):
        st = self._dna_selected_strategy()
        if st is None:
            return
        if not self._latest_coarse:
            self._latest_coarse = self.db.list_latest_layer_hits(1)
        if not self._latest_coarse:
            QMessageBox.information(
                self, "提示", "还没有一层粗筛结果。\n请先在上区运行「盘前/盘后全市场扫描」"
                              "或「盘中重点范围扫描」。")
            return
        self._set_running(True)
        ok = self.worker.request_dna_scan(
            st, self._latest_coarse, mode="dna_manual",
            as_of_date=self.scan_end.date().toString("yyyy-MM-dd"))
        if not ok:
            self._set_running(False)
            QMessageBox.information(self, "提示", "已有扫描在运行，请等待其完成后再试")

    def _dna_run_market(self):
        st = self._dna_selected_strategy()
        if st is None:
            return
        stock_df = self.worker.get_stock_df()
        if stock_df is None or stock_df.empty:
            QMessageBox.information(self, "提示", "股票列表尚未加载完成，请稍候")
            return
        if QMessageBox.question(
                self, "全市场DNA直测",
                f"将对全市场 {len(stock_df)} 只股票直接运行DNA比对（快速模式，"
                f"预计数分钟）。继续？") != QMessageBox.Yes:
            return
        candidates = [{"code": r.code, "name": r.name, "market": r.market}
                      for r in stock_df.itertuples()]
        self._set_running(True)
        ok = self.worker.request_dna_scan(
            st, candidates, mode="dna_fast", parallel=4,
            as_of_date=self.scan_end.date().toString("yyyy-MM-dd"))
        if not ok:
            self._set_running(False)
            QMessageBox.information(self, "提示", "已有扫描在运行，请等待其完成后再试")

    # ===================================================================
    #  自动扫描 + 当日总结（保留）
    # ===================================================================
    def _auto_intraday(self):
        if not is_trading_time():
            return
        if not self._snapshot_fresh() and self.batch_btn.isEnabled():
            self._request_snapshot()
        if not self.auto_check.isChecked():
            return
        now = datetime.datetime.now()
        if self._last_intraday is None or \
                (now - self._last_intraday).total_seconds() >= self.interval_spin.value() * 60:
            if self.batch_btn.isEnabled():
                self._last_intraday = now
                self._run_auto("intraday")

    def _daily_summary(self):
        today = datetime.date.today().isoformat()
        lines = [f"===== 交易决策辅助 · 当日总结 {today} =====", ""]
        hits = self.db.list_scan_history(day=today, limit=500)
        lines.append(f"【当日策略命中】共 {len(hits)} 条")
        for h in hits[:50]:
            lines.append(f"  {h['scan_time'][11:19]} {h['strategy_name']}: "
                         f"{h['name']}({h['code']}) 价格{h['price']} 涨幅{h['pct']}%")
        if not hits:
            lines.append("  （无）")
        lines.append("")
        logs = self.db.list_alert_logs(limit=500)
        today_logs = [x for x in logs if (x.get("created_at") or "").startswith(today)]
        lines.append(f"【当日提醒】共 {len(today_logs)} 条")
        for x in today_logs[:30]:
            lines.append(f"  {x['created_at'][11:19]} {x['message']}")
        if not today_logs:
            lines.append("  （无）")
        lines.append("")
        watches = self.db.list_watch()
        lines.append(f"【观察池】共 {len(watches)} 只")
        snap = {str(s["code"]): s for s in self.db.load_market_snapshot()}
        for w in watches[:50]:
            s = snap.get(str(w["code"]))
            if s:
                lines.append(f"  {w['name']}({w['code']}) [{w['status']}] "
                             f"现价{s['price']} 涨幅{s['pct']}% 入选价{w.get('entry_price') or '-'}")
            else:
                lines.append(f"  {w['name']}({w['code']}) [{w['status']}](无行情)")
        lines.append("")
        trades = self.db.list_trades(limit=500)
        today_trades = [t for t in trades if (t.get("trade_time") or "").startswith(today)]
        lines.append(f"【当日交易】共 {len(today_trades)} 笔")
        for t in today_trades:
            lines.append(f"  {t['trade_time'][11:19]} {t['name']}({t['code']}) "
                         f"{t['trade_type']} @{t['trade_price']} x{t['trade_volume']} "
                         f"条件:{t.get('trigger_condition') or '-'}")
        if not today_trades:
            lines.append("  （无）")
        SummaryDialog("\n".join(lines), self).exec_()

    def refresh_strategies(self):
        """策略管理页有变动时刷新两个 strategy 下拉."""
        # 历史子页的策略下拉
        cur = self.hist_strategy_combo.currentData()
        self.hist_strategy_combo.blockSignals(True)
        self.hist_strategy_combo.clear()
        self.hist_strategy_combo.addItem("全部", None)
        for s in self.db.list_strategies():
            self.hist_strategy_combo.addItem(s["name"], s["id"])
        if cur is not None:
            idx = self.hist_strategy_combo.findData(cur)
            if idx >= 0:
                self.hist_strategy_combo.setCurrentIndex(idx)
        self.hist_strategy_combo.blockSignals(False)
        self._load_history()
        # DNA 精筛下拉
        self._refresh_dna_strategies()


class SummaryDialog(QDialog):
    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle("当日总结")
        self.resize(680, 560)
        v = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setPlainText(text)
        self.text.setReadOnly(True)
        v.addWidget(self.text)
        bar = QHBoxLayout()
        save_btn = QPushButton("保存为文本")
        save_btn.clicked.connect(self._save)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        bar.addStretch(1)
        bar.addWidget(save_btn)
        bar.addWidget(close_btn)
        v.addLayout(bar)

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "保存总结", f"当日总结-{datetime.date.today().isoformat()}.txt",
            "文本文件 (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.text.toPlainText())
            QMessageBox.information(self, "保存", f"已保存到\n{path}")
