# -*- coding: utf-8 -*-
"""选股模块（v0.8.9 大改：拆分三子页；v0.10.0 新增「自动日报」子页）。

架构：
    - 顶部工具条（运行按钮 / 停止 / 自动扫描 / 间隔 / 候选数 / 信号日期 / 当日总结）
    - 进度条
    - QTabWidget 四子页：
        ① 本次一层（◆ 本次扫描 · 一层粗筛命中）
        ② 本次二层（◆ 本次扫描 · 二层DNA精筛打分榜）
        ③ 命中历史（全部层 + 策略/日期/层筛选）
        ④ 自动日报（v0.10.0：定时全市场扫描 → 汇总文档 → 飞书推送）

设计：
    - "本次" = 今天的 scan_history（按 trade_date/scan_time 过滤），
      按 layer 自动分流到对应子页。
    - "命中历史" = 全部历史，支持策略/日期/层多维度筛选。
    - "自动日报" = 每日定时跑一次 batch 全市场扫描，结束后把当日全部结果汇总成
      Markdown 并推送飞书（逻辑见 daily_report.py / feishu.py）。
    - 行情日历承担长期按日期归档（已有功能，本模块不重复）。
"""
from __future__ import annotations

import datetime
import threading
from typing import List, Optional

from PyQt5.QtCore import QDate, QTime, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QDialog, QFileDialog,
                             QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                             QPlainTextEdit, QProgressBar, QPushButton, QSpinBox,
                             QTableView, QTabWidget, QTimeEdit, QVBoxLayout, QWidget)

import logging

from data_feed import DataFeed, is_trading_time
from daily_report import (DailyReportConfig, build_daily_report, parse_hhmm,
                          resolve_scan_date, save_report, should_fire)
