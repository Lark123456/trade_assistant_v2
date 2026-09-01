# -*- coding: utf-8 -*-
"""统一命中列表组件（v0.6.6）：所有"选股结果/命中清单"共用的表格 + 行操作。

用于：行情日历的每日清单、扫描中心的最近命中/命中历史（后续接入）。
列：来源 | 策略 | 代码 | 名称 | 价格 | 涨幅% | 较加入价% | 10日最高涨幅% | 30日最高涨幅% | 时间 | 原因
行操作：查看K线 / 加入观察池 / 单票诊断 —— 与全局统一入口一致。

v0.8.1 新增列（计算口径）：
- 较加入价%：现价 / 加入日收盘价 - 1
- 10日最高涨幅%：加入日起往后 10 个交易日内最高收盘 / 加入日收盘 - 1
- 30日最高涨幅%：加入日起往后 30 个交易日内最高收盘 / 加入日收盘 - 1

数据来源：daily_bars_cache（缓存的全量日K），signal_date 优先从 row 取，否则用 self._day。
"""
import pandas as pd

from PyQt5.QtWidgets import (QHBoxLayout, QLabel, QMessageBox, QPushButton,
                             QTableView, QVBoxLayout, QWidget)

from data_feed import infer_market
from ui.models import PandasModel

# v0.8.1：新增三列（较加入价% + 10/30 日最高涨幅%）
HIT_COLUMNS = ["来源", "策略", "代码", "名称", "价格", "涨幅%",
               "较加入价%", "10日最高涨幅%", "30日最高涨幅%",
               "时间", "原因"]
# v0.8.1：百分点列都红涨绿跌着色
HIT_PERF_COLS = ("涨幅%", "较加入价%", "10日最高涨幅%", "30日最高涨幅%")


def _compute_post_hits(bars_df, signal_date, max_days=30):
    """从 daily_bars_cache 计算加入日之后的表现（v0.8.1）。

    输入：
      bars_df: 时间正序 DataFrame，含 date/close 等列（来自 daily_bars_cache）。
      signal_date: 加入日（'YYYY-MM-DD'）。
      max_days: 累计窗口上限（默认 30 日，覆盖 10/30 两窗口）。

    输出 dict：
      current_pct: 现价(最后一日收盘) / 加入日收盘 - 1，乘 100
      max_pct_10/30: 加入日之后 10/30 个交易日内最高收盘 / 加入日收盘 - 1
      status: "OK" / "NO_BASE"(无bars或加入日不在缓存) / "INSUFFICIENT"(加入日之后缓存不足 N 日) /
              "FUTURE"(加入日是未来) / "NO_SIGNAL"(signal_date 为空)
    """
    no_signal = {
        "current_pct": None, "max_pct_10": None, "max_pct_30": None,
        "status": "NO_SIGNAL",
    }
    if not signal_date:
        return no_signal
    sig_str = str(signal_date)
    if bars_df is None or bars_df.empty:
        return {**no_signal, "status": "NO_BASE"}
    # date 列可能为 str / Timestamp，统一转 str 比较
    dates_str = bars_df["date"].astype(str)
    base_mask = dates_str == sig_str
    if not base_mask.any():
        # 加入日不在缓存中——判断是未来还是缓存缺失
        max_cache_date = dates_str.iloc[-1] if len(dates_str) else ""
        if sig_str > max_cache_date:
            return {**no_signal, "status": "FUTURE"}
        return {**no_signal, "status": "NO_BASE"}
    base_close = float(bars_df.loc[base_mask, "close"].iloc[0])
    if base_close <= 0:
        return {**no_signal, "status": "NO_BASE"}
    # 加入日之后的所有日（不截断；max_days 只用于 10 日窗口）
    future_mask = dates_str > sig_str
    future_all = bars_df.loc[future_mask]
    future_count_all = len(future_all)
    current_close = float(bars_df["close"].iloc[-1])
    current_pct = (current_close / base_close - 1) * 100
    if future_count_all == 0:
        # 没有任何后续日，只能算 current_pct
        return {
            "current_pct": current_pct,
            "max_pct_10": None, "max_pct_30": None,
            "status": "OK",
        }
    future_closes = future_all["close"].astype(float)
    # v0.8.1：30 日窗口 = 整个 future 的最高（不截断到 30——历史越久样本越多越有意义）
    max_close_30 = float(future_closes.max())
    max_pct_30 = (max_close_30 / base_close - 1) * 100
    # 10 日窗口：取 future 的前 10 个交易日（max_days 限制）
    if future_count_all >= 10:
        max_close_10 = float(future_closes.iloc[:10].max())
        max_pct_10 = (max_close_10 / base_close - 1) * 100
        status = "OK"
    else:
        # 不足 10 日但有数据：仍可算 10 日窗口（实际 = 现有天数）
        max_close_10 = float(future_closes.max())
        max_pct_10 = (max_close_10 / base_close - 1) * 100
        status = "INSUFFICIENT"
    return {
        "current_pct": current_pct,
        "max_pct_10": max_pct_10,
        "max_pct_30": max_pct_30,
        "status": status,
    }


