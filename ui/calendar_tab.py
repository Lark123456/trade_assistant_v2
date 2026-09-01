# -*- coding: utf-8 -*-
"""行情日历页签（v0.6.6 → v0.7.0 → v0.7.4 → v0.7.5 → v0.7.6）：
以每日为单位归档「当日选股结果」——月历视图（有选股日期淡红背景+命中数）
→ 点击日期进入当日清单，三个子菜单：
  ① 一层粗筛（当日策略公式命中，layer=1）
  ② 二层精筛（当日DNA形态精筛命中：综合得分/骨架相似度/买点定级/模具/微观诊断，layer=2）
  ③ 观察池（当日进入观察池的记录：自动进池/手动入池；显示 现价/较加入价涨跌%/
     当日涨跌%/振幅% —— 盘中每 5 秒刷新行情）

v0.7.6 改进：三个子菜单统一使用 PerformanceListWidget 组件，
一层粗筛和二层精筛 Tab 同步显示「较加入价%/当日涨跌%/振幅%/现价」表现列，
盘中每 5 秒刷新（复用 worker 全局行情信号 + 各自定时器仅在可见时请求）。

v0.7.7 改进：表头点击排序（点击切换升/降序，自动识别数值/文本列）；
底部实时统计「涨/跌/平数与占比、总盈亏（每只买 1 单位）」；
超过加入后 40 个交易日的票组自动停止刷新（统计冻结在最后一次值）。

定位：历史回溯的入口（每天选了什么、哪些策略/模具命中），不是看盘工具。
"""
import datetime
from collections import Counter

from PyQt5.QtCore import QDate, QEvent, QItemSelectionModel, QPoint, Qt, QTimer
from PyQt5.QtGui import QColor, QPainter, QPolygon, QTextCharFormat
from PyQt5.QtWidgets import (QAbstractItemView, QAction, QCalendarWidget, QCheckBox,
                             QComboBox, QDialog, QDialogButtonBox, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMenu, QMessageBox, QPushButton,
                             QStackedWidget, QTableView, QTabWidget, QVBoxLayout,
                             QWidget)

from data_feed import infer_market, is_trading_time
from ui.models import PandasModel
from ui.hit_list import _compute_post_hits  # v0.8.1：10/30 日最高涨幅计算

HIT_BG = QColor("#ffd9d9")   # 淡红色：当日有选股
HIT_FG = QColor("#b03a2e")

# 一层粗筛展示列（v0.7.6 加入 现价/较加入价%/当日涨跌%/振幅%；v0.8.0 加「层级」；
# v0.8.1 "价格"→"加入价"；加 10日/30日最高涨幅%）
COARSE_PERF_COLUMNS = ["层级", "来源", "策略", "代码", "名称", "加入价", "现价",
                       "较加入价%", "10日最高涨幅%", "30日最高涨幅%",
                       "当日涨跌%", "振幅%", "时间", "原因"]
# 二层精筛展示列（v0.7.6 + v0.8.0 层级 + v0.8.1 10/30 日最高涨幅）
DNA_PERF_COLUMNS = ["序号", "层级", "股票代码", "股票名称", "综合得分", "骨架相似度",
                    "买点定级", "最佳匹配模具", "加入价", "现价", "较加入价%",
                    "10日最高涨幅%", "30日最高涨幅%",
                    "当日涨跌%", "振幅%", "微观特征诊断"]
# 观察池表现列（v0.7.5 + v0.8.0 层级 + v0.8.1 10/30 日最高涨幅）
WATCH_PERF_COLUMNS = ["层级", "来源", "代码", "名称", "加入价", "现价", "较加入价%",
                      "10日最高涨幅%", "30日最高涨幅%",
                      "当日涨跌%", "振幅%", "原因"]
# 买点定级高亮：SB1 醒目红/橙，B1 蓝色（注意 SB1 子串优先于 B1）
BUY_LEVEL_COLORS = {"SB1核心买点": "#c0392b", "B1底仓买点": "#1f6feb"}

# v0.8.0：每列哪些启用"表头筛选"（Excel 风格下拉箭头）。仅这些列点击时
# 弹筛选菜单，其他列点击按排序处理。
COARSE_FILTERABLE = {"策略", "来源", "层级"}
DNA_FILTERABLE = {"最佳匹配模具", "买点定级", "层级"}
WATCH_FILTERABLE = {"来源", "层级"}

QUOTE_REFRESH_MS = 5000   # 盘中行情刷新间隔（5 秒）
# 加入日后的统计追踪窗口（个交易日），超出后不再实时更新（统计冻结）
STATS_MAX_TRADING_DAYS = 40

# 筛选箭头大小（像素）
FILTER_ARROW_W = 18
FILTER_ARROW_PAD = 2


