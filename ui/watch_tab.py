# -*- coding: utf-8 -*-
"""观察池页签：候选股票管理、入选理由/触发指标、开平仓记录（自动因子快照）、K线。"""
import datetime

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (QComboBox, QCompleter, QDialog, QDoubleSpinBox,
                             QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                             QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
                             QSplitter, QTableView, QVBoxLayout, QWidget)

from data_feed import is_trading_time, infer_market
from ui.chart_widget import ChartDialog
from ui.models import PandasModel

WATCH_COLUMNS = ["id", "code", "name", "price", "pct", "add_date", "status",
                 "trigger_indicator", "dna_score", "dna_template", "reason"]


class ApproveDialog(QDialog):
    """待审核确认对话框：补充入选理由后转观察中。"""

    def __init__(self, watch, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"确认入池 - {watch.get('name')}（{watch.get('code')}）")
        self.setMinimumWidth(420)
        form = QFormLayout(self)
        self.reason_edit = QPlainTextEdit()
        self.reason_edit.setPlainText(watch.get("reason") or "")
        self.reason_edit.setPlaceholderText("补充/修改入选理由（为什么选这只票）")
        self.reason_edit.setFixedHeight(90)
        form.addRow("入选理由", self.reason_edit)
        self.trigger_edit = QLineEdit(watch.get("trigger_indicator") or "")
        self.trigger_edit.setPlaceholderText("触发指标/公式，如 MACD金叉")
        form.addRow("触发指标", self.trigger_edit)
        btns = QHBoxLayout()
        ok = QPushButton("确认入池")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        form.addRow(btns)


class AddWatchDialog(QDialog):
    def __init__(self, names, parent=None):
        super().__init__(parent)
        self.setWindowTitle("加入观察池")
        self.setMinimumWidth(360)
        form = QFormLayout(self)
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("如 600000")
        self.code_edit.textChanged.connect(self._fill_name)
        form.addRow("股票代码", self.code_edit)
        self.name_edit = QLineEdit()
        self.name_edit.setReadOnly(True)
        form.addRow("股票名称", self.name_edit)
        self.reason_edit = QPlainTextEdit()
        self.reason_edit.setPlaceholderText("为什么选这只票？（入选想法）")
        self.reason_edit.setFixedHeight(80)
        form.addRow("入选理由", self.reason_edit)
        self.trigger_edit = QLineEdit()
        self.trigger_edit.setPlaceholderText("如 MACD金叉 / MA5上穿MA10")
        form.addRow("触发指标", self.trigger_edit)
        self.price_spin = QDoubleSpinBox()
        self.price_spin.setRange(0, 100000)
        self.price_spin.setDecimals(2)
        form.addRow("入选价格", self.price_spin)
        btns = QHBoxLayout()
        ok = QPushButton("确定")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        form.addRow(btns)
        self._names = names
        if names:
            completer = QCompleter(names, self)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            self.code_edit.setCompleter(completer)

    def _fill_name(self, text):
        code = text.strip().zfill(6) if text.strip() else ""
        if code in self._names:
            self.name_edit.setText(self._names[code])

    def result(self):
        code = self.code_edit.text().strip().zfill(6)
        return {
            "code": code,
            "name": self.name_edit.text().strip() or code,
            "reason": self.reason_edit.toPlainText().strip(),
            "trigger": self.trigger_edit.text().strip(),
            "entry_price": self.price_spin.value() or None,
        }


