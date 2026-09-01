# -*- coding: utf-8 -*-
"""通达信自选股板块双向同步对话框（v0.8.2）。

从观察池页签打开，提供：
- 导出：观察池选中行 / 全部 → 写入通达信自定义板块（在通达信里浏览 K 线）
- 导入：读取通达信板块 → 一键加入观察池（status=待审核，标注来源）

TDX 路径自动探测：常见安装路径 + 注册表，失败后可手动输入。
"""
import logging
import os

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog,
                             QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                             QPushButton, QTableWidget, QTableWidgetItem,
                             QVBoxLayout)

from data_feed import infer_market
from tdx_sync import TdxBlockSync

log = logging.getLogger("tdx_sync_dialog")

# 常见通达信安装根目录（自动探测用）
DEFAULT_TDX_ROOTS = [
    r"E:\TDX",
    r"C:\new_tdx",
    r"D:\new_tdx",
    r"C:\zd_tdx",
    r"D:\zd_tdx",
    r"E:\new_tdx",
    r"C:\tdxw",
]


def detect_tdx_root() -> str:
    """自动探测通达信根目录（返回第一个含 T0002/blocknew 的路径）。"""
    for root in DEFAULT_TDX_ROOTS:
        if TdxBlockSync.is_valid_root(root):
            return root
    return ""