class FilterableHeaderView(QHeaderView):
    """自定义表头：每节右侧画筛选下拉箭头（Excel 风格）。

    - set_filterable_columns(columns: set[str]) 启用哪些列支持筛选
      其他列保持默认行为（点击排序）
    - 整个表头单元格任意位置点击 → 弹筛选菜单（多选 + 搜索 + 全选/清空）
    - 当前激活的筛选会在箭头处填充三角（标识有筛选生效）
    - 排序箭头（▲/▼）保留原生绘制，位于节左侧

    v0.8.1 改动：之前箭头只有 14px 宽，UX 不友好（用户找不到入口）。
    改为整个表头单元格都可点击触发筛选，更符合"按钮"直觉。
    """

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self._model = None  # PandasModel
        self._filterable = set()  # 启用了筛选的列名集合
        self._active = set()  # 当前有筛选生效的列名（视觉标识）
        self.setSectionsClickable(True)
        self.setHighlightSections(True)
        # 设为非默认的 selectionModel 也不影响
        self.setStretchLastSection(True)

    def attach_model(self, model):
        """绑定 PandasModel，用于查询每列候选值 + 设置筛选。"""
        self._model = model

    def set_filterable_columns(self, cols):
        self._filterable = set(cols)
        self.viewport().update()

    def refresh_active(self):
        """重算当前激活的列（用于画箭头填充）。"""
        if self._model is None:
            self._active = set()
        else:
            self._active = {c for c, v in self._model.get_active_filters().items() if v}
        self.viewport().update()

    # ------------------------------------------------------------------ paint
    def _col_name(self, logicalIndex):
        """安全获取列名（原始 df，按视图列号 → 真实列号）。"""
        if self._model is None:
            return None
        # 视图的 logicalIndex 就是 model 的列号（model._df.columns）
        cols = self._model.dataframe(False).columns
        if 0 <= logicalIndex < len(cols):
            return cols[logicalIndex]
        return None

    def paintSection(self, painter, rect, logicalIndex):
        super().paintSection(painter, rect, logicalIndex)
        if self._model is None:
            return
        col_name = self._col_name(logicalIndex)
        if col_name is None or col_name not in self._filterable:
            return
        # 在节右侧画筛选箭头（▼ / 填充的 ▼）
        cx = rect.right() - FILTER_ARROW_W // 2 - FILTER_ARROW_PAD
        cy = rect.center().y()
        if col_name in self._active:
            painter.setBrush(QColor("#3c78f6"))
            painter.setPen(QColor("#3c78f6"))
        else:
            painter.setBrush(QColor("#888"))
            painter.setPen(QColor("#888"))
        painter.drawPolygon(QPolygon([
            QPoint(cx - 4, cy - 2),
            QPoint(cx + 4, cy - 2),
            QPoint(cx, cy + 4),
        ]))

    # ------------------------------------------------------------------ mouse
    def mousePressEvent(self, event):
        """v0.8.1：整个表头单元格任意位置点击 → 弹筛选菜单（Excel 风格）。

        设计要点：箭头只有 14px，必须精确点击才能触发——UX 不友好。
        改为整个节可点击触发筛选，同时原生排序行为也保留：
        - 在筛选列上单击 → 弹筛选菜单（多选 + 搜索 + 全选/清空）
        - 长按或拖动 → 走原生排序（QHeaderView 默认行为，由 sectionsClickable 控制）
        """
        idx = self.logicalIndexAt(event.pos().x())
        if idx < 0:
            super().mousePressEvent(event)
            return
        if self._model is None:
            super().mousePressEvent(event)
            return
        col_name = self._col_name(idx)
        if col_name is None or col_name not in self._filterable:
            super().mousePressEvent(event)  # 默认行为（排序）
            return
        # 整个表头单元格任意位置都触发筛选（更易点击）
        self._show_filter_menu(idx, col_name, event.globalPos())
        event.accept()

    def _show_filter_menu(self, col_idx, col_name, global_pos):
        if self._model is None:
            return
        all_vals = self._model.get_unique_values(col_name)
        current = self._model.get_column_filter(col_name)  # set | None

        dlg = _FilterMenuDialog(col_name, all_vals, current, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            new_filter = dlg.result_values()  # set | None
            if new_filter is None or len(new_filter) == 0:
                self._model.set_column_filter(col_name, None)
            else:
                self._model.set_column_filter(col_name, new_filter)
            self.refresh_active()


class _FilterMenuDialog(QDialog):
    """筛选弹窗：搜索框 + 全选/清空 + 值列表多选 + 确定。

    result_values() 返回 set[str] 或 None（None = 不过滤）。
    """

    def __init__(self, col_name, all_values, current, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"筛选：{col_name}")
        self.resize(280, 360)
        v = QVBoxLayout(self)
        v.addWidget(QLabel(f"列 [{col_name}]：仅勾选的值可见"))

        # 搜索框
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索…")
        v.addWidget(self.search_edit)

        # 全选/清空按钮
        btns = QHBoxLayout()
        sel_all = QPushButton("全选")
        sel_none = QPushButton("清空")
        btns.addWidget(sel_all)
        btns.addWidget(sel_none)
        btns.addStretch(1)
        v.addLayout(btns)

        # 值列表
        self.listw = QListWidget()
        for val in all_values:
            item = QListWidgetItem(val if val else "（空）")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(
                Qt.Checked if (current is None or val in current) else Qt.Unchecked
            )
            self.listw.addItem(item)
        v.addWidget(self.listw, stretch=1)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        v.addWidget(bb)

        sel_all.clicked.connect(lambda: self._set_all(Qt.Checked))
        sel_none.clicked.connect(lambda: self._set_all(Qt.Unchecked))
        self.search_edit.textChanged.connect(self._apply_search)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)

    def _set_all(self, state):
        for i in range(self.listw.count()):
            item = self.listw.item(i)
            if not item.isHidden():
                item.setCheckState(state)

    def _apply_search(self, text):
        text = (text or "").lower()
        for i in range(self.listw.count()):
            item = self.listw.item(i)
            item.setHidden(text not in item.text().lower())

    def result_values(self):
        """返回勾选的值集合。若全部勾选 → 返回 None（不过滤，最高效）。"""
        checked = set()
        for i in range(self.listw.count()):
            item = self.listw.item(i)
            if item.checkState() == Qt.Checked and not item.isHidden():
                v = item.text()
                if v == "（空）":
                    v = ""
                checked.add(v)
        total_visible = sum(1 for i in range(self.listw.count()) if not self.listw.item(i).isHidden())
        total_checked = sum(1 for i in range(self.listw.count())
                            if self.listw.item(i).checkState() == Qt.Checked and not self.listw.item(i).isHidden())
        if total_checked == total_visible and total_visible > 0:
            return None  # 全选 = 不过滤
        return checked if checked else None