class WatchTab(QWidget):
    def __init__(self, worker, db, parent=None):
        super().__init__(parent)
        self.worker = worker
        self.db = db
        self._names = {}
        self._quotes = {}          # code -> quote dict
        self._pending_snapshot = {}  # code -> trade 字段
        self._build_ui()
        self._load_watch()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_quotes)
        self.timer.start(5000)

        self.worker.quotes_ready.connect(self._on_quotes)
        self.worker.bars_ready.connect(self._on_bars)

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.add_btn = QPushButton("＋ 加入观察池")
        self.add_btn.setMinimumWidth(120)
        self.add_btn.clicked.connect(self._add_watch)
        bar.addWidget(self.add_btn)
        self.refresh_btn = QPushButton("刷新行情")
        self.refresh_btn.setMinimumWidth(90)
        self.refresh_btn.clicked.connect(self._refresh_quotes)
        bar.addWidget(self.refresh_btn)
        # v0.8.2：通达信自选股板块双向同步入口
        self.tdx_btn = QPushButton("🔄 通达信")
        self.tdx_btn.setToolTip("把观察池导出到通达信自定义板块（浏览K线），"
                                "或把通达信里筛好的板块导入观察池")
        self.tdx_btn.clicked.connect(self._open_tdx_sync)
        bar.addWidget(self.tdx_btn)
        # v0.8.3：批量删除按钮（观察池）
        self.del_btn = QPushButton("🗑️ 删除")
        self.del_btn.setToolTip("删除观察池中选中的行；不选时删除当前过滤下的全部行")
        self.del_btn.clicked.connect(self._delete_selected)
        bar.addWidget(self.del_btn)
        # 两端对齐：左侧操作按钮，右侧状态过滤/统计，避免宽屏下左侧挤成一团
        bar.addStretch(1)
        bar.addWidget(QLabel("状态过滤:"))
        self.status_filter = QComboBox()
        self.status_filter.addItems(["全部", "待审核", "观察中", "已开仓", "已清仓"])
        self.status_filter.currentTextChanged.connect(lambda _t: self._load_watch())
        bar.addWidget(self.status_filter)
        bar.addSpacing(12)
        self.status_label = QLabel("")
        self.status_label.setMinimumWidth(80)
        bar.addWidget(self.status_label)
        root.addLayout(bar)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)   # 面板不允许折叠成零宽
        # 左：观察池表格
        self.table = QTableView()
        self.table.setModel(PandasModel(None, color_cols=("pct",)))
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setMinimumWidth(480)
        self.table.selectionModel().currentRowChanged.connect(self._on_select)
        self.table.doubleClicked.connect(lambda _i: self._view_chart())  # 双击看K线
        splitter.addWidget(self.table)
        # 右：详情面板
        self.detail = self._build_detail()
        self.detail.setMinimumWidth(360)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        # 用父窗口初始尺寸计算 splitter 宽度，避免 self.width() 为 0 时压成零高
        parent = self.parentWidget()
        init_w = parent.width() if parent else 1280
        splitter.setSizes([int(init_w * 0.6), int(init_w * 0.4)])
        # stretch=1 让 splitter 占满 root 布局的剩余垂直空间，防止顶部大片空白
        root.addWidget(splitter, stretch=1)

    def _build_detail(self):
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(4, 4, 4, 4)

        v.addWidget(QLabel("◆ 观察详情"))
        self.detail_title = QLabel("未选择股票")
        self.detail_title.setStyleSheet("font-size:14px;font-weight:bold;")
        v.addWidget(self.detail_title)
        form = QFormLayout()
        self.reason_edit = QPlainTextEdit()
        self.reason_edit.setPlaceholderText("入选想法/理由")
        self.reason_edit.setFixedHeight(70)
        form.addRow("入选理由", self.reason_edit)
        self.trigger_edit = QLineEdit()
        form.addRow("触发指标", self.trigger_edit)
        self.price_spin = QDoubleSpinBox()
        self.price_spin.setRange(0, 100000)
        self.price_spin.setDecimals(2)
        form.addRow("入选价格", self.price_spin)
        self.status_combo = QComboBox()
        self.status_combo.addItems(["待审核", "观察中", "已开仓", "已清仓"])
        form.addRow("状态", self.status_combo)
        v.addLayout(form)

        btns = QHBoxLayout()
        save_btn = QPushButton("保存修改")
        save_btn.clicked.connect(self._save_detail)
        approve_btn = QPushButton("✓ 确认入池")
        approve_btn.setToolTip("待审核 → 观察中（可补充理由）")
        approve_btn.clicked.connect(self._approve)
        reject_btn = QPushButton("✕ 驳回")
        reject_btn.setToolTip("删除该待审核记录")
        reject_btn.clicked.connect(self._delete_watch)
        chart_btn = QPushButton("查看K线")
        chart_btn.clicked.connect(self._view_chart)
        del_btn = QPushButton("删除")
        del_btn.clicked.connect(self._delete_watch)
        btns.addWidget(save_btn)
        btns.addWidget(approve_btn)
        btns.addWidget(reject_btn)
        btns.addWidget(chart_btn)
        btns.addWidget(del_btn)
        v.addLayout(btns)

        v.addWidget(QLabel("◆ 记录交易（自动抓取因子快照）"))
        tform = QFormLayout()
        self.type_combo = QComboBox()
        self.type_combo.addItems(["开仓", "止盈", "止损", "减仓", "加仓"])
        tform.addRow("操作类型", self.type_combo)
        self.trade_price = QDoubleSpinBox()
        self.trade_price.setRange(0, 100000)
        self.trade_price.setDecimals(2)
        tform.addRow("成交价格", self.trade_price)
        self.trade_vol = QSpinBox()
        self.trade_vol.setRange(0, 100_000_000)
        self.trade_vol.setSingleStep(100)
        tform.addRow("成交数量(股)", self.trade_vol)
        self.trigger_cond = QLineEdit()
        self.trigger_cond.setPlaceholderText("如 股价达到 10.5 / MACD死叉")
        tform.addRow("触发条件", self.trigger_cond)
        self.trade_notes = QPlainTextEdit()
        self.trade_notes.setPlaceholderText("备注（可选）")
        self.trade_notes.setFixedHeight(50)
        tform.addRow("备注", self.trade_notes)
        rec_btn = QPushButton("记录交易")
        rec_btn.clicked.connect(self._record_trade)
        tform.addRow(rec_btn)
        v.addLayout(tform)

        v.addWidget(QLabel("◆ 历史交易"))
        self.trade_table = QTableView()
        self.trade_table.setModel(PandasModel(None))
        self.trade_table.setAlternatingRowColors(True)
        self.trade_table.setMinimumHeight(120)
        # 历史交易表拉伸填满剩余空间，避免底部大片空白
        v.addWidget(self.trade_table, stretch=1)
        return panel

    # ------------------------------------------------------------ 数据加载
    def set_stock_names(self, names):
        self._names = names

    def _open_tdx_sync(self):
        """v0.8.2：打开通达信自选股板块双向同步对话框。

        导出范围：表格中「选中行」；无选中则导出当前过滤下的全部行。
        """
        from ui.tdx_sync_dialog import TdxSyncDialog

        def watch_rows_provider():
            df = self.table.model().dataframe()
            if df is None or df.empty:
                return []
            idxs = self.table.selectionModel().selectedRows()
            if idxs:
                rows = [df.iloc[i.row()].to_dict() for i in idxs
                        if 0 <= i.row() < len(df)]
                # 去重保序
                seen, out = set(), []
                for r in rows:
                    c = str(r.get("code", ""))
                    if c and c not in seen:
                        seen.add(c)
                        out.append(r)
                return out
            # 无选中 → 当前过滤下的全部行
            return df.to_dict("records")

        def watch_codes_provider():
            return {str(r["code"]) for r in self.db.list_watch()}

        dlg = TdxSyncDialog(
            self.db, self.worker,
            watch_codes_provider=watch_codes_provider,
            watch_rows_provider=watch_rows_provider,
            on_imported=self._load_watch,
            parent=self,
            dest="watch_pool")   # v0.8.5：一级观察池模块导入进一级观察池
        dlg.exec_()

    def _delete_selected(self):
        """v0.8.3：批量删除观察池中的行。

        - 选中行 → 删除选中行
        - 无选中 → 删除当前过滤下全部行
        - 弹确认框防误删；删除后刷新
        - 一并把 scan_history.in_watchlist 回写 0（避免历史命中仍标记在池中）
        """
        df = self.table.model().dataframe()
        if df is None or df.empty:
            QMessageBox.information(self, "提示", "观察池没有可删除的行")
            return
        idxs = self.table.selectionModel().selectedRows()
        if idxs:
            rows = []
            seen = set()
            for i in idxs:
                if 0 <= i.row() < len(df):
                    code = str(df.iloc[i.row()].get("code", ""))
                    if code and code not in seen:
                        seen.add(code)
                        rows.append(df.iloc[i.row()].to_dict())
            scope_desc = f"选中的 {len(rows)} 只"
        else:
            rows = df.to_dict("records")
            scope_desc = f"当前过滤下的全部 {len(rows)} 只"
        if not rows:
            QMessageBox.information(self, "提示", "没有可删除的行")
            return
        ret = QMessageBox.warning(
            self, "确认删除",
            f"将从观察池删除 {scope_desc} 标的。\n"
            f"（关联的交易记录会一并清理，scan_history 的 in_watchlist 标记会回写）\n\n确定继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        # 收集 id 和 code
        ids = [int(r["id"]) for r in rows if r.get("id") is not None]
        codes = [str(r.get("code", "")) for r in rows if r.get("code")]
        today = self.status_filter.currentText()  # 不重要，仅用作上下文
        for wid in ids:
            self.db.delete_watch(wid)
        for code in codes:
            try:
                # 回写 scan_history 的 in_watchlist=0（不限 day，对该票所有命中）
                self.db.update_scan_in_watchlist(code, day=None, flag=0)
            except Exception:  # noqa: BLE001
                pass
        self._load_watch()
        self.status_label.setText(f"已删除 {len(ids)} 只")

    def _load_watch(self):
        # 直接走渲染逻辑（合并实时行情，price/pct 来自行情快照而非数据库字段）
        self._render_watch_table()
        self._refresh_quotes()

    def watch_codes(self):
        rows = self.db.list_watch()
        return [(r["market"] if r["market"] is not None else infer_market(r["code"]), r["code"])
                for r in rows]

    def _refresh_quotes(self):
        codes = self.watch_codes()
        if codes:
            self.worker.request_quotes(codes)

    def _on_quotes(self, df):
        if df is None or df.empty:
            return
        self._quotes.update({str(r.code): r._asdict() for r in df.itertuples()})
        self._render_watch_table()

    def _render_watch_table(self):
        status = self.status_filter.currentText()
        rows = self.db.list_watch(status=None if status == "全部" else status)
        import pandas as pd
        out = []
        for r in rows:
            q = self._quotes.get(str(r["code"]), {})
            out.append({
                "id": r["id"], "code": r["code"], "name": r["name"],
                "price": q.get("price", ""), "pct": q.get("pct", ""),
                "add_date": r["add_date"], "status": r["status"],
                "trigger_indicator": r["trigger_indicator"] or "",
                "dna_score": r.get("dna_score"),
                "dna_template": r.get("dna_template") or "",
                "reason": (r["reason"] or "")[:40],
            })
        df = pd.DataFrame(out)
        self.table.model().setDataFrame(df if not df.empty else df)
        self._fit_columns()
        self.status_label.setText(f"观察池 {len(rows)} 只")

    def _fit_columns(self):
        """设置观察池表格列宽，并让最后一列（入选理由）拉伸填满，避免全屏右侧留白。"""
        header = self.table.horizontalHeader()
        widths = {"id": 40, "code": 72, "name": 90, "price": 72, "pct": 72,
                  "add_date": 85, "status": 72, "trigger_indicator": 110,
                  "dna_score": 72, "dna_template": 110, "reason": 140}
        for i, col in enumerate(WATCH_COLUMNS):
            self.table.setColumnWidth(i, widths.get(col, 80))
        header.setStretchLastSection(True)

    # ------------------------------------------------------------ 交互
    def _add_watch(self):
        dlg = AddWatchDialog(self._names, self)
        if dlg.exec_() != QDialog.Accepted:
            return
        info = dlg.result()
        if not info["code"] or not info["code"].isdigit():
            QMessageBox.warning(self, "提示", "请输入有效的股票代码")
            return
        watch_id = self.db.add_watch(
            code=info["code"], name=info["name"], market=infer_market(info["code"]),
            trigger_indicator=info["trigger"], entry_price=info["entry_price"],
            reason=info["reason"], status="观察中")
        if watch_id:
            self._load_watch()
            self.status_label.setText(f"已加入观察池：{info['name']}（{info['code']}）")

    def _selected_row(self):
        idx = self.table.currentIndex()
        df = self.table.model().dataframe()
        return df.iloc[idx.row()] if idx.isValid() and 0 <= idx.row() < len(df) else None

    def _on_select(self, current, previous):
        self._render_detail(self._selected_row())

    def _render_detail(self, rec):
        if rec is None:
            self.detail_title.setText("未选择股票")
            self.trade_table.model().setDataFrame(None)
            return
        # 从数据库取完整记录（表格里 reason 有截断）
        full = self.db.get_watch(int(rec["id"])) or dict(rec)
        self.detail_title.setText(f"{full['name']}（{full['code']}）")
        self.reason_edit.setPlainText(full.get("reason") or "")
        self.trigger_edit.setText(full.get("trigger_indicator") or "")
        self.price_spin.setValue(float(full.get("entry_price") or 0))
        self.status_combo.setCurrentText(full.get("status") or "观察中")
        self.trade_price.setValue(float(self._quotes.get(str(full["code"]), {}).get("price") or 0))
        trades = self.db.list_trades(code=str(full["code"]))
        import pandas as pd
        self.trade_table.model().setDataFrame(pd.DataFrame(trades))

    def _save_detail(self):
        rec = self._selected_row()
        if rec is None:
            return
        self.db.update_watch(int(rec["id"]),
                             reason=self.reason_edit.toPlainText().strip(),
                             trigger_indicator=self.trigger_edit.text().strip(),
                             entry_price=self.price_spin.value() or None,
                             status=self.status_combo.currentText())
        self._render_watch_table()
        self.status_label.setText("已保存")

    def _delete_watch(self):
        rec = self._selected_row()
        if rec is None:
            return
        if QMessageBox.question(self, "确认", f"删除 {rec['name']}（{rec['code']}）及其交易记录？") \
                == QMessageBox.Yes:
            self.db.delete_watch(int(rec["id"]))
            self._load_watch()

    def _approve(self):
        """待审核 → 观察中：弹出对话框补充入选理由后确认。"""
        rec = self._selected_row()
        if rec is None:
            return
        full = self.db.get_watch(int(rec["id"]))
        if full is None:
            return
        if full.get("status") != "待审核":
            QMessageBox.information(self, "提示", "仅「待审核」状态的记录需要确认入池")
            return
        dlg = ApproveDialog(full, self)
        if dlg.exec_() == QDialog.Accepted:
            self.db.update_watch(int(rec["id"]), status="观察中",
                                 reason=dlg.reason_edit.toPlainText().strip(),
                                 trigger_indicator=dlg.trigger_edit.text().strip())
            self._load_watch()
            self.status_label.setText(f"已确认入池：{full['name']}（{full['code']}）")

    def _view_chart(self):
        rec = self._selected_row()
        if rec is None:
            return
        # v0.7.3：K线图对话框直接打开（内部异步拉数据，周期/指标可切换）
        from ui.chart_widget import ChartDialog
        ChartDialog(str(rec["code"]), rec["name"],
                    int(rec.get("market") or infer_market(rec["code"])),
                    self.db, self).exec_()

    def _record_trade(self):
        rec = self._selected_row()
        if rec is None:
            QMessageBox.information(self, "提示", "请先在左侧选择一只股票")
            return
        trade_type = self.type_combo.currentText()
        price = self.trade_price.value()
        vol = self.trade_vol.value()
        if price <= 0 or vol <= 0:
            QMessageBox.warning(self, "提示", "请输入有效的成交价格和数量")
            return
        code = str(rec["code"])
        self._pending_snapshot[code] = {
            "watch_id": int(rec["id"]), "code": code, "name": rec["name"],
            "trade_type": trade_type,
            "trigger_condition": self.trigger_cond.text().strip(),
            "trade_price": price, "trade_volume": vol,
            "notes": self.trade_notes.toPlainText().strip(),
        }
        self.worker.request_bars(code, rec["name"],
                                 int(rec.get("market") or infer_market(code)),
                                 count=120, source="watch")
        self.status_label.setText("正在抓取因子快照…")

    def _on_bars(self, code, name, df, source):
        if source != "watch":
            return
        code = str(code)
        if code in self._pending_snapshot:
            info = self._pending_snapshot.pop(code)
            self._complete_trade(info, df)

    def _complete_trade(self, info, df):
        import pandas as pd
        from indicators import snapshot_indicators
        indicators = snapshot_indicators(df) if df is not None and len(df) else {}
        trade_id = self.db.add_trade(
            watch_id=info["watch_id"], code=info["code"], name=info["name"],
            trade_type=info["trade_type"], trigger_condition=info["trigger_condition"],
            trade_price=info["trade_price"], trade_volume=info["trade_volume"],
            notes=info["notes"])
        self.db.add_snapshot(trade_id, info["code"],
                             infer_market(info["code"]), indicators)
        # 状态联动：开仓->已开仓；止盈/止损->已清仓
        new_status = None
        if info["trade_type"] == "开仓":
            new_status = "已开仓"
        elif info["trade_type"] in ("止盈", "止损"):
            new_status = "已清仓"
        if new_status:
            self.db.update_watch(info["watch_id"], status=new_status)
        self._render_watch_table()
        self._render_detail(self._selected_row())
        self.status_label.setText(
            f"已记录{info['trade_type']}：{info['name']} @ {info['trade_price']}，"
            f"因子快照 {len(indicators)} 项")
