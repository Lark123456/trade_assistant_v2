# -*- coding: utf-8 -*-
"""单票条件级诊断器（保险机制第二层，核心）：
选一只股票 + 一个策略/指标，把公式每一步计算摊开——
逐条显示中间变量最新数值、每个条件的通过/不通过、最终信号结论，
让不懂代码的用户也能逐项和通达信对账。"""
import numpy as np

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel,
                             QLineEdit, QMessageBox, QPlainTextEdit,
                             QPushButton, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)

from data_feed import DataFeed, infer_market
from formula_explain import explain_formula, format_value, is_bool_array
from indicators import compute_indicators, evaluate_script, formula_signal
from scanner import CachedFeed


class DiagWorker(QThread):
    """后台取数 + 逐条求值，避免阻塞界面。"""
    done = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, db, code, name, formula, force_refresh=False, parent=None):
        super().__init__(parent)
        self.db = db
        self.code = code
        self.name = name
        self.formula = formula
        self.force_refresh = force_refresh

    def run(self):
        feed = None
        try:
            feed = CachedFeed(DataFeed(), self.db)
            bars = feed.get_daily_bars(self.code, market=infer_market(self.code),
                                       count=300, force_refresh=self.force_refresh)
            if bars is None or len(bars) < 30:
                raise ValueError("日K数据不足（少于30根），无法诊断")
            df = compute_indicators(bars)
            outputs, _env, _last, lines = evaluate_script(df, self.formula)
            sig, labels = formula_signal(df, self.formula)
            expl = explain_formula(self.formula)

            rows = []
            signal_name = None
            signal_is_bool = True
            if outputs:
                signal_name = list(outputs)[-1]
                signal_is_bool = is_bool_array(outputs[signal_name])
            elif lines:
                last_line = lines[-1]
                if last_line[0] is None:
                    signal_name = "（最终表达式）"
                    signal_is_bool = is_bool_array(last_line[2])
            for i, (name, is_output, val) in enumerate(lines):
                text = expl[i]["text"] if i < len(expl) else ""
                bool_ok = is_bool_array(val)
                last_true = None
                if bool_ok:
                    arr = np.asarray(val)
                    if arr.ndim == 0:
                        last_true = bool(arr)
                    else:
                        last_true = bool(np.nan_to_num(arr[-1], nan=0.0) > 0)
                is_signal = (name == signal_name) or (name is None and signal_name == "（最终表达式）")
                rows.append({
                    "name": name or "（无名称）",
                    "text": text,
                    "value": "通过" if (bool_ok and last_true) else
                             ("不通过" if bool_ok else format_value(val)),
                    "is_bool": bool_ok,
                    "passed": last_true,
                    "is_signal": is_signal,
                })
            self.done.emit({
                "code": self.code, "name": self.name,
                "date": str(bars["date"].iloc[-1]),
                "source": feed.last_source,
                "signal": sig, "labels": labels,
                "signal_is_bool": signal_is_bool,
                "rows": rows, "formula": self.formula,
            })
        except Exception as e:  # noqa: BLE001
            self.error.emit(str(e))
        finally:
            if feed is not None:
                try:
                    feed.feed.disconnect()
                except Exception:  # noqa: BLE001
                    pass


