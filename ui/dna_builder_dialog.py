# -*- coding: utf-8 -*-
"""🧬 股票形态黄金 DNA 模具制作器（PyQt5 版）。

流程：输入股票代码 + 起止日期 → 后台拉取前复权日K（v0.7.0）→
全序列计算黄白线（114天预热）→ 提取三维特征 → pyqtgraph 三维曲线核验 →
人工确认无误后保存入模具库（golden_templates.npz + templates_meta.json）。
"""
import datetime
import logging

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QComboBox, QDateEdit, QDialog, QHBoxLayout, QLabel,
                             QLineEdit, QMessageBox, QPushButton, QTableWidget,
                             QTableWidgetItem, QVBoxLayout)

import pyqtgraph as pg

from core.dna_engine import DNAFeatureExtractor, TemplateManager

log = logging.getLogger("dna_builder")

STRATEGY_CATEGORIES = ["B1回踩接力", "超跌反弹", "空中加油旗形"]

CURVE_STYLE = [
    ("价格走势 (0~1)", "#e64340"),
    ("量能衰减弧度 (0~1)", "#3c78f6"),
    ("支撑线贴合度 (0~1)", "#0f9d58"),
]


class FetchBarsThread(QThread):
    """后台拉取前复权日K，避免阻塞界面。"""
    done = pyqtSignal(object, str)   # (df, error)

    def __init__(self, feed, code, market, count=300, parent=None):
        super().__init__(parent)
        self.feed = feed
        self.code = code
        self.market = market
        self.count = count

    def run(self):
        try:
            df = self.feed.get_daily_bars(self.code, market=self.market,
                                          count=self.count, adjust="qfq")
            self.done.emit(df, "")
        except Exception as e:  # noqa: BLE001
            self.done.emit(None, str(e))


