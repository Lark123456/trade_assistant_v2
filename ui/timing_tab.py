# -*- coding: utf-8 -*-
"""择时模块（v0.8.8 极简版）。

极简 UI：两个巨大的 [空头] / [多头] 按钮各占一半屏幕，点击点亮作为视觉提醒，
并在 settings 表里持久化当前选择（程序重启后恢复）。

无任何数据获取、无任何指标计算。
"""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (QHBoxLayout, QLabel, QMessageBox, QPushButton,
                             QVBoxLayout, QWidget)

SETTING_REGIME = "timing.regime"   # "bull" | "bear" | ""（空=未选）

# 配色（与项目 A 股惯例一致：红涨绿跌 → 但这里 多头=涨=红，空头=跌=绿）
BULL_COLOR = "#e64340"   # 多头 = 红（涨）
BEAR_COLOR = "#089981"   # 空头 = 绿（跌）
DIM       = "#cccccc"   # 未选 = 灰


class BigRegimeButton(QPushButton):
    """单个巨大按钮。"""

    def __init__(self, kind: str, label: str, parent=None):
        """
        kind: "bull" | "bear"
        label: 按钮文字（如 "多头区间" / "空头区间"）
        """
        super().__init__(label, parent)
        self.kind = kind
        self.active_color = BULL_COLOR if kind == "bull" else BEAR_COLOR
        self.setMinimumHeight(400)
        f = self.font()
        f.setBold(True)
        f.setPointSize(28)
        self.setFont(f)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self._apply_style(active=False)
        self.toggled.connect(self._on_toggled)

    def _apply_style(self, active: bool):
        if active:
            bg = self.active_color
            fg = "white"
            border = self.active_color
        else:
            bg = "#f5f5f5"
            fg = "#999999"
            border = DIM
        self.setStyleSheet(f"""
            QPushButton {{
                background:{bg};
                color:{fg};
                border:3px solid {border};
                border-radius:8px;
                padding:20px;
            }}
            QPushButton:hover {{ opacity:0.9; }}
        """)

    def _on_toggled(self, checked: bool):
        self._apply_style(active=checked)
        # 通知 Tab（由外部连接）
        if hasattr(self, "_on_state_changed") and self._on_state_changed:
            self._on_state_changed(self.kind, checked)


class TimingTab(QWidget):
    """两个巨大按钮：空头 / 多头，各占一半."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 16)
        root.setSpacing(10)

        # 顶部状态栏 + 清除
        top = QHBoxLayout()
        self.status_label = QLabel("当前：未选择")
        f = self.status_label.font()
        f.setBold(True); f.setPointSize(11)
        self.status_label.setFont(f)
        top.addWidget(self.status_label)
        top.addStretch(1)
        clear_btn = QPushButton("清除选择")
        clear_btn.clicked.connect(self._on_clear)
        top.addWidget(clear_btn)
        root.addLayout(top)

        # 主体：两个按钮各占一半
        body = QHBoxLayout()
        body.setSpacing(8)

        self.btn_bull = BigRegimeButton("bull", "📈 多头区间")
        self.btn_bear = BigRegimeButton("bear", "📉 空头区间")

        # 互斥：点一个时取消另一个
        self.btn_bull._on_state_changed = self._on_bull_changed
        self.btn_bear._on_state_changed = self._on_bear_changed
        self.btn_bull.toggled.connect(self._on_bull_toggled)
        self.btn_bear.toggled.connect(self._on_bear_toggled)

        body.addWidget(self.btn_bull, 1)
        body.addWidget(self.btn_bear, 1)
        root.addLayout(body, 1)

        # 底部提示
        tip = QLabel("提示：点击按钮即可点亮/熄灭当前所处区间；选择会持久化，下次启动自动恢复。")
        tip.setStyleSheet("color:#888; font-size:10px;")
        tip.setAlignment(Qt.AlignCenter)
        root.addWidget(tip)

        # 恢复上次选择
        self._restore()

    # ------------------- 状态恢复 / 保存
    def _restore(self):
        kind = ""
        try:
            kind = self.db.get_setting(SETTING_REGIME) or ""
        except Exception:
            pass
        if kind == "bull":
            self.btn_bull.setChecked(True)
        elif kind == "bear":
            self.btn_bear.setChecked(True)
        else:
            self._update_status("")

    def _save(self, kind: str):
        try:
            self.db.set_setting(SETTING_REGIME, kind)
        except Exception:
            pass

    # ------------------- 点击逻辑
    def _on_bull_toggled(self, checked: bool):
        # 互斥：选多头时取消空头（但保留自身 setChecked 状态）
        if checked:
            if self.btn_bear.isChecked():
                self.btn_bear.blockSignals(True)
                self.btn_bear.setChecked(False)
                self.btn_bear.blockSignals(False)

    def _on_bear_toggled(self, checked: bool):
        if checked:
            if self.btn_bull.isChecked():
                self.btn_bull.blockSignals(True)
                self.btn_bull.setChecked(False)
                self.btn_bull.blockSignals(False)

    def _on_bull_changed(self, kind: str, checked: bool):
        self._save("bull" if checked else "")
        self._update_status("bull" if checked else "")

    def _on_bear_changed(self, kind: str, checked: bool):
        self._save("bear" if checked else "")
        self._update_status("bear" if checked else "")

    def _on_clear(self):
        self.btn_bull.blockSignals(True)
        self.btn_bear.blockSignals(True)
        self.btn_bull.setChecked(False)
        self.btn_bear.setChecked(False)
        self.btn_bull.blockSignals(False)
        self.btn_bear.blockSignals(False)
        self._save("")
        self._update_status("")

    # ------------------- 状态显示
    def _update_status(self, kind: str):
        if kind == "bull":
            text = "当前：<span style='color:#e64340; font-weight:bold;'>📈 多头区间</span>"
        elif kind == "bear":
            text = "当前：<span style='color:#089981; font-weight:bold;'>📉 空头区间</span>"
        else:
            text = "当前：<span style='color:#999;'>未选择</span>"
        self.status_label.setTextFormat(Qt.RichText)
        self.status_label.setText(text)