class HitListWidget(QWidget):
    """统一命中列表。set_rows(rows) 传入统一字段的 dict 列表。
    source: 实例标识（用于区分多个实例的 K线请求回调，v0.7.0 日历页三子菜单各一个实例）。"""

    def __init__(self, worker, db, on_watch_added=None, source="hitlist", parent=None):
        super().__init__(parent)
        self.worker = worker
        self.db = db
        self.on_watch_added = on_watch_added
        self.source = source
        self._watch_codes = {str(w["code"]) for w in self.db.list_watch()}
        self._day = None   # v0.7.4：当前清单的交易日（用于回写 scan_history.in_watchlist）
        self._build_ui()
        # v0.8.1：缓存每只股票的 daily_bars（避免每行重复查）
        self._bars_cache = {}

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.table = QTableView()
        self.table.setModel(PandasModel(None, color_cols=HIT_PERF_COLS))
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        # v0.7.4：支持多选后批量加入观察池
        self.table.setSelectionMode(QTableView.ExtendedSelection)
        self.table.doubleClicked.connect(lambda _i: self._view_chart())  # 双击看K线
        root.addWidget(self.table, stretch=1)
        bar = QHBoxLayout()
        chart_btn = QPushButton("查看K线")
        chart_btn.clicked.connect(self._view_chart)
        watch_btn = QPushButton("加入观察池")
        watch_btn.clicked.connect(self._add_watch)
        diag_btn = QPushButton("单票诊断")
        diag_btn.clicked.connect(self._diagnose)
        bar.addWidget(chart_btn)
        bar.addWidget(watch_btn)
        bar.addWidget(diag_btn)
        bar.addStretch(1)
        self.status_label = QLabel("")  # v0.8.1：计算状态/进度
        bar.addWidget(self.status_label)
        self.count_label = QLabel("")
        bar.addWidget(self.count_label)
        root.addLayout(bar)

    # ------------------------------------------------------------ 数据
    def set_rows(self, rows, day=None):
        self._day = day
        # v0.8.1：批量预取 daily_bars + 计算较加入价% / 10日 / 30 日最高涨幅
        enriched = self._enrich_with_post_hits(rows)
        df = pd.DataFrame(enriched, columns=HIT_COLUMNS) if enriched else pd.DataFrame()
        self.table.model().setDataFrame(df)
        # v0.8.1：状态栏显示计算结果统计
        ok = sum(1 for r in enriched if r.get("_post_hit_status") == "OK")
        insufficient = sum(1 for r in enriched if r.get("_post_hit_status") == "INSUFFICIENT")
        future = sum(1 for r in enriched if r.get("_post_hit_status") == "FUTURE")
        no_base = sum(1 for r in enriched if r.get("_post_hit_status") in (None, "NO_BASE"))
        bits = []
        if ok:
            bits.append(f"已算 {ok}")
        if insufficient:
            bits.append(f"缓存不足 {insufficient}")
        if future:
            bits.append(f"未来日 {future}")
        if no_base:
            bits.append(f"无基准 {no_base}")
        self.status_label.setText(" | ".join(bits))
        self.count_label.setText(f"共 {len(enriched)} 条")
        self._watch_codes = {str(w["code"]) for w in self.db.list_watch()}

    def _selected(self):
        idx = self.table.currentIndex()
        df = self.table.model().dataframe()
        if idx.isValid() and 0 <= idx.row() < len(df):
            return df.iloc[idx.row()]
        return None

    # -------------------------------------------------------- 后续表现计算
    def _enrich_with_post_hits(self, rows):
        """为每行计算：较加入价% / 10日最高涨幅% / 30日最高涨幅%。
        v0.8.1 性能：每只股票只查一次 daily_bars_cache，结果缓存于 self._bars_cache。
        signal_date 优先 row["signal_date"]，否则用 self._day。
        """
        if not rows:
            return rows
        # 1. 回收旧缓存，只保留本批次用到的 code
        used_codes = {str(r.get("代码", "")) for r in rows}
        used_codes.discard("")
        new_cache = {c: self._bars_cache[c] for c in used_codes if c in self._bars_cache}
        # 2. 拉未缓存的 code
        for code in used_codes:
            if code not in new_cache:
                try:
                    new_cache[code] = self.db.get_cached_daily_bars(code)
                except Exception:  # noqa: BLE001
                    new_cache[code] = pd.DataFrame()
        self._bars_cache = new_cache
        # 3. 逐行计算（不修改入参 dict——pandas.DataFrame 在 rows 为 None 列时可能报错）
        out = []
        for r in rows:
            r2 = dict(r)  # 拷贝避免污染入参
            code = str(r2.get("代码", "") or "")
            sig = r2.get("signal_date") or self._day
            perf = _compute_post_hits(self._bars_cache.get(code), sig)
            r2["较加入价%"] = perf["current_pct"]
            r2["10日最高涨幅%"] = perf["max_pct_10"]
            r2["30日最高涨幅%"] = perf["max_pct_30"]
            r2["_post_hit_status"] = perf["status"]
            out.append(r2)
        return out

    # ------------------------------------------------------------ 行操作
    def _view_chart(self):
        rec = self._selected()
        if rec is None:
            return
        # v0.7.3：K线图对话框直接打开（内部异步拉数据，周期/指标可切换）
        from ui.chart_widget import ChartDialog
        ChartDialog(str(rec["代码"]), str(rec["名称"]), infer_market(str(rec["代码"])),
                    self.db, self).exec_()

    def _add_watch(self):
        """批量加入观察池（支持多选）：写入 watch_pool、回写 in_watchlist、提示。"""
        df = self.table.model().dataframe()
        if df is None or df.empty:
            return
        idxs = self.table.selectionModel().selectedRows()
        if not idxs:
            # 无选中时回退到当前行（兼容单点操作习惯）
            cur = self.table.currentIndex()
            if cur.isValid():
                idxs = [cur]
        if not idxs:
            return
        added, skipped = 0, 0
        for idx in idxs:
            if not (0 <= idx.row() < len(df)):
                continue
            rec = df.iloc[idx.row()]
            code = str(rec["代码"])
            if code in self._watch_codes:
                skipped += 1
                continue
            strategy = str(rec["策略"]) if rec["策略"] != "-" else "手动加入"
            reason = str(rec["原因"]) if rec["原因"] else "从历史选股清单加入"
            self.db.add_watch(code=code, name=str(rec["名称"]), market=infer_market(code),
                              trigger_indicator=strategy,
                              entry_price=float(rec["价格"]) if rec["价格"] not in ("", None) else None,
                              reason=reason, status="观察中")
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

    def _diagnose(self):
        from ui.diagnose_dialog import DiagnoseDialog
        DiagnoseDialog(self.db, parent=self).exec_()
