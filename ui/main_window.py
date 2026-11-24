# -*- coding: utf-8 -*-
"""主窗口：页签整合、系统托盘提醒、状态栏。"""
import datetime
import logging
import os
import pathlib

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import (QAction, QApplication, QLabel, QMainWindow, QMenu,
                             QSystemTrayIcon, QTabWidget)

from ui.alert_tab import AlertTab
from ui.backtest_tab import BacktestTab
from ui.calendar_tab import CalendarTab
from ui.scan_center import ScanCenter
from ui.strategy_tab import StrategyTab
from ui.trade_tab import TradeTab
from ui.watch_tab import WatchTab
from ui.timing_tab import TimingTab

log = logging.getLogger("main_window")


def make_tray_icon() -> QIcon:
    pix = QPixmap(64, 64)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setBrush(QColor("#e64340"))
    p.setPen(Qt.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    p.setPen(QColor("white"))
    font = p.font()
    font.setBold(True)
    font.setPointSize(22)
    p.setFont(font)
    p.drawText(pix.rect(), Qt.AlignCenter, "票")
    p.end()
    return QIcon(pix)


class MainWindow(QMainWindow):
    def __init__(self, worker, db):
        super().__init__()
        self.worker = worker
        self.db = db
        self.setWindowTitle("个人股票交易决策辅助系统")
        self.resize(1280, 800)

        tabs = QTabWidget()
        self.timing_tab = TimingTab(db, self)  # v0.8.6：择时仪表盘（第一个页签，启动默认显示）
        self.watch_tab = WatchTab(worker, db, self)
        self.calendar_tab = CalendarTab(
            worker, db,
            on_watch_added=self.watch_tab._load_watch,
            parent=self)
        self.alert_tab = AlertTab(worker, db, names={}, parent=self)
        self.strategy_tab = StrategyTab(
            db, on_run_scan=self._run_strategy_manual,
            on_changed=self._on_strategies_changed,
            on_quick_backtest=self._on_quick_backtest, parent=self)
        self.scan_center = ScanCenter(
            worker, db,
            on_watch_changed=self.watch_tab._load_watch, parent=self)
        self.trade_tab = TradeTab(db, self)
        self.backtest_tab = BacktestTab(worker, db, parent=self)
        tabs.addTab(self.timing_tab, "🕐 择时")
        tabs.addTab(self.calendar_tab, "📅 行情日历")
        tabs.addTab(self.watch_tab, "👀 观察池")
        tabs.addTab(self.alert_tab, "🔔 提醒设置")
        tabs.addTab(self.strategy_tab, "🎯 策略管理")
        tabs.addTab(self.scan_center, "🔍 选股模块")
        tabs.addTab(self.backtest_tab, "📊 策略回测")
        tabs.addTab(self.trade_tab, "📒 交易记录")
        self.setCentralWidget(tabs)
        tabs.setCurrentWidget(self.timing_tab)   # v0.8.6：启动即落在择时页
        # v0.9.0：保留 self.tabs 引用，供「策略管理 → 策略回测」快速跳转
        self._tabs = tabs
        # v0.8.8：极简 TimingTab 在 __init__ 已自动恢复上次选择,无需 refresh
        # 移除旧版的 self.timing_tab.refresh() 调用

        self._setup_tray()
        self._setup_version_clock()

        # 后台线程信号
        self.worker.status.connect(self.statusBar().showMessage)
        self.worker.alerts_fired.connect(self._on_alerts)
        self.worker.list_ready.connect(self._on_list)

    # ------------------------------------------------------------ 右下角版本/时钟角标
    def _setup_version_clock(self):
        """状态栏右下角常驻角标：本版本代码最后修改保存时间 + 实时时钟。

        - 版本保存时间 = 项目目录下所有 .py 文件的最新修改时间（自动反映
          最近一次代码保存；代码改动保存后重启程序即更新）
        - 当前时间由 QTimer 每秒刷新，自动更新
        """
        base = pathlib.Path(__file__).resolve().parent.parent
        # 跳过 .venv/.git 等目录：里面上万个 .py 都不是本项目代码，
        # 遍历它们会让启动多花 5 秒左右
        skip_dirs = {".venv", ".git", "__pycache__", ".pytest_cache", "logs"}
        newest = datetime.datetime.fromtimestamp(0)
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for name in files:
                if not name.endswith(".py"):
                    continue
                p = pathlib.Path(root) / name
                try:
                    t = datetime.datetime.fromtimestamp(p.stat().st_mtime)
                    if t > newest:
                        newest = t
                except OSError:
                    continue
        if newest.year < 2000:  # 兜底：找不到任何 .py 时用当前时间
            newest = datetime.datetime.now()
        ver_text = newest.strftime("%Y-%m-%d %H:%M")

        self.clock_label = QLabel()
        self.clock_label.setStyleSheet("color:#888; font-size:11px;")
        self.clock_label.setToolTip(
            "🕐 版本保存时间 = 最近一次代码文件修改时间（代码保存后重启即更新）\n"
            "右侧为当前时间，每秒自动刷新")
        self.statusBar().addPermanentWidget(self.clock_label)

        def _tick():
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.clock_label.setText(f"🕐 版本保存 {ver_text} ｜ 当前 {now}")

        _tick()
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(_tick)
        self._clock_timer.start(1000)  # 每秒自动更新

    # ------------------------------------------------------------ 托盘
    def _setup_tray(self):
        self.tray = QSystemTrayIcon(make_tray_icon(), self)
        self.tray.setToolTip("股票交易决策辅助")
        menu = QMenu()
        show_act = QAction("显示主窗口", self)
        show_act.triggered.connect(self._show_window)
        quit_act = QAction("退出", self)
        quit_act.triggered.connect(QApplication.instance().quit)
        menu.addAction(show_act)
        menu.addSeparator()
        menu.addAction(quit_act)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self._show_window() if reason == QSystemTrayIcon.DoubleClick else None)
        self.tray.show()

    def _show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------ 数据回调
    def _on_list(self, df):
        names = {str(r.code): r.name for r in df.itertuples()}
        self.watch_tab.set_stock_names(names)
        self.alert_tab._names = names
        completer = self.alert_tab.code_edit.completer()
        if completer:
            from PyQt5.QtCore import QStringListModel
            completer.setModel(QStringListModel([f"{k} {v}" for k, v in names.items()]))

    def _on_alerts(self, events):
        for e in events[:3]:
            self.tray.showMessage("交易提醒", e["message"],
                                  QSystemTrayIcon.Information, 5000)
        self.statusBar().showMessage(
            f"🔔 触发 {len(events)} 条提醒: " + "；".join(e["message"] for e in events[:2]),
            8000)

    # ------------------------------------------------------------ 策略相关
    def _run_strategy_manual(self, strategy, start_date=None, end_date=None):
        """策略管理页「立即扫描所选」：按全市场模式跑单个策略。
        start_date/end_date: 历史日期扫描范围（v0.7.3），None=最新K线。"""
        stock_df = self.worker.get_stock_df()
        if stock_df is None or stock_df.empty:
            self.statusBar().showMessage("股票列表尚未加载完成，请稍候", 5000)
            return
        ok = self.worker.request_strategy_scan(
            strategy, "batch", stock_df,
            max_stocks=len(stock_df), parallel=4,
            start_date=start_date, end_date=end_date)
        if not ok:
            # v0.7.3：防重复点击——已有扫描在运行时拒绝再次提交
            self.statusBar().showMessage("已有扫描在运行，请等待其完成后再试", 6000)

    def _on_strategies_changed(self):
        self.scan_center.refresh_strategies()
        # v0.9.0：策略管理变化时同步刷新回测页的策略下拉
        if hasattr(self, "backtest_tab") and self.backtest_tab is not None:
            try:
                self.backtest_tab.refresh_strategies()
            except Exception as e:  # noqa: BLE001
                log.warning("刷新回测页策略列表失败: %s", e)

    # ------------------------------------------------------------ v0.9.0 策略回测联动
    def _on_quick_backtest(self, strategy_id: int, strategy_name: str = ""):
        """策略管理页「🔍 快速回测」入口：切到回测页签并选中策略。"""
        try:
            if hasattr(self, "_tabs") and self._tabs is not None:
                idx = self._tabs.indexOf(self.backtest_tab)
                if idx >= 0:
                    self._tabs.setCurrentIndex(idx)
            self.backtest_tab.set_selected_strategy(strategy_id, strategy_name)
            self.statusBar().showMessage(
                f"🔍 已切换到策略回测：{strategy_name or f'#{strategy_id}'}", 6000)
        except Exception as e:  # noqa: BLE001
            log.warning("快速回测跳转失败: %s", e)
            self.statusBar().showMessage(f"⚠️ 快速回测跳转失败: {e}", 5000)

    # ------------------------------------------------------------ 生命周期
    def closeEvent(self, event):
        self.worker.stop()
        self.worker.wait(3000)
        self.db.close()
        super().closeEvent(event)
