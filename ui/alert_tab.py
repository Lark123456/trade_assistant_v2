# -*- coding: utf-8 -*-
"""提醒设置页签：条件管理（价格/涨跌幅/成交量/MACD/KDJ/RSI）与提醒日志。"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QComboBox, QCompleter, QDoubleSpinBox, QHBoxLayout,
                             QLabel, QLineEdit, QMessageBox, QPushButton,
                             QSpinBox, QTableView, QVBoxLayout, QWidget)

from alert_engine import describe_alert
from data_feed import infer_market
from push import CHANNELS, test_push
from ui.models import PandasModel

TYPE_MAP = [("price", "价格"), ("percent", "涨跌幅"), ("volume", "成交量"),
            ("macd_cross", "MACD"), ("kdj", "KDJ"), ("rsi", "RSI6"),
            ("strategy_signal", "策略信号")]

ALERT_COLUMNS = ["id", "code", "name", "描述", "策略版本", "threshold", "cooldown_min",
                 "enabled", "triggered_count", "last_triggered_at"]
LOG_COLUMNS = ["id", "created_at", "code", "name", "message"]


class AlertTab(QWidget):
    def __init__(self, worker, db, names=None, parent=None):
        super().__init__(parent)
        self.worker = worker
        self.db = db
        self._names = names or {}
        self._build_ui()
        self._load()

    def _build_ui(self):
        root = QVBoxLayout(self)

        form = QHBoxLayout()
        form.addWidget(QLabel("代码:"))
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("600000")
        self.code_edit.setMaximumWidth(110)
        self.code_edit.textChanged.connect(self._fill_name)
        if self._names:
            completer = QCompleter([f"{k} {v}" for k, v in self._names.items()], self)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            self.code_edit.setCompleter(completer)
        form.addWidget(self.code_edit)
        self.name_label = QLabel("")
        self.name_label.setMinimumWidth(80)
        form.addWidget(self.name_label)
        form.addWidget(QLabel("类型:"))
        self.type_combo = QComboBox()
        for _, label in TYPE_MAP:
            self.type_combo.addItem(label)
        self.type_combo.currentIndexChanged.connect(self._sync_operator)
        form.addWidget(self.type_combo)
        form.addWidget(QLabel("条件:"))
        self.op_combo = QComboBox()
        form.addWidget(self.op_combo)
        self.thr_label = QLabel("阈值:")
        form.addWidget(self.thr_label)
        self.thr_spin = QDoubleSpinBox()
        self.thr_spin.setRange(-99999, 999999)
        self.thr_spin.setDecimals(3)
        self.thr_spin.setValue(0)
        form.addWidget(self.thr_spin)
        self.strategy_combo = QComboBox()
        self.strategy_combo.setVisible(False)
        self.strategy_combo.setMinimumWidth(220)
        self.strategy_combo.setToolTip("选择要盯的策略：盘中该股票满足策略信号即提醒（保存公式快照+编号版本）")
        form.addWidget(self.strategy_combo)
        form.addWidget(QLabel("冷却(分):"))
        self.cool_spin = QSpinBox()
        self.cool_spin.setRange(0, 10080)
        self.cool_spin.setValue(60)
        form.addWidget(self.cool_spin)
        add_btn = QPushButton("添加提醒")
        add_btn.clicked.connect(self._add)
        form.addWidget(add_btn)
        self._sync_operator()
        root.addLayout(form)

        # 推送配置
        pbar = QHBoxLayout()
        pbar.addWidget(QLabel("📱 推送:"))
        self.push_channel = QComboBox()
        for key, label in CHANNELS.items():
            self.push_channel.addItem(label, key)
        pbar.addWidget(self.push_channel)
        self.push_key = QLineEdit()
        self.push_key.setPlaceholderText("Server酱 SendKey 或 Bark Key")
        self.push_key.setMinimumWidth(260)
        pbar.addWidget(self.push_key)
        test_btn = QPushButton("测试推送")
        test_btn.clicked.connect(self._test_push)
        save_push_btn = QPushButton("保存推送配置")
        save_push_btn.clicked.connect(self._save_push)
        pbar.addWidget(test_btn)
        pbar.addWidget(save_push_btn)
        pbar.addStretch(1)
        root.addLayout(pbar)
        self._load_push()

        bar = QHBoxLayout()
        toggle_btn = QPushButton("启用/禁用")
        toggle_btn.clicked.connect(self._toggle)
        del_btn = QPushButton("删除")
        del_btn.clicked.connect(self._delete)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._load)
        bar.addWidget(toggle_btn)
        bar.addWidget(del_btn)
        bar.addWidget(refresh_btn)
        bar.addStretch(1)
        root.addLayout(bar)

        self.table = QTableView()
        self.table.setModel(PandasModel(None))
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        root.addWidget(self.table, stretch=3)

        root.addWidget(QLabel("◆ 提醒日志"))
        self.log_table = QTableView()
        self.log_table.setModel(PandasModel(None))
        self.log_table.setAlternatingRowColors(True)
        root.addWidget(self.log_table, stretch=2)

    def _sync_operator(self):
        self.op_combo.clear()
        atype = TYPE_MAP[self.type_combo.currentIndex()][0]
        if atype in ("macd_cross", "kdj"):
            self.op_combo.addItems(["金叉", "死叉"])
        elif atype == "strategy_signal":
            self.op_combo.addItem("命中即提醒")
        else:
            self.op_combo.addItems([">=", "<=", ">", "<"])
        is_ss = atype == "strategy_signal"
        self.op_combo.setVisible(True)
        self.thr_label.setVisible(not is_ss)
        self.thr_spin.setVisible(not is_ss)
        self.strategy_combo.setVisible(is_ss)
        if is_ss:
            self._reload_strategies()

    def _reload_strategies(self):
        """策略下拉：列出策略管理中的全部非指标策略（含编号与启停状态）。"""
        cur = self.strategy_combo.currentData()
        self.strategy_combo.blockSignals(True)
        self.strategy_combo.clear()
        for s in self.db.list_strategies():
            if s.get("kind") == "indicator":
                continue
            state = "启用" if s.get("enabled") else "停用"
            self.strategy_combo.addItem(
                f"{s['name']}（{s.get('code', '')}·{state}）", s["id"])
        self.strategy_combo.blockSignals(False)
        if cur is not None:
            idx = self.strategy_combo.findData(cur)
            if idx >= 0:
                self.strategy_combo.setCurrentIndex(idx)

    def _fill_name(self, text):
        code = text.strip().zfill(6) if text.strip() else ""
        self.name_label.setText(self._names.get(code, ""))

    # ------------------------------------------------------------ 数据
    def _load(self):
        rows = self.db.list_alerts()
        import pandas as pd
        out = []
        for r in rows:
            params = r.get("params") or {}
            ver_text = ""
            if r.get("alert_type") == "strategy_signal":
                ver_text = f"{params.get('strategy_code', '')} v{params.get('strategy_version', '')}"
            out.append({
                "id": r["id"], "code": r["code"], "name": r["name"],
                "描述": describe_alert(r),
                "策略版本": ver_text,
                "threshold": r["threshold"],
                "cooldown_min": int(float(params.get("cooldown", 3600)) / 60),
                "enabled": "启用" if r["enabled"] else "停用",
                "triggered_count": r["triggered_count"],
                "last_triggered_at": r["last_triggered_at"] or "",
            })
        df = pd.DataFrame(out)
        self.table.model().setDataFrame(df if not df.empty else df)

        logs = self.db.list_alert_logs()
        self.log_table.model().setDataFrame(pd.DataFrame(logs))

    # ------------------------------------------------------------ 操作
    def _add(self):
        code = self.code_edit.text().strip()
        if not code or not code.isdigit():
            QMessageBox.warning(self, "提示", "请输入有效的股票代码")
            return
        code = code.zfill(6)
        atype = TYPE_MAP[self.type_combo.currentIndex()][0]
        op_text = self.op_combo.currentText()
        op = {"金叉": "cross_up", "死叉": "cross_down"}.get(op_text, op_text)
        thr = self.thr_spin.value()
        cooldown = self.cool_spin.value() * 60
        params = {"cooldown": cooldown}
        if atype == "strategy_signal":
            # 策略信号提醒：绑定策略编号+版本，公式存快照（改策略不影响本提醒）
            sid = self.strategy_combo.currentData()
            if sid is None:
                QMessageBox.warning(self, "提示", "策略列表为空，请先到「策略管理」添加策略")
                return
            st = self.db.get_strategy(int(sid))
            if st is None or not (st.get("formula") or "").strip():
                QMessageBox.warning(self, "提示", "所选策略不存在或没有公式")
                return
            params.update({
                "strategy_id": st["id"],
                "strategy_code": st.get("code", ""),
                "strategy_name": st.get("name", ""),
                "strategy_version": self.db.get_strategy_latest_version(st["id"]),
                "formula": st.get("formula", ""),
            })
            op = "signal"
            thr = 0.0
        self.db.add_alert(code=code, name=self.name_label.text() or code,
                          market=infer_market(code), alert_type=atype,
                          operator=op, threshold=thr,
                          params=params)
        self.code_edit.clear()
        self.name_label.clear()
        self._load()

    def _selected(self):
        idx = self.table.currentIndex()
        df = self.table.model().dataframe()
        return df.iloc[idx.row()] if idx.isValid() and 0 <= idx.row() < len(df) else None

    def _toggle(self):
        rec = self._selected()
        if rec is None:
            return
        rows = self.db.list_alerts()
        for r in rows:
            if r["id"] == int(rec["id"]):
                self.db.update_alert(r["id"], enabled=0 if r["enabled"] else 1)
                break
        self._load()

    def _delete(self):
        rec = self._selected()
        if rec is None:
            return
        if QMessageBox.question(self, "确认", f"删除提醒 id={rec['id']}？") == QMessageBox.Yes:
            self.db.delete_alert(int(rec["id"]))
            self._load()

    # ------------------------------------------------------------ 推送配置
    def _load_push(self):
        channel = self.db.get_setting("push_channel", "none")
        key = self.db.get_setting("push_key", "")
        idx = self.push_channel.findData(channel)
        if idx >= 0:
            self.push_channel.setCurrentIndex(idx)
        self.push_key.setText(key)

    def _save_push(self):
        self.db.set_setting("push_channel", self.push_channel.currentData())
        self.db.set_setting("push_key", self.push_key.text().strip())
        self.statusBarHint("推送配置已保存")

    def _test_push(self):
        channel = self.push_channel.currentData()
        key = self.push_key.text().strip()
        if channel == "none" or not key:
            QMessageBox.warning(self, "提示", "请先选择推送渠道并填写 Key")
            return
        ok, msg = test_push(channel, key)
        if ok:
            QMessageBox.information(self, "测试推送", f"✅ 推送成功（{msg}）")
        else:
            QMessageBox.warning(self, "测试推送", f"❌ 推送失败：{msg}")

    def statusBarHint(self, text):
        win = self.window()
        if hasattr(win, "statusBar"):
            win.statusBar().showMessage(text, 5000)