class DiagnoseDialog(QDialog):
    def __init__(self, db, current=None, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("单票条件级诊断（核对代码实现 == 你的本意）")
        self.resize(880, 620)
        self._worker = None

        root = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("策略/指标:"))
        self.strategy_combo = QComboBox()
        self._fill_strategies(current)
        top.addWidget(self.strategy_combo, stretch=2)
        top.addWidget(QLabel("股票代码:"))
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("如 600000 / 000001")
        self.code_edit.setMaximumWidth(120)
        top.addWidget(self.code_edit)
        self.run_btn = QPushButton("🔍 开始诊断")
        self.run_btn.clicked.connect(self._run)
        top.addWidget(self.run_btn)
        self.refresh_check = QCheckBox("强制刷新缓存")
        self.refresh_check.setToolTip("勾选后会从行情服务器重新拉取日K，覆盖本地旧缓存")
        top.addWidget(self.refresh_check)
        root.addLayout(top)

        self.summary = QLabel("选择策略并输入股票代码，点击「开始诊断」。")
        self.summary.setStyleSheet("font-weight:bold; padding:4px;")
        root.addWidget(self.summary)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["语句", "中文释义", "最新值", "状态"])
        self.table.setColumnWidth(0, 130)
        self.table.setColumnWidth(1, 420)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 90)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        root.addWidget(self.table, stretch=3)

        root.addWidget(QLabel("公式原文:"))
        self.formula_view = QPlainTextEdit()
        self.formula_view.setReadOnly(True)
        self.formula_view.setFixedHeight(100)
        root.addWidget(self.formula_view)

        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        root.addWidget(close_btn)

    def _fill_strategies(self, current):
        self._items = self.db.list_strategies()
        cur_id = current.get("id") if current else None
        for i, s in enumerate(self._items):
            label = ("📈 " if s.get("kind") != "indicator" else "📊 ") + s["name"]
            self.strategy_combo.addItem(label, i)
            if cur_id is not None and s["id"] == cur_id:
                self.strategy_combo.setCurrentIndex(self.strategy_combo.count() - 1)

    def _current_item(self):
        idx = self.strategy_combo.currentData()
        if idx is None or idx >= len(self._items):
            return None
        return self._items[idx]

    def _run(self):
        if self._worker is not None and self._worker.isRunning():
            return
        item = self._current_item()
        if item is None:
            QMessageBox.information(self, "提示", "请先选择策略或指标")
            return
        code = self.code_edit.text().strip()
        if not (code.isdigit() and len(code) == 6):
            QMessageBox.warning(self, "提示", "请输入 6 位股票代码")
            return
        name = ""
        for s in self.db.load_stock_list():
            if str(s["code"]) == code:
                name = s["name"]
                break
        formula = item.get("formula") or ""
        if not formula:
            QMessageBox.warning(self, "提示", "该项没有公式内容")
            return
        self.run_btn.setEnabled(False)
        self.summary.setText(f"正在获取 {code} {name} 的日K数据并逐条求值…")
        force_refresh = self.refresh_check.isChecked()
        self._worker = DiagWorker(self.db, code, name, formula,
                                  force_refresh=force_refresh, parent=self)
        self._worker.done.connect(self._on_done)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(lambda: self.run_btn.setEnabled(True))
        self._worker.start()

    def _on_done(self, res):
        if res["signal_is_bool"]:
            concl = "✅ 命中（成立）" if res["signal"] else "❌ 未命中（不成立）"
        else:
            concl = "该指标为数值输出，无信号判定（见下方各输出最新值）"
        source = res.get("source", "unknown")
        source_text = {
            "cache": "本地缓存",
            "network": "行情服务器",
            "cache_stale": "本地缓存（网络失败，数据可能滞后）",
            "none": "无数据",
        }.get(source, source)
        self.summary.setText(
            f"股票：{res['code']} {res['name']}　|　最新K线：{res['date']}　|　"
            f"来源：{source_text}　|　结论：{concl}"
            + (("　|　标注：" + "；".join(
                f"{k}={'是' if v > 0 else '否'}" for k, v in res['labels'].items()))
                if res['labels'] else ""))
        self.formula_view.setPlainText(res["formula"])
        rows = res["rows"]
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            name_item = QTableWidgetItem(r["name"])
            if r["is_signal"]:
                name_item.setText(r["name"] + " ★信号")
                name_item.setForeground(QColor("#d4380d"))
                name_item.setBackground(QColor("#fff7e6"))
            self.table.setItem(i, 0, name_item)
            self.table.setItem(i, 1, QTableWidgetItem(r["text"]))
            self.table.setItem(i, 2, QTableWidgetItem(r["value"]))
            status = QTableWidgetItem()
            if r["is_bool"]:
                if r["passed"]:
                    status.setText("✅ 通过")
                    status.setForeground(QColor("#237804"))
                else:
                    status.setText("❌ 不通过")
                    status.setForeground(QColor("#cf1322"))
            else:
                status.setText("—")
            self.table.setItem(i, 3, status)
        self.table.resizeRowsToContents()

    def _on_error(self, msg):
        self.summary.setText(f"❌ 诊断失败：{msg}")
        self.table.setRowCount(0)

    def closeEvent(self, event):
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(2000)
        super().closeEvent(event)
