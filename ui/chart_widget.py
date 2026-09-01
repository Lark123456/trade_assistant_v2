# -*- coding: utf-8 -*-
"""K线图组件（pyqtgraph，v0.7.3 升级）：
- 周期切换：日K（前复权）/ 周K / 月K / 5分 / 15分 / 30分 / 60分钟
- 主图指标：MA(5/10/20/60)、BOLL、黄白线（YL/WL，仅日K）
- 副图切换：MACD / KDJ / RSI / 无（成交量固定）
- 周/月K由前复权日K重采样（复权口径一致）；分钟线为原始数据（UI 标注未复权）
- 数据异步拉取（QThread），不阻塞界面
"""
import logging

import numpy as np
import pandas as pd
import pyqtgraph as pg
from PyQt5.QtCore import Qt, QPointF, QRectF, QThread, pyqtSignal
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel,
                             QPushButton, QStackedWidget, QToolTip, QVBoxLayout)

from config import COLOR_DOWN, COLOR_UP

log = logging.getLogger("chart")

pg.setConfigOptions(antialias=True)
UP = QColor(COLOR_UP)
DOWN = QColor(COLOR_DOWN)

# K 线实体半宽上限（像素）：K线间距充足时实体总宽不超过 2*BODY_PX
BODY_PX = 6
# 实体总宽 / K线间距 的最大比例（防止K线密集时实体互相重叠）
BODY_SPACING_RATIO = 0.7
# 实体最小可见像素高度（防止十字星被压成不可见）
BODY_MIN_PX = 1.0

PERIODS = [
    ("日K", "day"), ("周K", "week"), ("月K", "month"),
    ("5分钟", "m5"), ("15分钟", "m15"), ("30分钟", "m30"), ("60分钟", "m60"),
]
MA_COLORS = [("MA5", "#f6b73c"), ("MA10", "#3c78f6"),
             ("MA20", "#a03cf6"), ("MA60", "#2f9e44")]
BOLL_COLORS = [("BOLL_MID", "#999999"), ("BOLL_UPPER", "#d4791e"),
               ("BOLL_LOWER", "#d4791e")]
MAIN_INDICATORS = [
    ("均线MA", "ma", "MA5/10/20/60"),
    ("布林BOLL", "boll", "BOLL中/上/下轨"),
    ("黄白线", "ylwl", "黄线YL(多空均线)与白线WL(双平滑)，仅日K"),
]