class TdxSyncDialog(QDialog):
    """通达信板块同步子界面：上半导出、下半导入。

    dest 目标（v0.8.5 导入分流）：
    - "watch_pool"（默认）：导入进**一级观察池大模块**（watch_pool 表）
    - "calendar:YYYY-MM-DD"：导入进**行情日历该日期的二级观察池**
      （tdx_imports 表，独立于一级观察池，仅在该日期的日历观察池子页显示）
    """

    def __init__(self, db, worker, watch_codes_provider, watch_rows_provider,
                 on_imported=None, parent=None, dest="watch_pool"):
        """参数：
        - db: Database 实例
        - worker: Worker（导入后需要名称映射时用 get_stock_df）
        - watch_codes_provider: callable -> set[str]，目标池当前全部代码（判断去重）
        - watch_rows_provider: callable -> list[dict]，观察池当前行（含 代码/名称，
          用于导出；调用方决定导出范围——全部或选中行）
        - on_imported: callable，导入成功后的回调（刷新目标池视图）
        - dest: 导入目标（见类注释）
        """
        super().__init__(parent)
        self.db = db
        self.worker = worker
        self._watch_codes_provider = watch_codes_provider
        self._watch_rows_provider = watch_rows_provider
        self.on_imported = on_imported
        # v0.8.5：导入目标分流（watch_pool / calendar:日期）
        self.dest = dest or "watch_pool"
        self.calendar_date = None
        if self.dest.startswith("calendar:"):
            self.calendar_date = self.dest.split(":", 1)[1]
        self.dest_label_text = (
            f"导入目标：行情日历 {self.calendar_date} 二级观察池（独立于一级观察池）"
            if self.calendar_date else "导入目标：一级观察池大模块")

        self.setWindowTitle("🔄 通达信自选股同步")
        self.resize(760, 560)

        root = QVBoxLayout(self)

        # ---------- TDX 路径 ----------
        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("通达信目录:"))
        self.path_edit = QLineEdit(detect_tdx_root())
        path_row.addWidget(self.path_edit, stretch=1)
        browse_btn = QPushButton("浏览…")
        browse_btn.clicked.connect(self._browse_root)
        path_row.addWidget(browse_btn)
        reload_btn = QPushButton("刷新板块")
        reload_btn.clicked.connect(self._reload_blocks)
        path_row.addWidget(reload_btn)
        root.addLayout(path_row)
        self.root_status = QLabel("")
        root.addWidget(self.root_status)

        # ---------- 导出区 ----------
        root.addWidget(QLabel("──────── ① 导出到通达信（在通达信里浏览 K 线）────────"))
        exp_row = QHBoxLayout()
        exp_row.addWidget(QLabel("目标板块名:"))
        self.export_name = QLineEdit("程序选股")
        self.export_name.setMaximumWidth(160)
        self.export_name.setToolTip("通达信板块名上限 4 个汉字（8 字节 GBK），超长自动截断")
        exp_row.addWidget(self.export_name)
        self.export_mode = QComboBox()
        self.export_mode.addItem("覆盖板块", "overwrite")
        self.export_mode.addItem("追加到板块", "append")
        exp_row.addWidget(self.export_mode)
        self.export_all_check_holder = None  # 由外部设置默认范围提示
        self.do_export_btn = QPushButton("导出所选/全部 →")
        self.do_export_btn.clicked.connect(self._do_export)
        exp_row.addWidget(self.do_export_btn)
        exp_row.addStretch(1)
        root.addLayout(exp_row)
        self.export_hint = QLabel(
            "导出范围：观察池中「勾选/选中」的行；未选中则导出全部当前行。")
        self.export_hint.setStyleSheet("color:#888;")
        root.addWidget(self.export_hint)
        self.export_result = QLabel("")
        self.export_result.setStyleSheet("color:#c0392b;")
        root.addWidget(self.export_result)

        # ---------- 导入区 ----------
        dest_label = QLabel(self.dest_label_text)
        dest_label.setStyleSheet("color:#0f6ab4; font-weight:bold;")
        root.addWidget(dest_label)
        root.addWidget(QLabel("──────── ② 从通达信导入────────"))
        imp_row = QHBoxLayout()
        imp_row.addWidget(QLabel("选择板块:"))
        self.block_combo = QComboBox()
        self.block_combo.setMinimumWidth(220)
        imp_row.addWidget(self.block_combo, stretch=1)
        preview_btn = QPushButton("预览")
        preview_btn.clicked.connect(self._preview_import)
        imp_row.addWidget(preview_btn)
        self.do_import_btn = QPushButton("导入 → 观察池")
        self.do_import_btn.clicked.connect(self._do_import)
        imp_row.addWidget(self.do_import_btn)
        root.addLayout(imp_row)

        # 选择辅助行（全选/反选/全不选/仅未在池/统计）
        sel_row = QHBoxLayout()
        sel_all_btn = QPushButton("全选")
        sel_all_btn.clicked.connect(lambda: self._set_all_checks(Qt.Checked))
        sel_row.addWidget(sel_all_btn)
        sel_none_btn = QPushButton("全不选")
        sel_none_btn.clicked.connect(lambda: self._set_all_checks(Qt.Unchecked))
        sel_row.addWidget(sel_none_btn)
        sel_inv_btn = QPushButton("反选")
        sel_inv_btn.clicked.connect(self._invert_checks)
        sel_row.addWidget(sel_inv_btn)
        self.only_new_check = QCheckBox("仅显示未在池")
        self.only_new_check.setChecked(True)
        self.only_new_check.toggled.connect(self._refresh_preview_view)
        sel_row.addWidget(self.only_new_check)
        sel_row.addStretch(1)
        self.import_hint = QLabel("")
        self.import_hint.setStyleSheet("color:#888;")
        sel_row.addWidget(self.import_hint)
        root.addLayout(sel_row)

        # 5 列：☐ 勾选 + 代码 + 名称 + 市场 + 在观察池?
        self.preview_table = QTableWidget(0, 5)
        self.preview_table.setHorizontalHeaderLabels(
            ["☐", "代码", "名称", "市场", "在观察池?"])
        self.preview_table.horizontalHeader().setStretchLastSection(True)
        self.preview_table.setColumnWidth(0, 36)
        self.preview_table.setColumnWidth(1, 90)
        self.preview_table.setColumnWidth(2, 160)
        self.preview_table.setColumnWidth(3, 70)
        self.preview_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.preview_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.preview_table.cellChanged.connect(self._on_cell_changed)
        root.addWidget(self.preview_table, stretch=1)

        # ---------- 底部提示 ----------
        if self.calendar_date:
            tip_text = ("提示：导出后需重启通达信才能看到新板块；通达信退出时可能回写板块配置，"
                        "建议先关通达信再导出。导入的票进入**行情日历该日期的二级观察池**"
                        "（独立于一级观察池，仅在该日期日历的「观察池」子页可见，可随时删除）。")
        else:
            tip_text = ("提示：导出后需重启通达信才能看到新板块；通达信退出时可能回写板块配置，"
                        "建议先关通达信再导出。导入的票进入一级观察池（待审核），来源标注「通达信导入」。")
        tip = QLabel(tip_text)
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#888;")
        root.addWidget(tip)

        # 初始化
        self._names = {}
        self._previewed = False   # v0.8.5：是否点过预览（防止 0 行时误走"全量导入"兜底）
        if worker is not None:
            try:
                stock_df = worker.get_stock_df()
                if stock_df is not None and not stock_df.empty:
                    self._names = {str(r.code): str(r.name) for r in stock_df.itertuples()}
            except Exception:  # noqa: BLE001
                pass
        self._reload_blocks()

    # ------------------------------------------------------------ 路径
    def _current_sync(self):
        root = self.path_edit.text().strip()
        if not root or not TdxBlockSync.is_valid_root(root):
            self.root_status.setText("⚠️ 目录无效：需要包含 T0002\\blocknew 子目录")
            self.root_status.setStyleSheet("color:#c0392b;")
            return None
        self.root_status.setText("✅ 通达信目录有效")
        self.root_status.setStyleSheet("color:#0f9d58;")
        return TdxBlockSync(root)

    def _browse_root(self):
        d = QFileDialog.getExistingDirectory(self, "选择通达信安装根目录",
                                             self.path_edit.text().strip() or "E:\\")
        if d:
            self.path_edit.setText(d)
            self._reload_blocks()

    def _reload_blocks(self):
        sync = self._current_sync()
        self.block_combo.clear()
        if sync is None:
            return
        for name, fname in sync.list_blocks():
            self.block_combo.addItem(name, fname)

    # ------------------------------------------------------------ 导出
    def _do_export(self):
        sync = self._current_sync()
        if sync is None:
            return
        rows = self._watch_rows_provider() or []
        if not rows:
            QMessageBox.information(self, "提示", "观察池没有可导出的行")
            return
        block_name = self.export_name.text().strip()
        if not block_name:
            QMessageBox.information(self, "提示", "请填写目标板块名")
            return
        codes = []
        for r in rows:
            code = str(r.get("代码") or r.get("code") or "").zfill(6)
            if code:
                codes.append(code)
        overwrite = self.export_mode.currentData() == "overwrite"
        try:
            n, path = sync.export_block(codes, block_name, overwrite=overwrite)
        except PermissionError as e:
            QMessageBox.critical(self, "写入失败",
                                 f"板块文件被占用（通达信可能正在运行）：\n{e}\n\n"
                                 "请先关闭通达信后重试。")
            return
        except OSError as e:
            QMessageBox.critical(self, "写入失败", str(e))
            return
        self.export_result.setText(
            f"✅ 已写入 {n} 只 → {path}\n重启通达信后即可在自定义板块中查看。")

    # ------------------------------------------------------------ 导入
    def _preview_import(self):
        sync = self._current_sync()
        if sync is None:
            return
        fname = self.block_combo.currentData()
        if not fname:
            return
        self._previewed = True   # v0.8.5：记录已预览（_do_import 据此判断是否兜底全量导入）
        # 暂时断开 cellChanged 防止 setItem 时触发 _on_cell_changed 反复重算
        try:
            self.preview_table.cellChanged.disconnect(self._on_cell_changed)
        except TypeError:
            pass

        stocks = sync.import_block(fname)
        watch_codes = self._watch_codes_provider() or set()
        only_new = self.only_new_check.isChecked()
        # 保存当前所有票（不论是否显示），供切换 "仅未在池" 过滤时复用
        self._all_stocks = stocks
        self._all_watch_codes = watch_codes
        self._all_names_map = self._names  # 用于 _refresh_preview_view

        self._populate_preview(stocks, watch_codes, self._names, only_new)

        # 恢复 cellChanged 连接
        self.preview_table.cellChanged.connect(self._on_cell_changed)
        self._update_import_hint()

    def _populate_preview(self, stocks, watch_codes, names, only_new):
        """把 stocks 填充到 preview_table（应用"仅未在池"过滤 + 默认勾选状态）。"""
        self.preview_table.setRowCount(0)
        for st in stocks:
            in_watch = st.code in watch_codes
            if only_new and in_watch:
                continue  # 过滤掉已在池的（不在表格中显示）
            row = self.preview_table.rowCount()
            self.preview_table.insertRow(row)
            name = (names or {}).get(st.code, "")
            market_label = {0: "深", 1: "沪", 2: "北"}.get(st.market, str(st.market))
            # 第 0 列：勾选框（仅未在池默认勾上）
            chk = QTableWidgetItem()
            chk.setFlags(chk.flags() | Qt.ItemIsUserCheckable)
            chk.setCheckState(Qt.Checked if not in_watch else Qt.Unchecked)
            chk.setTextAlignment(Qt.AlignCenter)
            self.preview_table.setItem(row, 0, chk)
            self.preview_table.setItem(row, 1, QTableWidgetItem(st.code))
            self.preview_table.setItem(row, 2, QTableWidgetItem(name))
            self.preview_table.setItem(row, 3, QTableWidgetItem(market_label))
            in_text = "✅ 已在池" if in_watch else ""
            item = QTableWidgetItem(in_text)
            if in_watch:
                item.setForeground(Qt.gray)
            self.preview_table.setItem(row, 4, item)

    def _refresh_preview_view(self):
        """切换「仅未在池」复选框后重新填充。"""
        if getattr(self, "_all_stocks", None) is None:
            return
        # 切换时也要重设勾选默认值
        try:
            self.preview_table.cellChanged.disconnect(self._on_cell_changed)
        except TypeError:
            pass
        self._populate_preview(self._all_stocks, self._all_watch_codes,
                              self._all_names_map,
                              self.only_new_check.isChecked())
        self.preview_table.cellChanged.connect(self._on_cell_changed)
        self._update_import_hint()

    def _selected_codes(self):
        """返回当前预览表中**勾选 + 可见**的代码列表。"""
        codes = []
        for row in range(self.preview_table.rowCount()):
            item = self.preview_table.item(row, 0)
            if item and item.checkState() == Qt.Checked:
                code_item = self.preview_table.item(row, 1)
                if code_item:
                    codes.append(code_item.text())
        return codes

    def _set_all_checks(self, state):
        """批量勾选 / 全清空。"""
        # 暂时断开 cellChanged 防止 setCheckState 触发多次统计
        try:
            self.preview_table.cellChanged.disconnect(self._on_cell_changed)
        except TypeError:
            pass
        for row in range(self.preview_table.rowCount()):
            item = self.preview_table.item(row, 0)
            if item:
                item.setCheckState(state)
        self.preview_table.cellChanged.connect(self._on_cell_changed)
        self._update_import_hint()

    def _invert_checks(self):
        try:
            self.preview_table.cellChanged.disconnect(self._on_cell_changed)
        except TypeError:
            pass
        for row in range(self.preview_table.rowCount()):
            item = self.preview_table.item(row, 0)
            if item:
                item.setCheckState(Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked)
        self.preview_table.cellChanged.connect(self._on_cell_changed)
        self._update_import_hint()

    def _on_cell_changed(self, row, col):
        # 只在第 0 列（勾选列）的变化时刷新统计
        if col == 0:
            self._update_import_hint()

    def _update_import_hint(self):
        total = self.preview_table.rowCount()
        selected = len(self._selected_codes())
        self.import_hint.setText(f"共 {total} 只可导，已勾选 {selected} 只")

    def _do_import(self):
        sync = self._current_sync()
        if sync is None:
            return
        fname = self.block_combo.currentData()
        if not fname:
            return
        stocks = sync.import_block(fname)
        if not stocks:
            QMessageBox.information(self, "提示", "该板块为空或读取失败")
            return
        # v0.8.4：只导入用户在预览表中**勾选**的票（默认勾上"仅未在池"）
        all_checked_codes = set(self._selected_codes())  # 勾选 + 当前可见
        # 板块内代码全集（用于容错：用户未预览时直接全导入）
        all_block_codes = {st.code for st in stocks}
        if not getattr(self, "_previewed", False):
            # 用户没点过预览 → 兜底导入全部板块票
            selected_set = all_block_codes
        else:
            selected_set = all_checked_codes
        if not selected_set:
            QMessageBox.information(self, "提示", "未勾选任何股票，未导入。\n"
                                            "请勾选要加入观察池的股票，或取消「仅未在池」显示全部后再勾选。")
            return

        if self.calendar_date:
            self._do_import_calendar(stocks, selected_set, fname)
        else:
            self._do_import_watch_pool(stocks, selected_set, fname)

    def _do_import_watch_pool(self, stocks, selected_set, fname):
        """v0.8.5：导入进一级观察池大模块（watch_pool，原行为）。"""
        watch_codes = self._watch_codes_provider() or set()
        added = skipped = 0
        for st in stocks:
            if st.code not in selected_set:
                continue
            if st.code in watch_codes:
                skipped += 1
                continue
            self.db.add_watch(
                code=st.code,
                name=self._names.get(st.code, ""),
                market=infer_market(st.code),
                trigger_indicator="通达信导入",
                entry_price=None,
                reason=f"通达信板块导入（{self.block_combo.currentText()}）",
                status="待审核",
                level=1,
            )
            watch_codes.add(st.code)
            added += 1
        if self.on_imported and added:
            self.on_imported()
        QMessageBox.information(
            self, "导入完成",
            f"新加入一级观察池 {added} 只（待审核），已在池跳过 {skipped} 只。")

    def _do_import_calendar(self, stocks, selected_set, fname):
        """v0.8.5：导入进行情日历 {self.calendar_date} 的二级观察池（tdx_imports 表，
        独立于一级观察池；仅在该日期的日历「观察池」子页显示，可随时删除）。"""
        day = self.calendar_date
        # 目标 = 当日二级观察池可见集合：watch_pool(add_date=day) ∪ tdx_imports(add_date=day)
        target_codes = {str(w["code"]) for w in self.db.list_watch()
                        if (w.get("add_date") or "") == day}
        target_codes |= {str(t["code"]) for t in self.db.list_tdx_imports(add_date=day)}
        added = skipped = 0
        for st in stocks:
            if st.code not in selected_set:
                continue
            if st.code in target_codes:
                skipped += 1
                continue
            self.db.add_tdx_import(
                code=st.code,
                name=self._names.get(st.code, ""),
                market=infer_market(st.code),
                add_date=day,
                block_name=self.block_combo.currentText(),
                reason=f"通达信板块导入（{self.block_combo.currentText()}）",
            )
            target_codes.add(st.code)
            added += 1
        if self.on_imported and added:
            self.on_imported()
        QMessageBox.information(
            self, "导入完成",
            f"已加入行情日历 {day} 的二级观察池 {added} 只（独立于一级观察池），"
            f"当日已存在跳过 {skipped} 只。")


def open_tdx_sync_dialog(db, worker, watch_codes_provider, watch_rows_provider,
                         on_imported=None, parent=None, dest="watch_pool"):
    """便捷入口：探测到 TDX 目录才弹窗，否则提示。dest 见 TdxSyncDialog。"""
    dlg = TdxSyncDialog(db, worker, watch_codes_provider, watch_rows_provider,
                        on_imported=on_imported, parent=parent, dest=dest)
    dlg.exec_()