from feishu import describe, is_configured, normalize_webhook, send_message, test_message
import ai_summary
from ai_summary import AIConfig
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
    """选股模块（v0.8.9：三子页重构；v0.10.0：新增「自动日报」子页）。"""

    # v0.10.0：飞书推送在工作线程执行，结果回主线程（避免网络阻塞界面）
    report_sent = pyqtSignal(bool, str)
    # v0.10.3：信号日探测完成（工作线程 → 主线程），携带 (信号日, 说明)
    scan_date_ready = pyqtSignal(str, str)
    # v0.10.4：AI 组装阶段的工作线程 → 主线程
    daily_log_line = pyqtSignal(str)      # 运行日志
    daily_doc_ready = pyqtSignal(str)     # 最终文档（手动模式弹窗用）

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
        # ---- v0.10.0 自动日报状态 ----
        self._daily_pending = False      # 定时任务已触发、等待扫描结束
        self._daily_day = None           # 本次日报对应的信号日
        self._silent_ui = False          # 定时任务期间不弹提示框
        self._daily_scheduled = False    # 本次是定时触发（据此决定是否记录「已执行」）
        self._daily_retry = 0            # 股票列表未就绪时的重试计数
        self._daily_cfg = None           # 配置内存缓存（_load_daily_cfg 填充）
        self._ai_cfg = None              # AI 点评配置缓存
        self._build_ui()
        self._load_layer1_today()
        self._load_layer2_today()
        self._load_history()
        self._refresh_dna_strategies()
        self._load_daily_cfg()

        self.worker.scan_progress.connect(self._on_progress)
        self.worker.scan_done.connect(self._on_done)
        self.worker.scan_error.connect(self._on_error)
        self.worker.market_quotes_ready.connect(self._on_market_quotes)
        # v0.10.0：整批扫描结束 → 生成日报并推送飞书
        self.worker.auto_scan_done.connect(self._on_auto_scan_done)
        self.report_sent.connect(self._on_report_sent)
        self.scan_date_ready.connect(self._on_scan_date_ready)
        self.daily_log_line.connect(self._daily_log)
        self.daily_doc_ready.connect(
            lambda text: SummaryDialog(text, self).exec_())

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._auto_intraday)
        self.timer.timeout.connect(self._on_daily_tick)   # v0.10.0
        self.timer.start(30000)
        # v0.10.0：启动 45 秒后检查一次是否错过当天定时（勾选「启动时补跑」才执行）
        QTimer.singleShot(45000, self._daily_catchup_check)

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
        self.tab_daily = QWidget()
        self.tabs.addTab(self.tab_layer1, "本次一层")
        self.tabs.addTab(self.tab_layer2, "本次二层")
        self.tabs.addTab(self.tab_history, "命中历史")
        self.tabs.addTab(self.tab_daily, "自动日报")   # v0.10.0
        root.addWidget(self.tabs, 1)

        self._build_tab_layer1()
        self._build_tab_layer2()
        self._build_tab_history()
        self._build_tab_daily()

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

    # ---------- 子页 4：自动日报（v0.10.0） ----------
    def _build_tab_daily(self):
        v = QVBoxLayout(self.tab_daily)

        tip = QLabel(
            "◆ 每天定时跑一次「盘前/盘后全市场扫描」→ 当日全部策略结果汇总成 Markdown 文档"
            " → 通过飞书机器人推送到群里/个人。\n"
            "   · 扫描范围 = 全部「盘前盘后全市场」节奏的启用策略（含二层 DNA 串联）；"
            "信号日固定为当天，避免区间扫描把多日结果混在一起。\n"
            "   · 程序需要在设定时间处于运行状态；错过时间时可在下方勾选「启动时补跑」。")
        tip.setWordWrap(True)
        v.addWidget(tip)

        # ---- 定时 ----
        row1 = QHBoxLayout()
        self.daily_enabled = QCheckBox("启用每日定时扫描")
        row1.addWidget(self.daily_enabled)
        row1.addWidget(QLabel("执行时间:"))
        self.daily_time = QTimeEdit()
        self.daily_time.setDisplayFormat("HH:mm")
        self.daily_time.setTime(QTime(15, 30))
        row1.addWidget(self.daily_time)
        self.daily_weekday = QCheckBox("仅工作日")
        row1.addWidget(self.daily_weekday)
        self.daily_catchup = QCheckBox("启动时补跑当天")
        self.daily_catchup.setToolTip(
            "在设定时间之后才打开程序时，当天仍补执行一次；\n"
            "不勾选则只在设定时间后 2 小时宽限内自动执行")
        row1.addWidget(self.daily_catchup)
        row1.addStretch(1)
        self.daily_last_label = QLabel("上次执行：—")
        row1.addWidget(self.daily_last_label)
        v.addLayout(row1)

        # ---- 飞书：推送方式 ----
        r_mode = QHBoxLayout()
        r_mode.addWidget(QLabel("推送方式:"))
        self.feishu_mode = QComboBox()
        self.feishu_mode.addItem("自建应用机器人（App ID + Secret + 接收方）", "app")
        self.feishu_mode.addItem("自定义机器人（Webhook 地址）", "webhook")
        self.feishu_mode.currentIndexChanged.connect(self._on_feishu_mode_changed)
        r_mode.addWidget(self.feishu_mode)
        r_mode.addStretch(1)
        self.feishu_test_btn = QPushButton("测试发送")
        self.feishu_test_btn.clicked.connect(self._test_feishu)
        r_mode.addWidget(self.feishu_test_btn)
        v.addLayout(r_mode)

        # ---- 应用机器人（app_id + app_secret + receive_id） ----
        r_app = QHBoxLayout()
        r_app.addWidget(QLabel("App ID:"))
        self.feishu_app_id = QLineEdit()
        self.feishu_app_id.setPlaceholderText(
            "开发者后台 → 应用详情 → 凭证与基础信息")
        r_app.addWidget(self.feishu_app_id, 1)
        r_app.addWidget(QLabel("App Secret:"))
        self.feishu_app_secret = QLineEdit()
        self.feishu_app_secret.setEchoMode(QLineEdit.Password)
        self.feishu_app_secret.setPlaceholderText("应用凭证里的 App Secret")
        r_app.addWidget(self.feishu_app_secret, 1)
        v.addLayout(r_app)

        r_recv = QHBoxLayout()
        r_recv.addWidget(QLabel("接收方:"))
        self.feishu_receive_id = QLineEdit()
        self.feishu_receive_id.setPlaceholderText(
            "群 chat_id（oc_ 开头，机器人需在群里）/ open_id / user_id / 邮箱")
        r_recv.addWidget(self.feishu_receive_id, 1)
        r_recv.addWidget(QLabel("类型:"))
        self.feishu_receive_type = QComboBox()
        for _key, _label in (("chat_id", "群 chat_id"), ("open_id", "用户 open_id"),
                             ("user_id", "用户 user_id"), ("email", "邮箱")):
            self.feishu_receive_type.addItem(_label, _key)
        r_recv.addWidget(self.feishu_receive_type)
        v.addLayout(r_recv)

        # ---- 自定义机器人（Webhook） ----
        r_hook = QHBoxLayout()
        r_hook.addWidget(QLabel("Webhook:"))
        self.feishu_webhook = QLineEdit()
        self.feishu_webhook.setPlaceholderText(
            "群机器人 → 添加自定义机器人 里复制；只粘最后那串 token 也能自动补全")
        r_hook.addWidget(self.feishu_webhook, 1)
        r_hook.addWidget(QLabel("签名密钥:"))
        self.feishu_secret = QLineEdit()
        self.feishu_secret.setEchoMode(QLineEdit.Password)
        self.feishu_secret.setPlaceholderText(
            "机器人安全设置选「签名校验」才需要填")
        r_hook.addWidget(self.feishu_secret, 1)
        v.addLayout(r_hook)

        # ---- AI 点评（v0.10.4）----
        r_ai1 = QHBoxLayout()
        self.ai_enabled = QCheckBox("启用 AI 点评")
        self.ai_enabled.setToolTip(
            "把当日汇总交给大模型写一段点评，追加到日报末尾并随飞书一起推送；\n"
            "AI 调用失败时自动退回纯数据版，不影响推送")
        r_ai1.addWidget(self.ai_enabled)
        r_ai1.addWidget(QLabel("Base URL:"))
        self.ai_base_url = QLineEdit()
        self.ai_base_url.setPlaceholderText(ai_summary.DEFAULT_BASE_URL)
        r_ai1.addWidget(self.ai_base_url, 1)
        r_ai1.addWidget(QLabel("模型:"))
        self.ai_model = QLineEdit()
        self.ai_model.setMinimumWidth(210)
        self.ai_model.setPlaceholderText(
            "doubao-seed-1-6-250615 / deepseek-chat / qwen2.5:14b")
        r_ai1.addWidget(self.ai_model)
        v.addLayout(r_ai1)

        r_ai2 = QHBoxLayout()
        r_ai2.addWidget(QLabel("API Key:"))
        self.ai_api_key = QLineEdit()
        self.ai_api_key.setEchoMode(QLineEdit.Password)
        self.ai_api_key.setPlaceholderText("方舟 / DeepSeek 等的 API Key")
        r_ai2.addWidget(self.ai_api_key, 1)
        r_ai2.addWidget(QLabel("额外要求:"))
        self.ai_extra = QLineEdit()
        self.ai_extra.setPlaceholderText("可选，例如「更关注缩量回踩，少提普涨」")
        r_ai2.addWidget(self.ai_extra, 1)
        self.ai_test_btn = QPushButton("测试 AI")
        self.ai_test_btn.clicked.connect(self._test_ai)
        r_ai2.addWidget(self.ai_test_btn)
        v.addLayout(r_ai2)

        # ---- 内容选项 ----
        r_opt = QHBoxLayout()
        r_opt.addWidget(QLabel("每策略最多列:"))
        self.daily_max = QSpinBox()
        self.daily_max.setRange(1, 50)
        self.daily_max.setValue(10)
        r_opt.addWidget(self.daily_max)
        self.daily_inc_watch = QCheckBox("含观察池")
        self.daily_inc_watch.setChecked(True)
        r_opt.addWidget(self.daily_inc_watch)
        self.daily_inc_alert = QCheckBox("含提醒")
        self.daily_inc_alert.setChecked(True)
        r_opt.addWidget(self.daily_inc_alert)
        self.daily_save_local = QCheckBox("保存本地文档")
        self.daily_save_local.setChecked(True)
        r_opt.addWidget(self.daily_save_local)
        r_opt.addStretch(1)
        v.addLayout(r_opt)

        # ---- 操作 ----
        r_act = QHBoxLayout()
        self.daily_save_btn = QPushButton("保存配置")
        self.daily_save_btn.clicked.connect(self._save_daily_cfg)
        r_act.addWidget(self.daily_save_btn)
        self.daily_now_btn = QPushButton("立即扫描并推送")
        self.daily_now_btn.setToolTip("马上跑一次全市场扫描，结束后立即生成日报并推送")
        self.daily_now_btn.clicked.connect(self._run_daily_now)
        r_act.addWidget(self.daily_now_btn)
        self.daily_report_btn = QPushButton("只生成日报并推送")
        self.daily_report_btn.setToolTip("不重新扫描，直接用数据库里当天已有的扫描结果")
        self.daily_report_btn.clicked.connect(lambda: self._run_daily_report(auto=False))
        r_act.addWidget(self.daily_report_btn)
        r_act.addStretch(1)
        v.addLayout(r_act)

        v.addWidget(QLabel("运行日志："))
        self.daily_log_view = QPlainTextEdit()
        self.daily_log_view.setReadOnly(True)
        self.daily_log_view.setMaximumBlockCount(500)
        v.addWidget(self.daily_log_view, 1)

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

    def _run_auto(self, mode, start_date=None, end_date=None, silent=False):
        """批量扫描：遍历所有匹配节奏的启用策略。

        v0.10.0：新增 start_date/end_date（定时日报固定用当天，不改界面上的日期控件）
        与 silent（定时任务期间不弹提示框，改为写日志），返回是否成功提交。
        """
        def _fail(msg, warn=False):
            if silent:
                self._daily_log(f"✗ {msg}")
                log.warning("自动扫描未启动：%s", msg)
            elif warn:
                QMessageBox.warning(self, "提示", msg)
            else:
                QMessageBox.information(self, "提示", msg)
            return False

        strategies = self._strategies_of_mode(mode)
        if not strategies:
            return _fail(
                "没有启用且节奏为「盘前盘后全市场」的策略" if mode == "batch"
                else "没有启用且节奏为「盘中重点」的策略\n\n请在「策略管理」页添加或修改策略节奏")
        stock_df = self.worker.get_stock_df()
        if stock_df is None or stock_df.empty:
            return _fail("股票列表尚未加载完成，请稍候")
        start_date = start_date or self.scan_start.date().toString("yyyy-MM-dd")
        end_date = end_date or self.scan_end.date().toString("yyyy-MM-dd")
        if start_date > end_date:
            return _fail("开始日期不能晚于结束日期", warn=True)
        if mode == "intraday":
            # 盘中候选依赖行情快照：过期则先拉快照，就绪后自动补跑
            if not self._snapshot_fresh():
                if self._request_snapshot() and not silent:
                    self._pending_intraday = True
                    self.statusBarHint("正在刷新行情快照，稍后自动开始盘中扫描…")
                return False
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
            return _fail("已有扫描在运行，请等待其完成后再试")
        log.info("已提交自动扫描：mode=%s 策略=%d 只 信号日 %s~%s",
                 mode, len(strategies), start_date, end_date)
        return True

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
                if not self._silent_ui:
                    QMessageBox.information(
                        self, "DNA精筛完成",
                        f"精筛策略「{info.get('strategy_name', '')}」："
                        f"通过 {len(info.get('results', []))} 只，"
                        f"新信号 {len(info.get('new_hits', []))} 只（已自动加入观察池待审核）")
            else:
                self.statusBarHint(f"DNA精筛「{info.get('strategy_name', '')}」完成："
                                    f"通过 {len(info.get('results', []))} 只")
            # 切到二层子页便于查看（定时任务期间不抢用户当前页签）
            if not self._silent_ui:
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
            if not self._silent_ui:
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
        # 定时任务失败时复位状态，避免后续 tick 被 _daily_pending 永久挡住
        if self._daily_pending:
            self._daily_pending = False
            self._silent_ui = False
            self._daily_log(f"✗ 扫描失败：{msg}")
            self.statusBarHint(f"自动日报：扫描失败 {msg}")
            return
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

    # ===================================================================
    #  v0.10.0 自动日报：定时全市场扫描 → 汇总文档 → 飞书推送
    # ===================================================================
    def _daily_log(self, text):
        """写「自动日报」子页的运行日志（同时进 app.log）。"""
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        try:
            self.daily_log_view.appendPlainText(f"[{stamp}] {text}")
        except AttributeError:
            pass
        log.info("自动日报: %s", text)

    # ---------------- 配置 ----------------
    def _load_daily_cfg(self):
        cfg = DailyReportConfig.load(self.db)
        self._daily_cfg = cfg          # 内存缓存：tick 每 30 秒跑一次，不再读库
        self.daily_enabled.setChecked(cfg.enabled)
        hh, mm = parse_hhmm(cfg.hhmm)
        self.daily_time.setTime(QTime(hh, mm))
        self.daily_weekday.setChecked(cfg.weekday_only)
        self.daily_catchup.setChecked(cfg.catchup)
        self.feishu_mode.setCurrentIndex(
            max(0, self.feishu_mode.findData(cfg.mode)))
        self.feishu_webhook.setText(cfg.webhook)
        self.feishu_secret.setText(cfg.secret)
        self.feishu_app_id.setText(cfg.app_id)
        self.feishu_app_secret.setText(cfg.app_secret)
        self.feishu_receive_id.setText(cfg.receive_id)
        self.feishu_receive_type.setCurrentIndex(
            max(0, self.feishu_receive_type.findData(cfg.receive_id_type)))
        self._on_feishu_mode_changed()
        self.daily_max.setValue(cfg.max_per_strategy)
        self.daily_inc_watch.setChecked(cfg.include_watch)
        self.daily_inc_alert.setChecked(cfg.include_alert)
        self.daily_save_local.setChecked(cfg.save_local)
        self.daily_last_label.setText(
            f"上次执行：{cfg.last_run}" if cfg.last_run else "上次执行：—")
        # ---- v0.10.4 AI 点评 ----
        ai = AIConfig.load(self.db)
        self._ai_cfg = ai
        self.ai_enabled.setChecked(ai.enabled)
        self.ai_base_url.setText(ai.base_url)
        self.ai_model.setText(ai.model)
        self.ai_api_key.setText(ai.api_key)
        self.ai_extra.setText(ai.extra_prompt)

    def _on_feishu_mode_changed(self, *_):
        """按推送方式启用/禁用对应输入框，避免填错地方。"""
        is_app = (self.feishu_mode.currentData() or "app") != "webhook"
        for w in (self.feishu_app_id, self.feishu_app_secret,
                  self.feishu_receive_id, self.feishu_receive_type):
            w.setEnabled(is_app)
        for w in (self.feishu_webhook, self.feishu_secret):
            w.setEnabled(not is_app)

    def _feishu_creds(self):
        """从界面读出推送凭证（给 feishu.send_message 用）。"""
        return {
            "mode": self.feishu_mode.currentData() or "app",
            "webhook": normalize_webhook(self.feishu_webhook.text()),
            "secret": self.feishu_secret.text().strip(),
            "app_id": self.feishu_app_id.text().strip(),
            "app_secret": self.feishu_app_secret.text().strip(),
            "receive_id": self.feishu_receive_id.text().strip(),
            "receive_id_type": self.feishu_receive_type.currentData() or "chat_id",
        }

    def _collect_daily_cfg(self):
        """从界面控件读出当前配置（不读库；last_run 取内存缓存，避免 tick 反复查库）。"""
        cached = getattr(self, "_daily_cfg", None)
        creds = self._feishu_creds()
        return DailyReportConfig(
            enabled=self.daily_enabled.isChecked(),
            hhmm=self.daily_time.time().toString("HH:mm"),
            weekday_only=self.daily_weekday.isChecked(),
            mode=creds["mode"],
            webhook=creds["webhook"],
            secret=creds["secret"],
            app_id=creds["app_id"],
            app_secret=creds["app_secret"],
            receive_id=creds["receive_id"],
            receive_id_type=creds["receive_id_type"],
            max_per_strategy=self.daily_max.value(),
            include_watch=self.daily_inc_watch.isChecked(),
            include_alert=self.daily_inc_alert.isChecked(),
            save_local=self.daily_save_local.isChecked(),
            catchup=self.daily_catchup.isChecked(),
            last_run=cached.last_run if cached else "")

    def _collect_ai_cfg(self):
        """从界面读 AI 配置（不落库）。"""
        cached = getattr(self, "_ai_cfg", None)
        return AIConfig(
            enabled=self.ai_enabled.isChecked(),
            base_url=(self.ai_base_url.text().strip()
                      or ai_summary.DEFAULT_BASE_URL),
            model=(self.ai_model.text().strip() or ai_summary.DEFAULT_MODEL),
            api_key=self.ai_api_key.text().strip(),
            extra_prompt=self.ai_extra.text().strip(),
            max_tokens=getattr(cached, "max_tokens", ai_summary.DEFAULT_MAX_TOKENS),
            timeout=getattr(cached, "timeout", ai_summary.DEFAULT_TIMEOUT))

    def _save_daily_cfg(self):
        cfg = self._collect_daily_cfg()
        # 只粘了 token（oc_xxx）或 open-apis/... 也能自动补成完整 Webhook
        fixed = normalize_webhook(cfg.webhook)
        if fixed != cfg.webhook:
            cfg.webhook = fixed
            self.feishu_webhook.setText(fixed)
        cfg.save(self.db)
        self._daily_cfg = cfg
        # v0.10.4：AI 点评配置一并保存
        ai = self._collect_ai_cfg()
        ai.save(self.db)
        self._ai_cfg = ai
        self.daily_last_label.setText(
            f"上次执行：{cfg.last_run}" if cfg.last_run else "上次执行：—")
        creds = cfg.credentials()
        self._daily_log(
            f"✓ 配置已保存：{'启用' if cfg.enabled else '未启用'}，每天 {cfg.hhmm}"
            f"{'，仅工作日' if cfg.weekday_only else ''}"
            f"{'，启动时补跑' if cfg.catchup else ''}；"
            f"推送：{describe(creds) if is_configured(creds) else '未配置完整'}；"
            f"AI：{'启用 ' + ai.model if ai.enabled else '未启用'}")
        self.statusBarHint("自动日报配置已保存")

    # ---------------- 定时触发 ----------------
    def _on_daily_tick(self):
        if self._daily_pending:
            return
        cfg = self._collect_daily_cfg()
        fire, reason = should_fire(cfg)
        if fire:
            self._daily_log(f"⏰ 定时触发（{reason}）")
            self._daily_scheduled = True
            self._start_daily_job(reason)

    def _daily_catchup_check(self):
        """启动 45 秒后检查：勾选「启动时补跑」且当天还没执行 → 补跑一次。"""
        cfg = self._collect_daily_cfg()
        if not cfg.enabled or not cfg.catchup:
            return
        fire, reason = should_fire(cfg)
        if fire:
            self._daily_log(f"⏰ 启动补跑（{reason}）")
            self._daily_scheduled = True
            self._start_daily_job(reason)

    def _start_daily_job(self, reason=""):
        """启动「全市场扫描 + 日报」流程（定时/补跑/手动共用）。

        v0.10.3：先确认信号日——今天没有日K（节假日 / 行情未落地）就自动回退到
        最近一个交易日，避免推出一份空日报。
        """
        if self._daily_pending:
            return
        stock_df = self.worker.get_stock_df()
        if stock_df is None or stock_df.empty:
            self._daily_retry += 1
            if self._daily_retry <= 30:
                self._daily_log(f"  股票列表未就绪，20 秒后重试（第 {self._daily_retry} 次）")
                QTimer.singleShot(20000, lambda: self._start_daily_job(reason))
            else:
                self._daily_log("✗ 股票列表长时间未加载，取消本次任务")
                self._daily_retry = 0
                self._daily_pending = False
                self._silent_ui = False
            return
        self._daily_retry = 0
        self._daily_pending = True
        self._silent_ui = True
        self._daily_log(f"▶ {datetime.datetime.now():%H:%M:%S} 确认信号日…（{reason}）")
        self._probe_scan_date()

    def _probe_scan_date(self):
        """在工作线程里探测信号日（要发网络请求，不能阻塞界面）。"""
        def _work():
            day = datetime.date.today().isoformat()
            note = "按今天执行"
            feed = None
            try:
                feed = DataFeed(db=self.db)

                def _probe(code, market):
                    try:
                        bars = feed.get_daily_bars(code, market=market, count=6)
                        if bars is None or not len(bars):
                            return None
                        return str(bars["date"].iloc[-1])[:10]
                    except Exception as e:  # noqa: BLE001
                        log.debug("探测 %s 日K失败: %s", code, e)
                        return None

                day, note = resolve_scan_date(self.db, probe=_probe)
            except Exception as e:  # noqa: BLE001
                note = f"信号日探测失败，按今天执行（{e}）"
            finally:
                try:
                    if feed is not None:
                        feed.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            self.scan_date_ready.emit(day, note)

        threading.Thread(target=_work, daemon=True).start()

    def _on_scan_date_ready(self, day, note):
        """信号日已确定 → 真正提交扫描。"""
        if not self._daily_pending:
            return
        self._daily_day = day
        today = datetime.date.today().isoformat()
        if day != today:
            self._daily_log(f"⚠ {note}")
        else:
            self._daily_log(f"  信号日 = {day}（{note}）")
        if not self._run_auto("batch", start_date=day, end_date=day, silent=True):
            self._daily_pending = False
            self._silent_ui = False
            self._daily_log("✗ 扫描未能启动")

    def _run_daily_now(self):
        """手动「立即扫描并推送」。"""
        cfg = self._collect_daily_cfg()
        if not is_configured(cfg.credentials()):
            if QMessageBox.question(
                    self, "提示",
                    "飞书推送凭证没填全，本次只生成文档、不推送。继续？") != QMessageBox.Yes:
                return
        self._daily_scheduled = False
        self._start_daily_job("手动触发")

    def _on_auto_scan_done(self, info):
        """整批扫描（含二层串联）结束 → 生成日报。"""
        if not self._daily_pending:
            return
        self._daily_pending = False
        self._silent_ui = False
        info = info or {}
        hits = info.get("hits")
        day = self._daily_day or datetime.date.today().isoformat()
        self._daily_log(f"■ 扫描结束：命中 {hits} 只"
                        + ("（中途被取消）" if info.get("cancelled") else ""))
        # v0.10.3：回报当日日K覆盖度，覆盖偏低说明数据没完全落地（结果会偏少）
        cov = 0
        try:
            cov = self.db.count_cached_bars_on(day)
        except Exception as e:  # noqa: BLE001
            log.debug("统计当日缓存覆盖失败: %s", e)
        if cov:
            self._daily_log(f"  {day} 日K缓存覆盖 {cov} 只")
            if cov < 1000:
                self._daily_log("  ⚠ 覆盖偏低：当天行情可能未完全落地或取数失败，结果会偏少")
        # 稍等片刻，让 scan_history 的最后几笔写入与界面刷新落定
        QTimer.singleShot(1500, lambda: self._run_daily_report(auto=True))

    # ---------------- 生成 + 推送 ----------------
    def _run_daily_report(self, auto=False, day=None):
        cfg = self._collect_daily_cfg()
        if not day:
            day = self._daily_day if auto else None
        day = day or datetime.date.today().isoformat()
        try:
            rep = build_daily_report(
                self.db, day=day,
                max_per_strategy=cfg.max_per_strategy,
                include_watch=cfg.include_watch,
                include_alert=cfg.include_alert)
        except Exception as e:  # noqa: BLE001
            log.exception("生成选股日报失败")
            self._daily_log(f"✗ 生成日报失败：{e}")
            return

        st = rep["stats"]
        self._daily_log(f"■ 日报已生成：策略 {st['strategies']} 个，命中 {st['total']} 只"
                        f"（一层 {st['layer1']} / 二层 {st['layer2']}）")

        ai_cfg = self._collect_ai_cfg()
        # AI 点评 + 落盘 + 推送统一放工作线程（网络与文件 IO 都不阻塞界面）
        self._compose_and_push(rep, cfg, ai_cfg, day, auto=auto)

        # 只有「定时/补跑」才记为今日已执行，避免手动提前生成把定时任务顶掉
        if self._daily_scheduled:
            cfg.mark_ran(self.db, day)
            self._daily_cfg = cfg
            self.daily_last_label.setText(f"上次执行：{day}")

    def _compose_and_push(self, rep, cfg, ai_cfg, day, auto=False):
        """AI 点评 → 组装文档 → 落盘 → 推送（v0.10.4，全部在后台线程）。"""
        def _work():
            doc = rep["markdown"]
            lines = list(rep["feishu_lines"])

            # ---- AI 点评（失败不影响后续）----
            if ai_cfg.enabled:
                if not ai_cfg.is_configured():
                    self.daily_log_line.emit("  ✗ 已启用 AI 点评但未配置完整，跳过")
                else:
                    self.daily_log_line.emit(f"  AI 生成点评中（{ai_cfg.model}）…")
                    try:
                        ok, text, info = ai_summary.summarize(
                            rep, ai_cfg, db=getattr(self, "db", None))
                    except Exception as e:  # noqa: BLE001
                        ok, text, info = False, f"调用异常: {e}", {}
                    if ok and text:
                        doc = doc.rstrip() + "\n\n## 五、AI 点评\n\n" + text + "\n"
                        lines = (["【AI 点评】"] + text.splitlines() + [""] + lines)
                        self.daily_log_line.emit(
                            f"  ✓ AI 点评完成（{ai_summary.format_info(info)}）")
                    else:
                        self.daily_log_line.emit(f"  ✗ AI 点评失败：{text}")

            # ---- 落盘 ----
            if cfg.save_local:
                try:
                    path = save_report(day, doc)
                    self.daily_log_line.emit(f"  文档：{path}")
                except Exception as e:  # noqa: BLE001
                    log.warning("保存日报文档失败: %s", e)
                    self.daily_log_line.emit(f"  ✗ 保存文档失败：{e}")

            # ---- 手动模式：把最终文档弹出来 ----
            if not auto:
                self.daily_doc_ready.emit(doc)

            # ---- 推送 ----
            creds = cfg.credentials()
            if not is_configured(creds):
                self.daily_log_line.emit("✗ 飞书推送凭证没填全，跳过推送（文档已生成）")
                return
            self.daily_log_line.emit(f"  推送飞书 {describe(creds)} …")
            try:
                ok, msg = send_message(creds, rep["title"], lines=lines)
            except Exception as e:  # noqa: BLE001
                ok, msg = False, f"推送异常: {e}"
            self.report_sent.emit(ok, msg)

        threading.Thread(target=_work, daemon=True).start()

    def _on_report_sent(self, ok, msg):
        self._daily_log(("✓ " if ok else "✗ ") + msg)
        self.statusBarHint("飞书推送成功" if ok else f"飞书推送失败：{msg}")
        try:
            self.db.set_setting("daily_report_last_status",
                                f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} "
                                f"{'OK' if ok else 'FAIL'} {msg}")
        except Exception as e:  # noqa: BLE001
            log.debug("写入推送状态失败: %s", e)

    def _test_ai(self):
        """测试 AI 连通性。"""
        cfg = self._collect_ai_cfg()
        if not cfg.is_configured():
            QMessageBox.information(
                self, "提示", "AI 需要填 Base URL / API Key / 模型名")
            return
        self._daily_log(f"→ 测试 AI（{cfg.model} @ {cfg.base_url}）…")

        def _work():
            try:
                ok, msg = ai_summary.test_ai(cfg)
            except Exception as e:  # noqa: BLE001
                ok, msg = False, f"调用异常: {e}"
            self.daily_log_line.emit(("✓ " if ok else "✗ ") + msg)

        threading.Thread(target=_work, daemon=True).start()

    def _test_feishu(self):
        creds = self._feishu_creds()
        if not is_configured(creds):
            QMessageBox.information(
                self, "提示",
                "推送凭证没填全：\n"
                "· 应用机器人：App ID / App Secret / 接收方\n"
                "· 自定义机器人：Webhook 地址")
            return
        self._daily_log(f"→ 测试发送（{describe(creds)}）…")

        def _work():
            try:
                ok, msg = test_message(creds)
            except Exception as e:  # noqa: BLE001
                ok, msg = False, f"推送异常: {e}"
            self.report_sent.emit(ok, msg)

        threading.Thread(target=_work, daemon=True).start()

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