class CandlestickItem(pg.GraphicsObject):
    """蜡烛图：data 为 Nx5 数组 [index, open, high, low, close]。

    - 实体宽度=屏幕像素（自动缩放下恒定可见）
    - 十字星实体高度最小 1px（防止压扁）
    - 支持 hover：在鼠标位置显示当根 K 线 OHLC
    - 支持外部框选 ROI 联动（提供 `indexAt(pos)` 给 ChartDialog 用）
    """

    def __init__(self, data, date_labels=None, name="", code=""):
        super().__init__()
        self.data = np.asarray(data, dtype=float)
        self.date_labels = list(date_labels or [])
        self.name = name
        self.code = code
        # 缓存：x 索引 -> 数据，便于 hover 时查 OHLC
        self._xs = self.data[:, 0]
        self._opens = self.data[:, 1]
        self._highs = self.data[:, 2]
        self._lows = self.data[:, 3]
        self._closes = self.data[:, 4]
        self.setAcceptHoverEvents(True)
        self._last_hover_idx = -1
        self.generatePicture()

    def generatePicture(self):
        """实体宽度在 paint 时按当前 viewPixelSize 实时算（X 缩放下恒定像素宽）。"""
        # 不再缓存 QPicture（缓存后无法跟 X 轴缩放同步实体宽度）
        pass

    def _body_width_data(self, painter):
        """把实体宽度（像素）换算成数据坐标宽度。

        QPainter.combinedTransform() = world * device = 数据 → 屏幕像素。
        1 数据单位 = combinedTransform.map((1,0)).x() - .map((0,0)).x() 像素。

        实体总宽 = clamp(间距×70%, 下限2px, 上限12px)：
        - K线间距充足（默认视图~180根）时实体至少 2px 宽，保证实体形态清晰可辨
        - K线极密（缩放看全部800根，间距<3px）时退回 间距×70%，优先不重叠
        - 间距再大时不超过 12px 上限，保持清爽
        """
        try:
            if painter is None:
                return 0.4
            ct = painter.combinedTransform()
            px_per_unit_x = ct.map(QPointF(1, 0)).x() - ct.map(QPointF(0, 0)).x()
            if px_per_unit_x <= 0:
                return 0.4
            # K线间距（像素）= 1 数据单位的像素宽（x = arange(n)）
            spacing_px = px_per_unit_x
            # 实体总宽 = 间距×70%，上限 2*BODY_PX
            total_w_px = min(spacing_px * BODY_SPACING_RATIO, BODY_PX * 2)
            # 间距足够时才保证最小 2px 可见宽度；间距不足时优先不重叠
            if spacing_px >= 3.0:
                total_w_px = max(total_w_px, 2.0)
            half_w_px = total_w_px / 2
            return half_w_px / px_per_unit_x
        except Exception:
            return 0.4

    def _min_body_height_data(self, painter):
        """十字星实体最小 1px 对应的数据坐标高度。"""
        try:
            if painter is None:
                return 0.01
            ct = painter.combinedTransform()
            px_per_unit_y = ct.map(QPointF(0, 1)).y() - ct.map(QPointF(0, 0)).y()
            if abs(px_per_unit_y) <= 0:
                return 0.01
            return max(BODY_MIN_PX / abs(px_per_unit_y), 1e-4)
        except Exception:
            return 0.01

    def paint(self, painter, *args):
        painter.setRenderHint(QPainter.Antialiasing, False)
        w = self._body_width_data(painter)
        min_h = self._min_body_height_data(painter)
        for i in range(len(self._xs)):
            t = self._xs[i]
            o, h, l, c = self._opens[i], self._highs[i], self._lows[i], self._closes[i]
            color = UP if c >= o else DOWN
            # 影线：恒定 1px 细线（cosmetic 笔，像素宽不随缩放变化）。
            # 注意：不能使用非 cosmetic 笔——其线宽=1个数据单位（=1根K线间距），
            # K线密集时影线会占满间距导致整片粘连。
            wick_pen = pg.mkPen(color, width=1)
            wick_pen.setCosmetic(True)
            painter.setPen(wick_pen)
            painter.drawLine(QPointF(t, l), QPointF(t, h))
            # 实体：动态宽度（随缩放与K线密度自适应），细边框 + 填充
            body_pen = pg.mkPen(color, width=1)
            body_pen.setCosmetic(True)
            painter.setPen(body_pen)
            painter.setBrush(pg.mkBrush(color))
            top, bot = max(o, c), min(o, c)
            # 实体高度 = top - bot（正值），矩形起点用低价 bot：
            # QRectF(y=bot, h=top-bot) 覆盖数据区间 [bot, top]，在 y 轴
            # 翻转的坐标系下正确落在高低价之间。
            # 原代码用 QRectF(y=top, h=bot-top) —— h 为负时 Qt 虽会归一化，
            # 但 body_h = max(bot-top, min_h) 把负数换成 min_h 后不再归一化，
            # 矩形画到高价上方，且实体只剩 min_h 高（1px 十字星）。
            body_h = max(top - bot, min_h)
            painter.drawRect(QRectF(t - w, bot, w * 2, body_h))

    def boundingRect(self):
        if len(self._xs) == 0:
            return QRectF()
        return QRectF(float(self._xs.min()) - 1, float(self._lows.min()) - 1,
                      float(self._xs.max() - self._xs.min()) + 2,
                      float(self._highs.max() - self._lows.min()) + 2)

    # ---------------------- hover ----------------------
    def indexAt(self, pos):
        """返回鼠标位置最近的 K 线索引。"""
        if len(self._xs) == 0:
            return -1
        idx = int(np.argmin(np.abs(self._xs - pos.x())))
        return idx

    def hoverEvent(self, ev):
        if ev.isExit():
            QToolTip.hideText()
            self._last_hover_idx = -1
            return
        idx = self.indexAt(ev.pos())
        if idx < 0 or idx == self._last_hover_idx:
            return
        self._last_hover_idx = idx
        t = self._xs[idx]
        o, h, l, c = self._opens[idx], self._highs[idx], self._lows[idx], self._closes[idx]
        date = self.date_labels[idx] if idx < len(self.date_labels) else f"#{int(t)}"
        pct = (c / o - 1) * 100 if o else 0.0
        amp = (h / l - 1) * 100 if l else 0.0
        sign = "+" if pct >= 0 else ""
        lines = [
            f"{self.name} {self.code}",
            f"日期: {date}",
            f"开: {o:.2f}  收: {c:.2f}",
            f"高: {h:.2f}  低: {l:.2f}",
            f"涨跌: {sign}{pct:.2f}%   振幅: {amp:.2f}%",
        ]
        QToolTip.showText(ev.screenPos().toPoint(), "\n".join(lines))


