# -*- coding: utf-8 -*-
"""数据中心页签（v0.11.0）：本地板块/成分查看 + 东财板块手动同步。"""
from __future__ import annotations

import logging

from PyQt5.QtCore import QThread, Qt, pyqtSignal
from PyQt5.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                             QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                             QMessageBox, QPlainTextEdit, QPushButton,
                             QSplitter, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)

log = logging.getLogger("data_center")

TYPE_LABELS = {
    "industry": "行业",
    "concept": "概念",
    "region": "地域",
    "custom": "自定义",
}


class BoardSyncThread(QThread):
    """后台运行 board_sync.sync_boards，避免卡住界面。"""

    progress = pyqtSignal(str)
    done = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, db, full=False, parent=None):
        super().__init__(parent)
        self.db = db
        self.full = full

    def run(self):
        try:
            from board_sync import sync_boards
            stats = sync_boards(self.db, full=self.full,
                               progress=self.progress.emit)
            self.done.emit(stats)
        except Exception as e:  # noqa: BLE001
            log.exception("手动同步板块失败")
            self.failed.emit(f"{type(e).__name__}: {e}")


class DataCenterTab(QWidget):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._thread = None
        self._boards = []
        self._names = {}
        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        root = QVBoxLayout(self)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("类型:"))
        self.type_combo = QComboBox()
        self.type_combo.addItem("全部", None)
        for key, label in TYPE_LABELS.items():
            self.type_combo.addItem(label, key)
        self.type_combo.currentIndexChanged.connect(self._apply_filter)
        bar.addWidget(self.type_combo)

        bar.addWidget(QLabel("板块:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("输入板块名称筛选")
        self.search_edit.setMaximumWidth(200)
        self.search_edit.textChanged.connect(self._apply_filter)
        bar.addWidget(self.search_edit)

        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh)
        bar.addWidget(refresh_btn)
        bar.addStretch(1)

        self.full_check = QCheckBox("全量重拉")
        self.full_check.setToolTip("不勾选时，7 天内同步过的板块会自动跳过")
        bar.addWidget(self.full_check)

        self.sync_btn = QPushButton("同步东财板块")
        self.sync_btn.clicked.connect(self._start_sync)
        bar.addWidget(self.sync_btn)
        root.addLayout(bar)

        self.summary_label = QLabel("")
        self.summary_label.setStyleSheet("color:#666;padding:2px;")
        root.addWidget(self.summary_label)

        split = QSplitter(Qt.Horizontal)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(QLabel("板块列表"))
        self.board_table = QTableWidget(0, 4)
        self.board_table.setHorizontalHeaderLabels(["类型", "板块", "成分", "最近同步"])
        self._setup_table(self.board_table)
        self.board_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        self.board_table.itemSelectionChanged.connect(self._load_members)
        lv.addWidget(self.board_table)
        split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.members_label = QLabel("成分股（选中左侧板块查看）")
        rv.addWidget(self.members_label)
        self.member_table = QTableWidget(0, 2)
        self.member_table.setHorizontalHeaderLabels(["代码", "名称"])
        self._setup_table(self.member_table)
        self.member_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        rv.addWidget(self.member_table)
        split.addWidget(right)

        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)

        log_bar = QHBoxLayout()
        log_bar.addWidget(QLabel("同步日志"))
        log_bar.addStretch(1)
        clear_btn = QPushButton("清空日志")
        clear_btn.clicked.connect(lambda: self.log_edit.clear())
        log_bar.addWidget(clear_btn)
        root.addLayout(log_bar)

        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumHeight(150)
        self.log_edit.setPlaceholderText("点击「同步东财板块」后，进度与结果会显示在这里")
        root.addWidget(self.log_edit)

    @staticmethod
    def _setup_table(table):
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)

    # ------------------------------------------------------------ 数据加载
    def refresh(self):
        self._names = {r["code"]: r.get("name") or ""
                       for r in self.db.load_stock_list()}
        self._boards = self.db.list_boards(active_only=True)
        self._apply_filter()
        self._load_members()   # 列表重建后选中项已失效，清空右侧成分
        self._update_summary()

    def _update_summary(self):
        counts = {}
        for b in self._boards:
            counts[b["board_type"]] = counts.get(b["board_type"], 0) + 1
        parts = "｜".join(f"{TYPE_LABELS.get(k, k)} {v}"
                          for k, v in sorted(counts.items()))
        total = self.db.get_board_member_total()
        self.summary_label.setText(
            f"共 {len(self._boards)} 个板块（{parts or '暂无'}），"
            f"有效归属 {total:,} 条")

    def _current_type(self):
        return self.type_combo.currentData()

    def _apply_filter(self):
        bt = self._current_type()
        keyword = self.search_edit.text().strip().lower()
        rows = [b for b in self._boards
                if (bt is None or b["board_type"] == bt)
                and (not keyword or keyword in (b["name"] or "").lower())]

        self.board_table.setUpdatesEnabled(False)
        self.board_table.setRowCount(len(rows))
        for i, b in enumerate(rows):
            type_item = QTableWidgetItem(
                TYPE_LABELS.get(b["board_type"], b["board_type"]))
            type_item.setData(Qt.UserRole, b)
            self.board_table.setItem(i, 0, type_item)
            self.board_table.setItem(
                i, 1, QTableWidgetItem(b.get("name") or ""))
            count_item = QTableWidgetItem()
            count_item.setData(Qt.DisplayRole, int(b.get("stock_count") or 0))
            self.board_table.setItem(i, 2, count_item)
            self.board_table.setItem(
                i, 3, QTableWidgetItem((b.get("last_synced_at") or "未同步")
                                       .replace("T", " ")))
        self.board_table.resizeColumnToContents(0)
        self.board_table.resizeColumnToContents(2)
        self.board_table.resizeColumnToContents(3)
        self.board_table.setUpdatesEnabled(True)

    def _selected_board(self):
        items = self.board_table.selectedItems()
        if not items:
            return None
        return self.board_table.item(items[0].row(), 0).data(Qt.UserRole)

    def _load_members(self):
        board = self._selected_board()
        self.member_table.setRowCount(0)
        if not board:
            self.members_label.setText("成分股（选中左侧板块查看）")
            return
        codes = self.db.get_board_members(board["id"])
        self.members_label.setText(
            f"成分股：{board.get('name')}（{TYPE_LABELS.get(board['board_type'], board['board_type'])}，"
            f"{len(codes)} 只）")
        self.member_table.setUpdatesEnabled(False)
        self.member_table.setRowCount(len(codes))
        for i, code in enumerate(codes):
            self.member_table.setItem(i, 0, QTableWidgetItem(code))
            self.member_table.setItem(
                i, 1, QTableWidgetItem(self._names.get(code, "")))
        self.member_table.setUpdatesEnabled(True)

    # ------------------------------------------------------------ 同步
    def is_syncing(self):
        return self._thread is not None and self._thread.isRunning()

    def _start_sync(self):
        if self.is_syncing():
            return
        full = self.full_check.isChecked()
        self.sync_btn.setEnabled(False)
        self.sync_btn.setText("同步中…")
        self.log_edit.clear()
        self._append_log("开始同步东财板块（%s）…"
                         % ("全量重拉" if full else "增量，7 天内已同步的跳过"))

        self._thread = BoardSyncThread(self.db, full=full, parent=self)
        self._thread.progress.connect(self._append_log)
        self._thread.done.connect(self._on_sync_done)
        self._thread.failed.connect(self._on_sync_failed)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    def _append_log(self, text):
        text = str(text).strip("\n")
        if not text.strip():
            return
        self.log_edit.appendPlainText(text)
        self.log_edit.verticalScrollBar().setValue(
            self.log_edit.verticalScrollBar().maximum())

    def _on_sync_done(self, stats):
        if stats.get("skipped"):
            self._append_log("本次跳过：板块数据仍在有效期内，需要重拉请勾选「全量重拉」")
        else:
            self._append_log(
                f"完成：板块 {stats.get('boards_seen', 0)} 个，"
                f"刷新成分 {stats.get('boards_refreshed', 0)} 个，"
                f"失败 {stats.get('boards_failed', 0)} 个，"
                f"库内有效归属 {stats.get('members_total', 0):,} 条")
            if stats.get("failures"):
                self._append_log("失败板块（已保留旧数据）：")
                for b, err in stats["failures"][:10]:
                    self._append_log(f"  - {b['name']}({b['source_code']}): {err}")
        self.refresh()

    def _on_sync_failed(self, message):
        self._append_log(f"同步失败：{message}")
        QMessageBox.warning(self, "同步失败", message)

    def _on_thread_finished(self):
        self.sync_btn.setEnabled(True)
        self.sync_btn.setText("同步东财板块")
        self._thread = None