class HitCalendar(QCalendarWidget):
    """月历：有选股日期淡红背景 + 单元格右下角命中数 + 悬停摘要。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hit_info = {}   # QDate -> (命中数, 手动数)

    def set_hit_info(self, info):
        self._hit_info = info or {}
        fmt = QTextCharFormat()
        fmt.setBackground(HIT_BG)
        for d in self._hit_info:
            self.setDateTextFormat(d, fmt)
        self.updateCells()

    def paintCell(self, painter: QPainter, rect, date):
        super().paintCell(painter, rect, date)
        n = self._hit_info.get(date)
        if n:
            hits, manual = n
            painter.save()
            painter.setPen(HIT_FG)
            font = painter.font()
            font.setPointSize(8)
            painter.setFont(font)
            painter.drawText(rect.adjusted(0, 0, -2, 2),
                             Qt.AlignRight | Qt.AlignBottom, str(hits + manual))
            painter.restore()

    def event(self, e):
        if e.type() == QEvent.ToolTip:
            pos = e.pos()
            state, date = self.hitTest(pos)
            if state == self.HitDate and date in self._hit_info:
                hits, manual = self._hit_info[date]
                self.setToolTip(
                    f"{date.toString('yyyy-MM-dd')}：策略命中 {hits} 只 / 手动入池 {manual} 只")
                return True
            self.setToolTip("")
        return super().event(e)


class PerformanceListWidget(QWidget):
    """统一表现列表（v0.7.6）：行情日历三个 Tab 共用。

    行为：
    - 表头列由 columns 决定（包含「代码」「名称」与业务列，及表现列「现价/较加入价%/
      当日涨跌%/振幅%」）；行数据由调用方预先组装（含 base_price）。
    - 「较加入价%」 = (现价 ÷ base_price − 1) × 100（base_price 由调用方提供：
      观察池 = add_date 当日收盘；粗筛/精筛 = 命中日 close，即 scan_history.price）。
    - 「当日涨跌%」「振幅%」盘中取实时行情；非交易时段回退最近日K
      （database.get_latest_bar_snapshot）。
    - 行情刷新：监听 worker.quotes_ready 合并 → 原地刷新（保留选中行）；
      同时 QTimer 5s 在 Tab 可见时主动 request_quotes 兜底（覆盖已被删出池的历史票）。
    """

    def __init__(self, worker, db, columns, on_watch_added=None,
                 fallback_to_base=False, parent=None):
        """columns: 显示列定义（必含「代码」「名称」+ 业务列；表现列 现价/较加入价%/
        当日涨跌%/振幅% 由本组件自动追加）。

        fallback_to_base: 【已废弃 v0.8.1】保留参数仅作向后兼容，行为已统一——
        无实时行情时一律回退到 `db.get_latest_close()`（最新日K收盘），
        让"较加入价%"真实反映"加入价 → 现在"的涨跌幅（而不是恒为 0%）。
        """
        super().__init__(parent)
        self.worker = worker
        self.db = db
        self.columns = list(columns)
        self.on_watch_added = on_watch_added
        self.fallback_to_base = fallback_to_base
        self._day = None
        self._rows = []
        self._quotes = {}
        self._base_prices = {}
        self._fallback_prices = {}
        self._fallback_snap = {}
        self._watch_codes = {str(w["code"]) for w in self.db.list_watch()}
        self._build_ui()
        self.timer = QTimer(self)
        self.timer.setInterval(QUOTE_REFRESH_MS)
        self.timer.timeout.connect(self._on_timer)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        rules = {"买点定级": [(tag, color) for tag, color in BUY_LEVEL_COLORS.items()]}
        # v0.8.1：百分点列全部支持红涨绿跌着色（含 10/30 日最高涨幅）
        color_cols = [c for c in ("较加入价%", "10日最高涨幅%", "30日最高涨幅%",
                                   "当日涨跌%") if c in self.columns]
        self.model = PandasModel(None, color_cols=color_cols, text_color_rules=rules)
        self.table = QTableView()
        # v0.8.0：自定义表头（点击箭头弹筛选菜单 Excel 风格）
        self.header = FilterableHeaderView(self.table)
        self.table.setHorizontalHeader(self.header)
        self.table.setModel(self.model)
        self.header.attach_model(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.doubleClicked.connect(lambda _i: self._view_chart())
        # v0.7.7：点击表头切换升/降序（PandasModel.sort 支持数值/文本自动识别）
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSortIndicatorShown(True)
        root.addWidget(self.table, stretch=1)
        bar = QHBoxLayout()
        chart_btn = QPushButton("查看K线")
        chart_btn.clicked.connect(self._view_chart)
        watch_btn = QPushButton("加入观察池")
        watch_btn.clicked.connect(self._add_watch)
        # v0.8.0：删除按钮（从当前列表中删除，可作用于粗筛/精筛 Tab 删除命中记录，
        # 或观察池 Tab 从观察池移除）
        del_btn = QPushButton("🗑️ 删除")
        del_btn.setToolTip("从当前列表移除选中行（粗筛/精筛 = 删除命中；观察池 = 移除观察）")
        del_btn.clicked.connect(self._delete_selected_rows)
        # v0.8.3：通达信自选股同步按钮（粗筛/精筛/观察池 Tab 都可导出当前列表）
        tdx_btn = QPushButton("🔄 通达信")
        tdx_btn.setToolTip("导出当前 Tab 可见行到通达信自定义板块（在通达信中浏览 K 线）；"
                            "或从通达信板块导入新观察对象")
        tdx_btn.clicked.connect(self._open_tdx_sync)
        bar.addWidget(chart_btn)
        bar.addWidget(watch_btn)
        bar.addWidget(del_btn)
        bar.addWidget(tdx_btn)
        bar.addStretch(1)
        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color:#666;")
        bar.addWidget(self.count_label)
        root.addLayout(bar)
        # 子类（Tab）需设置此集合启用筛选箭头列；空集合 = 不画筛选箭头
        self.filterable_columns = set()

    # ------------------------------------------------------------ 数据
    def set_rows(self, rows, day=None):
        """rows: 行 dict 列表。每行至少含 code/name/base_price/其他业务列。

        base_price 来源建议：
        - 观察池：db.get_close_on_date(code, add_date) or entry_price
        - 粗筛/精筛：scan_history.price（命中日收盘）
        """
        self._day = day
        self._deadline = None    # 重置 40 天统计追踪窗口（下次 _render 重算）
        self._expired = False
        self._rows = list(rows)
        self._watch_codes = {str(w["code"]) for w in self.db.list_watch()}
        self._base_prices = {}
        self._fallback_prices = {}
        self._fallback_snap = {}
        # v0.8.1：预取每只股票的 daily_bars + 算 10/30 日最高涨幅（缓存，不随实时刷新变）
        self._bars_cache = {}
        self._post_cache = {}  # {code: {current_pct, max_pct_10, max_pct_30}}
        for r in self._rows:
            code = str(self._row_get(r, "代码") or self._row_get(r, "股票代码") or "")
            if not code:
                continue
            self._base_prices[code] = r.get("base_price")
            # v0.8.1 bugfix：fallback 一律用最新日K收盘（不再 fallback 到加入价）
            # 原逻辑 `fallback_to_base=True` 会让历史票的"现价"永远=加入价，
            # 导致"较加入价%"恒为 0%、"10/30 日最高涨幅"被隐藏。
            # 正确逻辑：现价 = 加入日之后的最新日K收盘（或盘中实时行情）。
            self._fallback_prices[code] = self.db.get_latest_close(code)
            # 涨跌/振幅快照（无论 fallback 策略如何，都从最近日K兜底）
            self._fallback_snap[code] = self.db.get_latest_bar_snapshot(code)
            # v0.8.1：每日 bars 预取（一次）+ 算 10/30 日最高涨幅
            try:
                bars = self.db.get_cached_daily_bars(code)
                self._bars_cache[code] = bars
                sig = (r.get("signal_date") or r.get("add_date") or self._day)
                perf = _compute_post_hits(bars, sig)
                self._post_cache[code] = perf
            except Exception:
                self._post_cache[code] = {"current_pct": None, "max_pct_10": None,
                                           "max_pct_30": None, "status": "ERROR"}
        self._render()
        if rows and not self._expired:
            self.timer.start()
        else:
            self.timer.stop()

    def stop_refresh(self):
        """返回月历视图时停止定时刷新。"""
        self.timer.stop()

    def on_quotes(self, df):
        """worker.quotes_ready 转调：合并当日股票行情并原地刷新表格。"""
        if df is None or df.empty:
            return
        day_codes = {str(self._row_get(r, "代码") or self._row_get(r, "股票代码") or "")
                     for r in self._rows if self._row_get(r, "代码") or self._row_get(r, "股票代码")}
        changed = False
        for r in df.itertuples():
            d = r._asdict()
            code = str(d.get("code", ""))
            if code in day_codes:
                if self._quotes.get(code) != d:
                    self._quotes[code] = d
                    changed = True
        if changed:
            self._render(preserve_selection=True)

    def _on_timer(self):
        """每 5 秒：盘中请求当日股票实时行情（非交易时段不刷网络）。"""
        if not self._day or not self.isVisible():
            return
        codes = [(infer_market(c), c) for c in self._base_prices if c]
        if not codes:
            return
        if is_trading_time():
            if hasattr(self.worker, "request_quotes"):
                self.worker.request_quotes(codes)

    # ------------------------------------------------------------ 渲染
    @staticmethod
    def _row_get(r, col):
        """列名 → 行 dict 取值。处理 code/name 列在不同表中的别名。"""
        if col in r:
            return r[col]
        if col in ("代码", "股票代码") and "code" in r:
            return r["code"]
        if col in ("名称", "股票名称") and "name" in r:
            return r["name"]
        return r.get(col, "")

    def _render(self, preserve_selection=False):
        import pandas as pd
        from pandas.tseries.offsets import BDay
        # ---- 40 个交易日过期判断：超过后停止刷新，统计冻结 ----
        if self._day:
            try:
                self._deadline = (pd.Timestamp(self._day) + BDay(STATS_MAX_TRADING_DAYS)).date()
            except Exception:
                self._deadline = None
        today = datetime.date.today()
        self._expired = bool(self._deadline and today > self._deadline)
        if self._expired and self.timer.isActive():
            self.timer.stop()

        sel_codes = set()
        if preserve_selection:
            df0 = self.table.model().dataframe()
            for idx in self.table.selectionModel().selectedRows():
                if 0 <= idx.row() < len(df0):
                    sel_codes.add(str(df0.iloc[idx.row()].get("代码", "")))
        out = []
        for r in self._rows:
            code = str(self._row_get(r, "代码") or self._row_get(r, "股票代码") or "")
            base = self._base_prices.get(code)
            q = self._quotes.get(code)
            price = None
            if q and q.get("price"):
                price = float(q["price"])
            elif not q:
                price = self._fallback_prices.get(code)
            pct_base = (price / base - 1) * 100 if (price and base) else None
            day_pct = q.get("pct") if q else None
            amp = None
            if q and q.get("high") and q.get("low") and q.get("last_close"):
                amp = (float(q["high"]) - float(q["low"])) / float(q["last_close"]) * 100
            if day_pct is None and not q:
                snap = self._fallback_snap.get(code)
                if snap and snap.get("prev_close"):
                    day_pct = (snap["close"] / snap["prev_close"] - 1) * 100
                    amp = (snap["high"] - snap["low"]) / snap["prev_close"] * 100
            row_out = {col: self._row_get(r, col) for col in self.columns}
            # v0.8.1：从预取的 _post_cache 取 10/30 日最高涨幅（不随实时刷新变）
            perf = self._post_cache.get(code, {})
            row_out.update({
                "现价": (round(price, 2) if price is not None else None),
                "较加入价%": (round(pct_base, 2) if pct_base is not None else None),
                "10日最高涨幅%": (round(perf.get("max_pct_10"), 2)
                                if perf.get("max_pct_10") is not None else None),
                "30日最高涨幅%": (round(perf.get("max_pct_30"), 2)
                                if perf.get("max_pct_30") is not None else None),
                "当日涨跌%": (round(float(day_pct), 2) if day_pct is not None else None),
                "振幅%": (round(amp, 2) if amp is not None else None),
            })
            out.append(row_out)
        df = pd.DataFrame(out, columns=self.columns)
        self.table.model().setDataFrame(df)

        # ---- 实时统计：按筛选后的可见行统计涨跌概率 + 总盈亏（每只买 1 单位 = Σ(现价 - 加入价)） ----
        # v0.8.1 改进：用户通过表头筛选（如只筛 "B1海选"）后，底部统计按筛选后的结果
        # 计算，不再统计全局。筛选为空或全选时等效统计全部。
        model = self.table.model()
        if model._visible_idx is None:
            visible_idx = list(range(len(model._df)))
        else:
            visible_idx = model._visible_idx

        up = down = flat = 0
        total_pnl = 0.0
        total_with_pnl = 0
        for orig_row in visible_idx:
            # model._df 已按 sort 排序；orig_row 是排序后的行索引（指向原始数据）
            base_code = str(model._df.iloc[orig_row].get("代码", "") or
                            model._df.iloc[orig_row].get("股票代码", "") or "")
            base = self._base_prices.get(base_code)
            price = model._df.iloc[orig_row].get("现价")
            if price is None or base is None or base == 0:
                continue
            try:
                price = float(price)
            except (ValueError, TypeError):
                continue
            pct = price / base - 1
            total_with_pnl += 1
            if pct > 1e-6:
                up += 1
            elif pct < -1e-6:
                down += 1
            else:
                flat += 1
            total_pnl += (price - base)

        # ---- 底部状态栏 ----
        if self._expired:
            mode = f"已超过加入后{STATS_MAX_TRADING_DAYS}个交易日（统计冻结）"
        elif is_trading_time():
            mode = "盘中实时刷新（5秒）"
        else:
            mode = "收盘价（非交易时段）"
        stats = ""
        if total_with_pnl > 0:
            pct_up = up / total_with_pnl * 100
            sign = "+" if total_pnl >= 0 else ""
            stats = (f"  |  {up}涨 {down}跌 {flat}平 ({pct_up:.1f}%)"
                     f"  |  总盈亏(每只1单位) {sign}{total_pnl:.2f} 元")
        # v0.8.1：显示筛选后的可见行数（筛选前总数如果筛选为空 = 显示全部）
        visible_count = model.rowCount()
        if visible_count != len(out):
            self.count_label.setText(
                f"共 {visible_count} / {len(out)} 只 · {mode}{stats}")
        else:
            self.count_label.setText(f"共 {len(out)} 只 · {mode}{stats}")

        if preserve_selection and sel_codes:
            sm = self.table.selectionModel()
            code_col_idx = self.columns.index("代码") if "代码" in self.columns else 0
            for i in range(len(df)):
                if str(df.iloc[i].get("代码", "")) in sel_codes:
                    sm.select(self.table.model().index(i, code_col_idx),
                              QItemSelectionModel.Select | QItemSelectionModel.Rows)
        # v0.8.0：刷新表头筛选箭头（激活状态）
        if hasattr(self, "header"):
            self.header.refresh_active()

    # ------------------------------------------------------------ 行操作
    def _selected_rows(self):
        df = self.table.model().dataframe()
        if df is None or df.empty:
            return []
        idxs = self.table.selectionModel().selectedRows()
        if not idxs:
            cur = self.table.currentIndex()
            if cur.isValid():
                idxs = [cur]
        return [df.iloc[i.row()] for i in idxs if 0 <= i.row() < len(df)]

    def _view_chart(self):
        recs = self._selected_rows()
        if not recs:
            return
        rec = recs[0]
        from ui.chart_widget import ChartDialog
        code_col = "代码" if "代码" in rec.index else "股票代码"
        name_col = "名称" if "名称" in rec.index else "股票名称"
        code = str(rec[code_col])
        ChartDialog(code, str(rec[name_col]), infer_market(code),
                    self.db, self).exec_()

    def _add_watch(self):
        """批量加入观察池；重复已在池提示；已入池则更新 DNA 标注。"""
        recs = self._selected_rows()
        if not recs:
            return
        from PyQt5.QtWidgets import QMessageBox
        added, skipped = 0, 0
        for rec in recs:
            code_col = "代码" if "代码" in rec.index else "股票代码"
            name_col = "名称" if "名称" in rec.index else "股票名称"
            code = str(rec[code_col])
            if code in self._watch_codes:
                skipped += 1
                continue
            self.db.add_watch(code=code, name=str(rec[name_col]),
                              market=infer_market(code),
                              trigger_indicator=str(rec.get("来源", "") or "手动加入"),
                              entry_price=(float(rec["现价"])
                                           if rec.get("现价") not in ("", None) else None),
                              reason=str(rec.get("原因", "") or "从历史选股清单加入"),
                              status="观察中",
                              dna_score=(rec.get("综合得分")
                                         if rec.get("综合得分") not in ("", None) else None),
                              dna_template=str(rec.get("最佳匹配模具", "") or "") or None)
            if self._day:
                self.db.update_scan_in_watchlist(code, day=self._day, flag=1)
            self._watch_codes.add(code)
            added += 1
        if added:
            if self.on_watch_added:
                self.on_watch_added()
            self.count_label.setText(f"已加入观察池 {added} 只")
            QMessageBox.information(self, "提示", f"已成功加入观察池：{added} 只")
        elif skipped:
            QMessageBox.information(self, "提示", "所选股票均已在观察池")

    def _delete_selected_rows(self):
        """删除选中行：粗筛/精筛 Tab = 从 scan_history 删除；观察池 Tab = 从 watch_pool 删除。

        删除语义由 tab_type 区分（调用方在 _build_ui 后通过 set_tab_type 设置）。
        """
        recs = self._selected_rows()
        if not recs:
            QMessageBox.information(self, "提示", "请先选中要删除的行")
            return
        tab_type = getattr(self, "tab_type", "coarse")
        names = ", ".join(str(r.get("代码") or r.get("股票代码") or "?") for r in recs[:3])
        if len(recs) > 3:
            names += f" 等 {len(recs)} 只"
        if tab_type == "watch":
            msg = (f"从观察池移除 {len(recs)} 只股票（含 {names}）？\n"
                   f"观察池记录与对应 scan_history.in_watchlist 会一并更新。")
        else:
            layer = 1 if tab_type == "coarse" else 2
            msg = (f"删除 {len(recs)} 条{'一层粗筛' if layer == 1 else '二层精筛'}命中记录\n"
                   f"（{names}）？\n\n⚠️ 此操作将影响：\n"
                   f"· 行情日历月历的当日命中数\n"
                   f"· 选股模块的命中历史\n"
                   f"· 自动进池的回写状态\n\n确定继续？")
        ret = QMessageBox.question(
            self, "确认删除", msg,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return

        deleted_codes = []
        for rec in recs:
            code_col = "代码" if "代码" in rec.index else "股票代码"
            code = str(rec[code_col])
            if tab_type == "watch":
                # 观察池 Tab：从 watch_pool 删除该票当日入池记录
                for w in list(self.db.list_watch()):
                    if str(w.get("code")) == code and w.get("add_date") == self._day:
                        self.db.delete_watch(w["id"])
                        break
                # v0.8.5：日历通达信导入条目一并删除（独立存储）
                if self._day:
                    self.db.delete_tdx_import(code=code, add_date=self._day)
                # 回写 scan_history.in_watchlist=0（同 day）
                if self._day:
                    self.db.update_scan_in_watchlist(code, day=self._day, flag=0)
            else:
                layer = 1 if tab_type == "coarse" else 2
                self.db.delete_scan_hit(code=code, day=self._day, layer=layer)
            deleted_codes.append(code)
        # 从本地 rows 中移除（避免下次 _render 又写回数据库）
        self._rows = [r for r in self._rows
                     if str(self._row_get(r, "代码") or self._row_get(r, "股票代码")) not in deleted_codes]
        self._watch_codes -= set(deleted_codes)
        self._render(preserve_selection=False)
        self.count_label.setText(f"已删除 {len(deleted_codes)} 只")
        # 通知 CalendarTab 刷新顶部计数 + 月历
        if self.on_watch_added:
            self.on_watch_added()

    def _open_tdx_sync(self):
        """v0.8.3：打开通达信自选股板块双向同步对话框。

        导出范围：本 Tab 当前可见行（已应用表头筛选/排序）；从本地 self._rows 取。
        导入目标（v0.8.5）：进行情日历 {self._day} 的二级观察池（tdx_imports，
        独立于一级观察池），导入后只刷新本日期视图 + 观察池月历标记。
        """
        from ui.tdx_sync_dialog import TdxSyncDialog

        day = self._day or ""

        def watch_rows_provider():
            # 用本地 _rows（_open_date 时缓存的当日清单）而非从 db 读，
            # 保证显示的列结构与 Tab 一致。
            return list(self._rows)

        def watch_codes_provider():
            # 去重判断 = 当日二级观察池可见集合：
            # watch_pool(add_date=day) ∪ tdx_imports(add_date=day)
            codes = {str(w["code"]) for w in self.db.list_watch()
                     if (w.get("add_date") or "") == day}
            codes |= {str(t["code"]) for t in self.db.list_tdx_imports(add_date=day)}
            return codes

        tab_label = {"coarse": "一层粗筛", "dna": "二层精筛",
                     "watch": "观察池"}.get(getattr(self, "tab_type", "coarse"),
                                           self.tab_type)

        def _on_imported():
            # 导入后只刷新本日期视图（父级 CalendarTab 会刷新月历标记）
            if self.on_watch_added:
                self.on_watch_added()
            # 本 Tab 自己也重绘（如果有导入的票在当前 _rows）
            self._render(preserve_selection=False)

        QMessageBox.information(
            self, "通达信同步",
            f"将在「{tab_label}」Tab 的当前可见行（共 {len(self._rows)} 只）与通达信同步。\n"
            f"导出范围=当前筛选后的行；导入将进入行情日历 {day or '今日'} 的二级观察池"
            f"（独立于一级观察池）。")

        dlg = TdxSyncDialog(
            self.db, self.worker,
            watch_codes_provider=watch_codes_provider,
            watch_rows_provider=watch_rows_provider,
            on_imported=_on_imported,
            parent=self,
            dest=f"calendar:{day}" if day else "watch_pool")
        dlg.exec_()


class CalendarTab(QWidget):
    """行情日历页签：第一级月历视图 ↔ 第二级当日清单（叠层切换）。"""

    def __init__(self, worker, db, on_watch_added=None, parent=None):
        super().__init__(parent)
        self.worker = worker
        self.db = db
        self.on_watch_added = on_watch_added
        self._current_qdate = None   # 当前打开的日期（观察池变动后用于刷新计数）
        self._build_ui()
        self._reload_month()
        self.calendar.currentPageChanged.connect(lambda _y, _m: self._reload_month())
        # v0.7.5/0.7.6：复用全局行情信号，三个 Tab 共用刷新源（合并而非覆盖）
        if hasattr(self.worker, "quotes_ready"):
            self.worker.quotes_ready.connect(self._on_quotes)

    def _on_quotes(self, df):
        """全局行情就绪 → 三个 Tab 表现列同步更新（合并语义，无重复请求）。"""
        self.coarse_list.on_quotes(df)
        self.dna_list.on_quotes(df)
        self.watch_list.on_quotes(df)

    def _build_ui(self):
        root = QVBoxLayout(self)
        self.stack = QStackedWidget()

        # ---------------- 第一级：月历视图 ----------------
        page_cal = QWidget()
        cv = QVBoxLayout(page_cal)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("◆ 选股日历（淡红 = 当日有选股；点日期查看当日清单）"))
        bar.addStretch(1)
        today_btn = QPushButton("回到今天")
        today_btn.clicked.connect(self._go_today)
        bar.addWidget(today_btn)
        cv.addLayout(bar)
        self.calendar = HitCalendar()
        self.calendar.setGridVisible(True)
        cv.addWidget(self.calendar, stretch=1)
        self.month_label = QLabel("")
        self.month_label.setStyleSheet("color:#666;padding:4px;")
        cv.addWidget(self.month_label)
        page_cal.setLayout(cv)

        # ---------------- 第二级：当日清单（三子菜单） ----------------
        page_list = QWidget()
        lv = QVBoxLayout(page_list)
        lbar = QHBoxLayout()
        back_btn = QPushButton("‹ 返回日历")
        back_btn.clicked.connect(self._back_to_calendar)
        lbar.addWidget(back_btn)
        self.date_label = QLabel("")
        self.date_label.setStyleSheet("font-weight:bold;")
        lbar.addWidget(self.date_label)
        # v0.8.1：移除顶部"方向筛选"下拉框——已被表头筛选菜单取代
        # （点击"策略"列表头任意位置弹菜单多选策略）
        lbar.addStretch(1)
        lv.addLayout(lbar)

        self.day_tabs = QTabWidget()
        # v0.8.1 bugfix：fallback 一律用最新日K（不再 fallback 到加入价）
        # 原 fallback_to_base=True 对历史票会让"现价"永远=加入价，掩盖真实涨跌。
        # 一层粗筛/二层精筛/观察池三个 Tab 都用最新日K兜底（观察池原本就是这逻辑）。
        self.coarse_list = PerformanceListWidget(
            self.worker, self.db, columns=COARSE_PERF_COLUMNS,
            on_watch_added=self._on_day_watch_added,
            fallback_to_base=True)  # 参数已废弃，行为统一为最新日K
        self.coarse_list.tab_type = "coarse"
        self.coarse_list.filterable_columns = COARSE_FILTERABLE
        self.coarse_list.header.set_filterable_columns(COARSE_FILTERABLE)
        self.day_tabs.addTab(self.coarse_list, "一层粗筛")
        self.dna_list = PerformanceListWidget(
            self.worker, self.db, columns=DNA_PERF_COLUMNS,
            on_watch_added=self._on_day_watch_added,
            fallback_to_base=True)  # 参数已废弃
        self.dna_list.tab_type = "dna"
        self.dna_list.filterable_columns = DNA_FILTERABLE
        self.dna_list.header.set_filterable_columns(DNA_FILTERABLE)
        self.day_tabs.addTab(self.dna_list, "二层精筛")
        self.watch_list = PerformanceListWidget(
            self.worker, self.db, columns=WATCH_PERF_COLUMNS,
            on_watch_added=self._on_day_watch_added,
            fallback_to_base=False)
        self.watch_list.tab_type = "watch"
        self.watch_list.filterable_columns = WATCH_FILTERABLE
        self.watch_list.header.set_filterable_columns(WATCH_FILTERABLE)
        self.day_tabs.addTab(self.watch_list, "观察池")
        lv.addWidget(self.day_tabs, stretch=1)
        page_list.setLayout(lv)

        self.stack.addWidget(page_cal)
        self.stack.addWidget(page_list)
        root.addWidget(self.stack)
        self.calendar.clicked.connect(self._open_date)

    def _back_to_calendar(self):
        """返回月历：停止三 Tab 行情定时器。"""
        for w in (self.coarse_list, self.dna_list, self.watch_list):
            w.stop_refresh()
        self.stack.setCurrentIndex(0)

    # ------------------------------------------------------------ 计数联动
    def _on_day_watch_added(self):
        """观察池变动后：刷新观察池页签 + 重载当日清单（顶部计数精准同步）。"""
        if self.on_watch_added:
            self.on_watch_added()
        if self._current_qdate is not None:
            self._open_date(self._current_qdate)

    # ------------------------------------------------------------ 数据
    def _reload_month(self):
        year = self.calendar.yearShown()
        month = self.calendar.monthShown()
        hits = Counter(self.db.list_scan_dates(year, month))
        manual = Counter()
        for w in self.db.list_watch():
            d = w.get("add_date") or ""
            if d.startswith(f"{year:04d}-{month:02d}"):
                manual[d] += 1
        # v0.8.5：通达信日历导入也计入当日观察池标记
        tdx = Counter()
        for t in self.db.list_tdx_imports():
            d = t.get("add_date") or ""
            if d.startswith(f"{year:04d}-{month:02d}"):
                tdx[d] += 1
        info = {}
        for d in set(hits) | set(manual) | set(tdx):
            info[QDate.fromString(d, "yyyy-MM-dd")] = (
                hits.get(d, 0), manual.get(d, 0) + tdx.get(d, 0))
        self.calendar.set_hit_info(info)
        self.month_label.setText(
            f"{year}年{month}月：有选股 {len(info)} 天，共命中 {sum(hits.values())} 条 / "
            f"入池 {sum(manual.values()) + sum(tdx.values())} 条"
            f"（通达信导入 {sum(tdx.values())} 条）")

    def _go_today(self):
        self.calendar.setSelectedDate(QDate.currentDate())
        self.calendar.setCurrentPage(QDate.currentDate().year(),
                                     QDate.currentDate().month())

    def _open_date(self, date):
        d = date.toString("yyyy-MM-dd")
        self._current_qdate = date

        # v0.8.1：direction 字段已废弃，不再按 direction 过滤；
        # 用户通过表头"策略"列筛选（FilterableHeaderView 自定义菜单多选策略）。

        # ① 一层粗筛（v0.7.6：base_price=命中日 close=scan_history.price）
        coarse_rows = []
        for h in self.db.list_scan_history(day=d):
            if h.get("layer", 1) != 1:
                continue
            # 提取 tags（来自 strategy；若无关联则为空）
            tags = ""
            if h.get("strategy_name"):
                st = self.db.list_strategies()
                for s in st:
                    if s.get("name") == h["strategy_name"]:
                        tags = s.get("tags", "") or ""
                        break
            coarse_rows.append({
                "来源": "策略命中", "策略": h.get("strategy_name") or "-",
                "代码": h.get("code"), "名称": h.get("name"),
                "加入价": h.get("price"),                   # 命中日收盘 = 加入价（v0.8.1:列名从"价格"改"加入价"，更直观）
                "base_price": h.get("price"),
                "signal_date": h.get("trade_date") or h.get("signal_date") or d,
                "时间": (h.get("scan_time") or "")[11:19],
                "原因": h.get("reason") or "",
                "层级": f"L{h.get('level', 1)}{'·' + tags if tags else ''}",
            })
        n_coarse = len(coarse_rows)
        self.coarse_list.set_rows(coarse_rows, day=d)

        # ② 二层精筛（v0.7.6：按综合得分降序；base_price=命中日 close=scan_history.price）
        dna_rows = []
        best_by_code = {}
        for h in self.db.list_scan_history(day=d):
            if h.get("layer", 1) != 2:
                continue
            fs = (h.get("final_score") if h.get("final_score") is not None
                  else h.get("dna_score") or 0.0)
            code = h.get("code")
            if code in best_by_code and fs <= best_by_code[code][0]:
                continue
            # 提取 tags（来自 strategy）
            tags = ""
            if h.get("strategy_name"):
                st = self.db.list_strategies()
                for s in st:
                    if s.get("name") == h["strategy_name"]:
                        tags = s.get("tags", "") or ""
                        break
            best_by_code[code] = (fs, {
                "code": code, "name": h.get("name"),
                "综合得分": fs,
                "骨架相似度": (h.get("dtw_score") if h.get("dtw_score") is not None
                              else h.get("dna_score")),
                "买点定级": h.get("buy_level") or "",
                "最佳匹配模具": h.get("matched_template") or h.get("dna_template") or "",
                "加入价": h.get("price"),                 # v0.8.1：命中日收盘 = 加入价
                "base_price": h.get("price"),
                "signal_date": h.get("trade_date") or h.get("signal_date") or d,
                "微观特征诊断": h.get("signals") or h.get("reason") or "",
                "层级": f"L{h.get('level', 2)}{'·' + tags if tags else ''}",
            })
        # 按综合得分降序，重排序号
        ordered = sorted(best_by_code.values(), key=lambda v: v[0], reverse=True)
        for i, (_, row) in enumerate(ordered, 1):
            row["序号"] = i
            dna_rows.append(row)
        n_dna = len(dna_rows)
        self.dna_list.set_rows(dna_rows, day=d)

        # ③ 观察池（v0.7.5/0.7.6：base_price=加入交易日 add_date 收盘 or entry_price；
        #    v0.8.5：+ 通达信日历导入 tdx_imports，独立于一级观察池）
        watch_rows = []
        for w in self.db.list_watch():
            if (w.get("add_date") or "") != d:
                continue
            # v0.8.1：观察池的 tags 来自 strategy（通过 trigger_indicator 关联）
            w_tags = ""
            if w.get("trigger_indicator"):
                st = self.db.list_strategies()
                for s in st:
                    if s.get("name") == w["trigger_indicator"]:
                        w_tags = s.get("tags", "") or ""
                        break
            src = "手动入池"
            if w.get("dna_score") is not None:
                src = "精筛入池"
            elif w.get("trigger_indicator"):
                src = "策略命中"
            reason = w.get("reason") or ""
            if w.get("dna_score") is not None:
                reason = f"{reason}｜DNA[{w.get('dna_template', '')}] {w.get('dna_score')}分"
            code = w.get("code")
            base = (self.db.get_close_on_date(code, d)
                    or w.get("entry_price"))
            watch_rows.append({
                "code": code, "name": w.get("name"),
                "来源": src,
                "加入价": base,
                "base_price": base,
                "signal_date": w.get("add_date"),       # v0.8.1：观察池的加入日 = add_date
                "最佳匹配模具": w.get("dna_template") or "",
                "原因": reason,
                "层级": f"L{w.get('level', 1)}{'·' + w_tags if w_tags else ''}",
            })
        # v0.8.5：通达信日历导入条目（只出现在该日期的二级观察池，不进一级观察池）
        for ti in self.db.list_tdx_imports(add_date=d):
            code = ti.get("code")
            base = self.db.get_close_on_date(code, d)
            watch_rows.append({
                "code": code, "name": ti.get("name"),
                "来源": "通达信导入",
                "加入价": base,
                "base_price": base,
                "signal_date": d,
                "最佳匹配模具": "",
                "原因": ti.get("reason") or f"通达信板块导入（{ti.get('block_name') or '—'}）",
                "层级": "L1·通达信",
            })
        n_watch = len(watch_rows)
        self.watch_list.set_rows(watch_rows, day=d)

        weekday = "一二三四五六日"[date.dayOfWeek() - 1]
        self.date_label.setText(
            f"{d}（周{weekday}）当日选股：粗筛 {n_coarse} 只 / 精筛 {n_dna} 只 / "
            f"观察池 {n_watch} 只")
        self.stack.setCurrentIndex(1)