class FetchBarsThread(QThread):
    """后台拉取K线（日K前复权 / 分钟线），避免阻塞界面。"""
    done = pyqtSignal(object, str)   # (df, error)

    def __init__(self, feed, code, market, period, count=800, parent=None):
        super().__init__(parent)
        self.feed = feed
        self.code = code
        self.market = market
        self.period = period
        self.count = count

    def run(self):
        try:
            if self.period == "day":
                # CachedFeed 内部即前复权口径（v0.7.0 起），无需（也不接受）adjust 参数
                df = self.feed.get_daily_bars(self.code, market=self.market,
                                              count=self.count)
            elif self.period in ("week", "month"):
                df = self.feed.get_daily_bars(self.code, market=self.market,
                                              count=self.count)
                df = resample_daily_bars(df, "week" if self.period == "week" else "month")
            else:
                cat = {"m5": 0, "m15": 1, "m30": 2, "m60": 3}[self.period]
                df = self.feed.get_kline_bars(self.code, market=self.market,
                                              category=cat, count=self.count)
            self.done.emit(df, "")
        except Exception as e:  # noqa: BLE001
            self.done.emit(None, str(e))


def resample_daily_bars(df: pd.DataFrame, period: str) -> pd.DataFrame:
    """日K（前复权）重采样为周K/月K：open=首开 high=max low=min close=末收 vol/amount=sum。

    周期标签：周K=周五（自然周），月K=月末交易日。列结构与日K一致（date,open,high,low,
    close,vol,amount），指标层可直接复用 compute_indicators。
    """
    if df is None or df.empty:
        return df
    d = df.copy()
    d["_dt"] = pd.to_datetime(d["date"])
    d = d.set_index("_dt")
    if period == "week":
        g = d.resample("W-FRI")
    else:
        try:  # pandas 2.2+ 用 ME，旧版用 M
            g = d.resample("ME")
        except ValueError:
            g = d.resample("M")
    g = g.agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "vol": "sum", "amount": "sum",
    }).dropna(subset=["close"])
    g = g.reset_index()
    g["date"] = g["_dt"].dt.strftime("%Y-%m-%d")
    return g[["date", "open", "high", "low", "close", "vol", "amount"]]