class DnaBuilderDialog(QDialog):
    """模具制作工作台：抓取 → 三维曲线核验 → 保存入库 / 删除。"""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.tm = TemplateManager()
        self._matrix = None          # 当前核验的特征矩阵
        self._meta = None            # 当前核验的元信息
        self._feed = None            # 延迟创建 DataFeed（避免启动即联网）
        self._thread = None
        self.setWindowTitle("🧬 股票形态黄金 DNA 模具制作器")
        self.resize(1080, 700)
        self._build_ui()
        self._refresh_template_list()

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        root = QVBoxLayout(self)

        top = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("◆ 1. 选取历史标杆波段"))

        form = QVBoxLayout()
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("股票代码"))
        self.code_edit = QLineEdit("002940")
        self.code_edit.setMaximumWidth(110)
        row1.addWidget(self.code_edit)
        row1.addWidget(QLabel("模具名称"))
        self.name_edit = QLineEdit("昂利康_经典B1缩量回踩")
        row1.addWidget(self.name_edit, 1)
        form.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("策略分类"))
        self.cat_combo = QComboBox()
        self.cat_combo.setEditable(True)
        for c in STRATEGY_CATEGORIES:
            self.cat_combo.addItem(c)
        self.cat_combo.setCurrentText("B1回踩接力")
        row2.addWidget(self.cat_combo, 1)
        row2.addWidget(QLabel("起"))
        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        self.start_date.setDisplayFormat("yyyy-MM-dd")
        self.start_date.setDate(datetime.date.today() - datetime.timedelta(days=30))
        row2.addWidget(self.start_date)
        row2.addWidget(QLabel("止"))
        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setDisplayFormat("yyyy-MM-dd")
        self.end_date.setDate(datetime.date.today())
        row2.addWidget(self.end_date)
        form.addLayout(row2)

        self.fetch_btn = QPushButton("🔍 抓取数据并提取 DNA 特征")
        self.fetch_btn.clicked.connect(self._fetch)
        self.save_btn = QPushButton("💾 确认无误，保存入黄金模具库")
        self.save_btn.setEnabled(False)
        self.save_btn.clicked.connect(self._save)
        btns = QHBoxLayout()
        btns.addWidget(self.fetch_btn)
        btns.addWidget(self.save_btn)
        form.addLayout(btns)

        self.status_label = QLabel(
            "提示：YL黄线需114天预热，系统自动拉取300天计算后再截取所选区间。"
            "绿线=支撑线贴合度（0.5踩线，白线优先/破位切黄线，±25%映射）。")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color:#666;")
        form.addWidget(self.status_label)

        left.addLayout(form)
        left.addStretch(1)

        right = QVBoxLayout()
        right.addWidget(QLabel("◆ 2. DNA 骨架曲线核验（无量纲三维特征）"))
        self.plot = pg.PlotWidget()
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.addLegend()
        self.plot.setLabel("left", "特征值域 [0,1]")
        self.plot.setLabel("bottom", "交易日序列")
        right.addWidget(self.plot, stretch=3)

        top.addLayout(left, 2)
        top.addLayout(right, 3)
        root.addLayout(top, stretch=3)

        # 模具库列表
        root.addWidget(QLabel("◆ 3. 已有黄金模具库"))
        self.tbl = QTableWidget(0, 7)
        self.tbl.setHorizontalHeaderLabels(
            ["模具名称", "股票", "代码", "分类", "区间", "天数", "操作"])
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setColumnWidth(0, 200)
        root.addWidget(self.tbl, stretch=2)

    # ------------------------------------------------------------ 数据抓取
    def _fetch(self):
        # 注意：必须在函数开头无条件导入！原代码把 import 放在
        # `if self._feed is None:` 块内，导致第二次点击时 infer_market
        # 局部变量未绑定，抛出 UnboundLocalError（被 Qt 静默吞掉，表现为按钮无反应）。
        from data_feed import DataFeed, infer_market

        # 防止重复点击
        if getattr(self, "_fetching", False):
            QMessageBox.warning(self, "提示", "正在抓取数据，请稍候…")
            return

        code = self.code_edit.text().strip()
        if not code:
            QMessageBox.warning(self, "提示", "请填写股票代码")
            return
        start = self.start_date.date().toString("yyyy-MM-dd")
        end = self.end_date.date().toString("yyyy-MM-dd")
        if start >= end:
            QMessageBox.warning(self, "提示", "起始日期必须早于结束日期")
            return

        # 清理旧线程：若上一次抓取仍在运行，提示等待而非静默替换。
        # （socket 接收超时兜底下旧线程最多 10 秒内会结束；直接替换会导致
        #   新线程在 DataFeed 锁上排队，表现为再次超时）
        if self._thread is not None:
            if self._thread.isRunning():
                self._fetching = False
                self.fetch_btn.setEnabled(True)
                self.fetch_btn.setText("🔍 抓取数据并提取 DNA 特征")
                QMessageBox.warning(
                    self, "提示",
                    "上一次抓取仍在进行中（可能是行情服务器响应慢或网络不畅），\n"
                    "请稍候片刻，等待上一次请求结束后再重试。")
                return
            try:
                self._thread.done.disconnect()
            except TypeError:
                pass  # 没有连接时 ignore

        self._fetching = True  # 设置标志
        try:
            if self._feed is None:
                self._feed = DataFeed(db=self.db)
            market = infer_market(code)
            self.fetch_btn.setEnabled(False)
            self.fetch_btn.setText("正在抓取…")
            self.save_btn.setEnabled(False)

            # 计算需要拉取的K线数量：默认 300 根（含 114 天黄线预热 + 区间长度）。
            # 若所选结束日期较早（历史波段），按距今自然日动态扩展拉取量，
            # 否则拉取范围覆盖不到所选区间，会得到"0个交易日"。
            end_dt = datetime.datetime.strptime(end, "%Y-%m-%d").date()
            days_since = max(0, (datetime.date.today() - end_dt).days)
            count = min(300 + int(days_since * 0.8) + 30, 3000)  # 自然日→交易日粗算+缓冲，上限12年
            self.status_label.setText(
                f"正在拉取 {code} 前复权日K（{count} 根，覆盖至 {end} 之前）并计算特征…")
            self._thread = FetchBarsThread(self._feed, code, market, count)
            self._thread.done.connect(lambda df, err: self._on_fetched(df, err, code, start, end))
            self._thread.start()
        except Exception as e:  # noqa: BLE001
            # 启动失败必须重置状态，否则按钮永久禁用、后续点击无反应
            self._fetching = False
            self.fetch_btn.setEnabled(True)
            self.fetch_btn.setText("🔍 抓取数据并提取 DNA 特征")
            QMessageBox.critical(self, "抓取启动失败", str(e))
            return

        # 30秒超时保护：防止线程永久阻塞（如网络故障）
        QTimer.singleShot(30000, lambda: self._on_fetch_timeout(code))

    def _on_fetched(self, df, err, code, start, end):
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("🔍 抓取数据并提取 DNA 特征")
        self._fetching = False  # 重置标志

        if err or df is None or df.empty:
            QMessageBox.critical(self, "抓取失败", f"获取 {code} 日K失败：{err or '无数据'}")
            return
        try:
            seg = df[(df["date"] >= start) & (df["date"] <= end)]
            if len(seg) < 5:
                if len(seg) == 0 and not df.empty:
                    info = (f"所选区间（{start} ~ {end}）内没有交易日。\n\n"
                            f"本次K线覆盖范围：{df['date'].min()} ~ {df['date'].max()}\n\n"
                            "请检查日期是否合理，或所选股票在该区间是否停牌/未上市。")
                else:
                    info = (f"所选区间仅 {len(seg)} 个交易日（前复权口径）。\n"
                            "建议选取 20~30 天的完整建仓波；少于15天特征提取意义有限。")
                QMessageBox.warning(self, "区间过短", info)
                return
            fdf = DNAFeatureExtractor.calculate_indicators(seg)
            self._matrix = DNAFeatureExtractor.extract_matrix(fdf)
            self._meta = {
                "code": code, "name": self.name_edit.text().strip() or code,
                "strategy": self.cat_combo.currentText().strip(),
                "start": start, "end": end, "len_days": len(seg),
                "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            }
            self._draw(self._matrix)
            self.save_btn.setEnabled(True)
            self.status_label.setText(
                f"✅ 成功提取 {len(seg)} 个交易日三维特征（区间 {start} ~ {end}，"
                f"数据源：前复权 pytdx 自算）。确认曲线无误后点击保存。")
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "特征提取失败", str(e))

    def _on_fetch_timeout(self, code):
        """30秒超时保护：如果线程还没完成，视为网络故障，强制重置界面。"""
        if self._thread is not None and self._thread.isRunning():
            try:
                self._thread.done.disconnect()
            except TypeError:
                pass
            # 注意：不要将 self._thread 置 None——线程可能仍在运行
            # （socket 接收超时兜底下 10 秒内会自然结束并释放锁），
            # 置 None 后 QThread 可能被 Python GC 提前销毁导致崩溃。
        self.fetch_btn.setEnabled(True)
        self.fetch_btn.setText("🔍 抓取数据并提取 DNA 特征")
        self._fetching = False
        self.status_label.setText(f"⚠️ 抓取 {code} 超时（30秒），请检查网络后重试。")
        QMessageBox.warning(self, "抓取超时",
                           f"获取 {code} 日K数据超时（30秒），\n"
                           "可能原因：网络延迟或行情服务器响应慢。\n"
                           "请稍后重试，或检查网络连接。")

    def _draw(self, mat):
        self.plot.clear()
        n = len(mat)
        for i, (name, color) in enumerate(CURVE_STYLE):
            pen = pg.mkPen(color, width=1.6)
            if i == 1:
                pen.setStyle(Qt.DashLine)
            self.plot.plot(range(n), mat[:, i], pen=pen, name=name)
        self.plot.setTitle(f"三维特征曲线（{self._meta['name']}，{n} 天）")

    # ------------------------------------------------------------ 保存/删除
    def _save(self):
        if self._matrix is None or self._meta is None:
            return
        name = self._meta["name"]
        try:
            self.tm.save_template(name, self._matrix, self._meta)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(e))
            return
        QMessageBox.information(self, "已入库",
                                f"模具 【{name}】 已保存到黄金模具库！\n"
                                f"{self.tm.npz_path}\n{self.tm.meta_path}")
        self._refresh_template_list()

    def _delete_template(self, name):
        if QMessageBox.question(self, "删除模具",
                                f"确定删除模具 【{name}】？") != QMessageBox.Yes:
            return
        self.tm.delete_template(name)
        self._refresh_template_list()

    def _refresh_template_list(self):
        self.tbl.setRowCount(0)
        for name, meta in self.tm.list_templates():
            row = self.tbl.rowCount()
            self.tbl.insertRow(row)
            self.tbl.setItem(row, 0, QTableWidgetItem(name))
            self.tbl.setItem(row, 1, QTableWidgetItem(meta.get("name", "")))
            self.tbl.setItem(row, 2, QTableWidgetItem(meta.get("code", "")))
            self.tbl.setItem(row, 3, QTableWidgetItem(meta.get("strategy", "")))
            self.tbl.setItem(row, 4, QTableWidgetItem(
                f"{meta.get('start', '')} ~ {meta.get('end', '')}"))
            self.tbl.setItem(row, 5, QTableWidgetItem(str(meta.get("len_days", ""))))
            del_btn = QPushButton("🗑️ 删除")
            del_btn.clicked.connect(lambda _, n=name: self._delete_template(n))
            self.tbl.setCellWidget(row, 6, del_btn)

    def closeEvent(self, event):
        if self._feed is not None:
            try:
                self._feed.disconnect()
            except Exception:  # noqa: BLE001
                pass
        super().closeEvent(event)
