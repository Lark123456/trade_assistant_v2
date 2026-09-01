# -*- coding: utf-8 -*-
"""交易记录页签：查询/删除交易日志，查看因子快照。"""
import json

from PyQt5.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel,
                             QLineEdit, QMessageBox, QPushButton, QTableWidget,
                             QTableWidgetItem, QTableView, QVBoxLayout, QWidget)

from ui.models import PandasModel

TRADE_COLUMNS = ["id", "code", "name", "trade_type", "trade_price",
                 "trade_volume", "trade_time", "trigger_condition", "notes"]


class SnapshotDialog(QDialog):
    def __init__(self, trade, snapshot, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"因子快照 - {trade.get('name')}({trade.get('code')}) "
                            f"{trade.get('trade_type')} @ {trade.get('trade_time')}")
        self.resize(420, 480)
        v = QVBoxLayout(self)
        table = QTableWidget()
        table.setColumnCount(2)
        table.setHorizontalHeaderLabels(["因子", "数值"])
        items = []
        if snapshot:
            try:
                ind = json.loads(snapshot.get("indicators") or "{}")
            except (json.JSONDecodeError, TypeError):
                ind = {}
            for k, val in ind.items():
                items.append((k, "" if val is None else str(val)))
            items.append(("市场指数", "" if snapshot.get("market_index") is None
                          else str(snapshot["market_index"])))
            items.append(("成交量(手)", "" if snapshot.get("volume") is None
                          else str(snapshot["volume"])))
            items.append(("快照时间", str(snapshot.get("snapshot_time", ""))))
        table.setRowCount(len(items))
        for i, (k, val) in enumerate(items):
            table.setItem(i, 0, QTableWidgetItem(k))
            table.setItem(i, 1, QTableWidgetItem(val))
        table.horizontalHeader().setStretchLastSection(True)
        table.setColumnWidth(0, 160)
        v.addWidget(table)


class TradeTab(QWidget):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._build_ui()
        self._load()

    def _build_ui(self):
        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("类型:"))
        self.type_combo = QComboBox()
        self.type_combo.addItems(["全部", "开仓", "止盈", "止损", "减仓", "加仓"])
        self.type_combo.currentTextChanged.connect(self._load)
        bar.addWidget(self.type_combo)
        bar.addWidget(QLabel("代码:"))
        self.code_edit = QLineEdit()
        self.code_edit.setMaximumWidth(140)
        self.code_edit.textChanged.connect(self._load)
        bar.addWidget(self.code_edit)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._load)
        bar.addWidget(refresh_btn)
        bar.addStretch(1)
        self.count_label = QLabel("")
        bar.addWidget(self.count_label)
        root.addLayout(bar)
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("color:#666;padding:2px;")
        root.addWidget(self.stats_label)

        self.table = QTableView()
        self.table.setModel(PandasModel(None))
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        root.addWidget(self.table)

        abar = QHBoxLayout()
        abar.addStretch(1)
        snap_btn = QPushButton("查看因子快照")
        snap_btn.clicked.connect(self._view_snapshot)
        del_btn = QPushButton("删除选中")
        del_btn.clicked.connect(self._delete)
        abar.addWidget(snap_btn)
        abar.addWidget(del_btn)
        root.addLayout(abar)

    def _load(self):
        t = self.type_combo.currentText()
        trade_type = None if t == "全部" else t
        code = self.code_edit.text().strip() or None
        trades = self.db.list_trades(code=code, trade_type=trade_type, limit=5000)
        import pandas as pd
        df = pd.DataFrame(trades)
        self.table.model().setDataFrame(df[TRADE_COLUMNS] if not df.empty else df)
        self.count_label.setText(f"共 {len(trades)} 条")
        self._update_stats(trades)

    def _update_stats(self, trades):
        """轻量统计：笔数分布、止盈/止损胜率、累计买卖金额（决策日记的辅助参考）。"""
        from collections import Counter
        cnt = Counter(x["trade_type"] for x in trades)
        buy = sum((x["trade_price"] or 0) * (x["trade_volume"] or 0)
                  for x in trades if x["trade_type"] in ("开仓", "加仓"))
        sell = sum((x["trade_price"] or 0) * (x["trade_volume"] or 0)
                   for x in trades if x["trade_type"] in ("止盈", "止损", "减仓"))
        win = cnt.get("止盈", 0)
        loss = cnt.get("止损", 0)
        closed = win + loss
        rate = f"{win / closed * 100:.1f}%" if closed else "-"
        self.stats_label.setText(
            f"轻量统计｜总 {len(trades)} 笔｜开仓 {cnt.get('开仓', 0)}｜加仓 {cnt.get('加仓', 0)}｜"
            f"止盈 {win}｜止损 {loss}｜减仓 {cnt.get('减仓', 0)}｜"
            f"胜率(止盈/已了结) {rate}｜累计买入 {buy:,.0f} 元｜累计卖出 {sell:,.0f} 元")

    def _selected(self):
        idx = self.table.currentIndex()
        df = self.table.model().dataframe()
        return df.iloc[idx.row()] if idx.isValid() and 0 <= idx.row() < len(df) else None

    def _view_snapshot(self):
        rec = self._selected()
        if rec is None:
            return
        snapshot = None
        if rec.get("snapshot_id"):
            snapshot = self.db.get_snapshot(int(rec["snapshot_id"]))
        SnapshotDialog(dict(rec), snapshot, self).exec_()

    def _delete(self):
        rec = self._selected()
        if rec is None:
            return
        if QMessageBox.question(self, "确认", f"删除该交易记录（id={rec['id']}）？") == QMessageBox.Yes:
            self.db.delete_trade(int(rec["id"]))
            self._load()