class ChartDialog(QDialog):
    """股票K线图对话框：周期/主图指标/副图自由切换，数据异步拉取。"""

    def __init__(self, code, name, market=None, db=None, parent=None):
        super().__init__(parent)
        self.code = str(code).zfill(6)
        self.name = name or ""
        self.market = int(market if market is not None else 0)
        self.db = db
        self._feed = None
        self._thread = None
        self._df = None
        self.setWindowTitle(f"K线图：{self.name}（{self.code}）")
        self.resize(1100, 720)
        self._build_ui()
        self._load()

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        root = QVBoxLayout(self)

        # 工具栏
        bar = QHBoxLayout()
        bar.addWidget(QLabel("周期:"))
        self.period_combo = QComboBox()
        for label, key in PERIODS:
            self.period_combo.addItem(label, key)
        self.period_combo.currentIndexChanged.connect(self._load)
        bar.addWidget(self.period_combo)
        bar.addSpacing(12)
        bar.addWidget(QLabel("主图:"))
        self.ma_check = QCheckBox("均线MA")
        self.ma_check.setChecked(True)
        self.boll_check = QCheckBox("布林BOLL")
        self.ylwl_check = QCheckBox("黄白线")
        self.ylwl_check.setToolTip("黄线YL/白线WL（仅日K周期显示）")
        for cb in (self.ma_check, self.boll_check, self.ylwl_check):
            cb.toggled.connect(self._redraw)
            bar.addWidget(cb)
        bar.addSpacing(12)
        bar.addWidget(QLabel("副图:"))
        self.sub_combo = QComboBox()
        for label, key in (("MACD", "macd"), ("KDJ", "kdj"), ("RSI", "rsi"),
                           ("单针下30", "needle"), ("砖型图", "brick"), ("无", "none")):
            self.sub_combo.addItem(label, key)
        self.sub_combo.currentIndexChanged.connect(self._redraw)
        bar.addWidget(self.sub_combo)
        bar.addStretch(1)
        self.src_label = QLabel("")
        self.src_label.setStyleSheet("color:#666;")
        bar.addWidget(self.src_label)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._load)
        bar.addWidget(refresh_btn)
        root.addLayout(bar)

        # 主图
        self.main_plot = pg.PlotWidget()
        self.main_plot.showGrid(x=True, y=True, alpha=0.3)
        self.main_plot.setLabel("left", "价格")
        root.addWidget(self.main_plot, stretch=5)

        # 成交量
        self.vol_plot = pg.PlotWidget()
        self.vol_plot.setLabel("left", "成交量(手)")
        self.vol_plot.setXLink(self.main_plot)
        self.vol_plot.hideAxis("bottom")
        root.addWidget(self.vol_plot, stretch=2)

        # 副图（MACD/KDJ/RSI/单针下30/砖型图/无）
        self.sub_stack = QStackedWidget()
        self.sub_macd = pg.PlotWidget()
        self.sub_macd.setLabel("left", "MACD")
        self.sub_kdj = pg.PlotWidget()
        self.sub_kdj.setLabel("left", "KDJ")
        self.sub_rsi = pg.PlotWidget()
        self.sub_rsi.setLabel("left", "RSI")
        self.sub_needle = pg.PlotWidget()   # 单针下30（内置副图指标）
        self.sub_needle.setLabel("left", "单针下30")
        self.sub_brick = pg.PlotWidget()    # 砖型图（内置副图指标）
        self.sub_brick.setLabel("left", "砖型图")
        self.sub_none = pg.PlotWidget()
        self.sub_none.hideAxis("left")
        for w in (self.sub_macd, self.sub_kdj, self.sub_rsi,
                  self.sub_needle, self.sub_brick, self.sub_none):
            w.setXLink(self.main_plot)
            w.hideAxis("bottom")
            self.sub_stack.addWidget(w)
        root.addWidget(self.sub_stack, stretch=2)

    # ------------------------------------------------------------ 数据加载
    def _load(self):
        if self._thread is not None and self._thread.isRunning():
            return
        if self._feed is None:
            from data_feed import DataFeed
            from scanner import CachedFeed
            self._feed = CachedFeed(DataFeed(db=self.db), self.db)
        period = self.period_combo.currentData()
        self.src_label.setText("加载中…")
        self._thread = FetchBarsThread(self._feed, self.code, self.market, period)
        self._thread.done.connect(self._on_loaded)
        self._thread.start()

    def _on_loaded(self, df, err):
        if err or df is None or len(df) < 3:
            self.src_label.setText(f"获取失败：{err or '数据不足'}")
            self._df = None
            return
        self._df = df
        period = self.period_combo.currentData()
        # 数据源标注
        if period == "day":
            adj = getattr(self._feed, "last_adjust", "qfq")
            self.src_label.setText(f"日K·前复权" if adj == "qfq" else "日K·未复权(降级)")
        elif period in ("week", "month"):
            self.src_label.setText("周K/月K·前复权重采样")
        else:
            self.src_label.setText("分钟线·原始未复权")
        self._redraw()

    # ------------------------------------------------------------ 绘制
    def _redraw(self):
        if self._df is None or self._df.empty:
            return
        df = self._df
        n = len(df)
        x = np.arange(n)
        period = self.period_combo.currentData()

        # 指标计算（日/周/月/分钟通用；黄白线仅日K）
        from indicators import compute_indicators
        dfi = compute_indicators(df)
        if period == "day":
            try:
                from core.dna_engine import DNAFeatureExtractor
                dfi = DNAFeatureExtractor.calculate_indicators(df)
            except Exception as e:  # noqa: BLE001
                log.debug("黄白线计算失败: %s", e)

        # ---- 主图 ----
        self.main_plot.clear()
        data = np.column_stack([
            x, df["open"].to_numpy(), df["high"].to_numpy(),
            df["low"].to_numpy(), df["close"].to_numpy()])
        # 日期列：日/周/月用 date；分钟用 datetime
        if "date" in df.columns:
            date_labels = df["date"].astype(str).tolist()
        elif "datetime" in df.columns:
            date_labels = df["datetime"].astype(str).tolist()
        else:
            date_labels = [str(i) for i in x]
        self._candles = CandlestickItem(data, date_labels=date_labels,
                                         name=self.name, code=self.code)
        self.main_plot.addItem(self._candles)
        self._attach_box_select()
        if self.ma_check.isChecked():
            for ma, color in MA_COLORS:
                if ma in dfi.columns:
                    self.main_plot.plot(x, dfi[ma].to_numpy(),
                                        pen=pg.mkPen(color, width=1.2), name=ma)
        if self.boll_check.isChecked():
            for name, color in BOLL_COLORS:
                if name in dfi.columns:
                    pen = pg.mkPen(color, width=1.0)
                    pen.setStyle(Qt.DashLine)
                    self.main_plot.plot(x, dfi[name].to_numpy(), pen=pen, name=name)
        if period == "day" and self.ylwl_check.isChecked():
            for name, color, width in (("yl", "#f2b01e", 1.6), ("wl", "#e6e6e6", 1.2)):
                if name in dfi.columns:
                    self.main_plot.plot(x, dfi[name].to_numpy(),
                                        pen=pg.mkPen(color, width=width), name=name)
        self.main_plot.setTitle(
            f"{df['date'].iloc[-1] if 'date' in df.columns else df['datetime'].iloc[-1]}  "
            f"收 {df['close'].iloc[-1]:.2f}  涨跌 "
            f"{(df['close'].iloc[-1] / df['close'].iloc[-2] - 1) * 100:+.2f}%")

        # ---- 成交量 ----
        self.vol_plot.clear()
        vol_colors = [UP if c >= o else DOWN
                      for o, c in zip(df["open"], df["close"])]
        self.vol_plot.addItem(pg.BarGraphItem(x=x, height=df["vol"].to_numpy(),
                                              width=0.6, brushes=vol_colors))

        # ---- 副图 ----
        sub = self.sub_combo.currentData()
        idx_map = {"macd": 0, "kdj": 1, "rsi": 2, "needle": 3, "brick": 4, "none": 5}
        self.sub_stack.setCurrentIndex(idx_map.get(sub, 5))
        for plot in (self.sub_macd, self.sub_kdj, self.sub_rsi,
                     self.sub_needle, self.sub_brick):
            plot.clear()
        if sub == "macd" and {"MACD_DIF", "MACD_DEA"}.issubset(dfi.columns):
            self.sub_macd.plot(x, dfi["MACD_DIF"].to_numpy(), pen=pg.mkPen("#3c78f6"))
            self.sub_macd.plot(x, dfi["MACD_DEA"].to_numpy(), pen=pg.mkPen("#f6b73c"))
            if "MACD" in dfi.columns:
                colors = [UP if v >= 0 else DOWN for v in dfi["MACD"]]
                self.sub_macd.addItem(pg.BarGraphItem(x=x, height=dfi["MACD"].to_numpy(),
                                                      width=0.6, brushes=colors))
        elif sub == "kdj" and {"KDJ_K", "KDJ_D", "KDJ_J"}.issubset(dfi.columns):
            for name, color in (("KDJ_K", "#3c78f6"), ("KDJ_D", "#f6b73c"),
                                ("KDJ_J", "#a03cf6")):
                self.sub_kdj.plot(x, dfi[name].to_numpy(), pen=pg.mkPen(color, width=1.1))
        elif sub == "rsi" and {"RSI6", "RSI12", "RSI24"}.issubset(dfi.columns):
            for name, color in (("RSI6", "#e64340"), ("RSI12", "#3c78f6"),
                                ("RSI24", "#999999")):
                self.sub_rsi.plot(x, dfi[name].to_numpy(), pen=pg.mkPen(color, width=1.1))
        elif sub == "needle":
            self._draw_needle(x, df)
        elif sub == "brick":
            self._draw_brick(x, df)

        # 默认视图只显示最近 N 根K线：全部800根挤进一屏时每根仅~1px，
        # 实体被压成细线（只剩影线十字星）。只显示尾部窗口后实体宽度充足、
        # 形态清晰（鼠标滚轮/框选可查看更早的K线）。
        DEFAULT_VISIBLE = 180
        if n > DEFAULT_VISIBLE:
            self.main_plot.setXRange(n - DEFAULT_VISIBLE, n)
        else:
            self.main_plot.setXRange(0, n)

    # ------------------------------------------------------------ 右键框选分析
    def _attach_box_select(self):
        """绑定右键按住拖动 → 框选 → 弹汇总。

        修复 v0.7.7：用 ViewBox.mouseDragEvent 钩子（pyqtgraph 官方扩展点），
        替换之前的包装 mousePress/Move/Release ——后者会被 pyqtgraph 的事件链吞掉。
        """
        if getattr(self, "_box_wired", False):
            return
        self._box_wired = True
        self._box_origin = None
        self._box_roi = None
        # 拦截 ViewBox 的拖拽事件。pyqtgraph 在 mouseDragEvent 里分发
        # 左键(pan)、中键(zoom)、右键(自定义)；我们劫持右键分支。
        vb = self.main_plot.getViewBox()
        original_drag = vb.mouseDragEvent
        vb.mouseDragEvent = self._wrap_drag(original_drag)
        self._original_drag = original_drag

    def _wrap_drag(self, original):
        def _h(ev):
            # ev.button() == Qt.RightButton → 右键拖拽
            try:
                btn = ev.button()
            except Exception:
                btn = None
            if btn == Qt.RightButton:
                pos = ev.pos()
                if ev.isStart():
                    if self._df is not None and not self._df.empty:
                        self._box_origin = QPointF(pos.x(), pos.y())
                        if self._box_roi is None:
                            self._box_roi = pg.LinearRegionItem(
                                values=(pos.x(), pos.x()),
                                orientation="vertical", movable=False,
                                brush=pg.mkBrush(255, 215, 0, 35),
                                pen=pg.mkPen("#f6b73c", width=1,
                                             style=Qt.DashLine))
                            self.main_plot.addItem(self._box_roi)
                        ev.accept()
                        return
                elif ev.isFinish() and self._box_origin is not None:
                    # 拖拽结束：弹汇总
                    if self._box_roi is not None:
                        x1, x2 = self._box_roi.getRegion()
                        self.main_plot.removeItem(self._box_roi)
                        self._box_roi = None
                        self._box_origin = None
                        if abs(x2 - x1) >= 2:
                            self._show_box_summary(min(x1, x2), max(x1, x2))
                    else:
                        self._box_origin = None
                    ev.accept()
                    return
                elif self._box_origin is not None:
                    # 拖拽中：更新 ROI
                    if self._box_roi is not None:
                        self._box_roi.setRegion(
                            (self._box_origin.x(), pos.x()))
                    ev.accept()
                    return
            return original(ev)
        return _h

    def mouseReleaseEvent(self, ev):
        """兜底：万一 mouseDragEvent.isFinish 没收到，正常 PyQt 释放也能弹汇总。"""
        if ev.button() == Qt.RightButton and self._box_origin is not None:
            if self._box_roi is not None:
                x1, x2 = self._box_roi.getRegion()
                self.main_plot.removeItem(self._box_roi)
                self._box_roi = None
                self._box_origin = None
                if abs(x2 - x1) >= 2:
                    self._show_box_summary(min(x1, x2), max(x1, x2))
            else:
                self._box_origin = None
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def _show_box_summary(self, x_lo, x_hi):
        """框选 [x_lo, x_hi] 区间（数据坐标），弹汇总信息。"""
        if self._df is None or self._df.empty:
            return
        # 索引 = int(round(x))，因为 x = np.arange(n)
        n = len(self._df)
        i_lo = max(0, int(round(x_lo)))
        i_hi = min(n - 1, int(round(x_hi)))
        if i_lo >= i_hi:
            return
        seg = self._df.iloc[i_lo:i_hi + 1]
        if "date" in seg.columns:
            date_col = "date"
        elif "datetime" in seg.columns:
            date_col = "datetime"
        else:
            date_col = None
        d_start = str(seg[date_col].iloc[0]) if date_col else f"#{i_lo}"
        d_end = str(seg[date_col].iloc[-1]) if date_col else f"#{i_hi}"

        o_first = float(seg["open"].iloc[0])
        c_last = float(seg["close"].iloc[-1])
        h_max = float(seg["high"].max())
        l_min = float(seg["low"].min())
        pct = (c_last / o_first - 1) * 100 if o_first else 0.0
        amp = (h_max / l_min - 1) * 100 if l_min else 0.0
        n_up = int((seg["close"] >= seg["open"]).sum())
        n_dn = int((seg["close"] < seg["open"]).sum())
        n_total = len(seg)
        avg_amp = float(((seg["high"] / seg["low"] - 1) * 100).mean())
        # 单日最大涨/跌幅
        prev_close = seg["close"].shift(1)
        daily_pct = (seg["close"] / prev_close - 1) * 100
        daily_pct = daily_pct.iloc[1:]  # 去掉首行 NaN
        if len(daily_pct):
            max_up = float(daily_pct.max())
            max_dn = float(daily_pct.min())
        else:
            max_up = max_dn = 0.0
        total_vol = float(seg["vol"].sum())

        sign = "+" if pct >= 0 else ""
        lines = [
            f"{self.name} {self.code}  区间统计",
            f"区间: {d_start} ~ {d_end}  ({n_total} 根)",
            f"起点开盘: {o_first:.2f}   终点收盘: {c_last:.2f}",
            f"区间最高: {h_max:.2f}   区间最低: {l_min:.2f}",
            f"区间涨跌: {sign}{pct:.2f}%   区间振幅: {amp:.2f}%",
            f"阳/阴: {n_up} / {n_dn}    平均振幅: {avg_amp:.2f}%",
            f"最大单日涨: {max_up:+.2f}%   最大单日跌: {max_dn:+.2f}%",
            f"区间成交量合计: {total_vol:,.0f} 手",
        ]
        QToolTip.showText(self.main_plot.mapToGlobal(QPointF(10, 10).toPoint()),
                          "\n".join(lines))
        # 状态栏同步
        if hasattr(self, "src_label"):
            self.src_label.setText(
                f"框选 {n_total} 根 ({d_start}~{d_end}) "
                f"涨跌{sign}{pct:.2f}% 振幅{amp:.2f}%")

    # ------------------------------------------------------------ 内置副图指标
    @staticmethod
    def _preset_indicator_formula(name):
        from strategy_presets import PRESET_INDICATORS
        for p in PRESET_INDICATORS:
            if p["name"] == name:
                return p["formula"]
        return None

    def _eval_indicator(self, name, df, with_env=False):
        """用公式引擎计算内置副图指标，返回 (命名输出dict, 环境dict)。
        中期/中长期等 `:=` 赋值变量在环境 dict 中（通达信副图也显示这些线）。"""
        from indicators import evaluate_script
        formula = self._preset_indicator_formula(name)
        if not formula:
            return ({}, {}) if with_env else {}
        try:
            outputs, env, _last, _lines = evaluate_script(df, formula)
        except Exception as e:  # noqa: BLE001
            log.debug("副图指标 %s 计算失败: %s", name, e)
            return ({}, {}) if with_env else {}
        outs = outputs or {}
        return (outs, env) if with_env else outs

    def _draw_needle(self, x, df):
        """单针下30：短期/中期/中长期/长期 四线 + 85/30 参考线 + 四买点标记。"""
        outs, env = self._eval_indicator("单针下30(副图)", df, with_env=True)
        if not outs and not env:
            return
        lines = (("短期", "#f2f2f2", 1.0), ("中期", "#f6b73c", 1.0),
                 ("中长期", "#e06ac4", 1.0), ("长期", "#e64340", 1.6))
        for name, color, width in lines:
            arr = outs.get(name) if name in outs else env.get(name)
            if arr is not None:
                self.sub_needle.plot(x, np.asarray(arr, dtype=float),
                                     pen=pg.mkPen(color, width=width))
        # 参考线 85 / 30
        for v in (85, 30):
            pen = pg.mkPen("#999999", width=1.0)
            pen.setStyle(Qt.DashLine)
            self.sub_needle.addItem(pg.InfiniteLine(pos=v, angle=0, pen=pen))
        # 四买点标记（输出值为 -30 的位置）
        marks_x, marks_y = [], []
        for name in ("四线归零买", "白线下20买", "白穿红线买", "白穿黄线买"):
            if name in outs:
                arr = np.asarray(outs[name], dtype=float)
                idx = np.where(arr < -1)[0]
                marks_x.extend(x[idx].tolist())
                marks_y.extend([-30] * len(idx))
        if marks_x:
            self.sub_needle.addItem(pg.ScatterPlotItem(
                marks_x, marks_y, size=9, brush=pg.mkBrush("#e64340"), pen=None))
        self.sub_needle.setYRange(-35, 105)

    def _draw_brick(self, x, df):
        """砖型图：数值 >0 红砖 / =0 绿砖（与通达信副图一致）。"""
        outs = self._eval_indicator("砖型图(副图)", df)
        if not outs or "砖型图" not in outs:
            return
        arr = np.asarray(outs["砖型图"], dtype=float)
        colors = [UP if v > 0 else DOWN for v in arr]
        self.sub_brick.addItem(pg.BarGraphItem(x=x, height=arr, width=0.6,
                                               brushes=colors))
        vmax = max(float(arr.max()), 1.0)
        self.sub_brick.setYRange(-vmax * 0.05, vmax * 1.1)

    def closeEvent(self, event):
        if self._feed is not None:
            try:
                self._feed.feed.disconnect()
            except Exception:  # noqa: BLE001
                pass
        super().closeEvent(event)
