# -*- coding: utf-8 -*-
"""微观特征规则引擎（二层精筛第二环，v0.7.4）。

在 DNA 骨架相似度（DTW 形态分）基础上，对**当前信号日**的微观量价结构做加减分
与买点定级，输出：
- score_modifier: 综合分修正量（clip 到 [-20, +20]）
- buy_level:     买点定级 —— "SB1核心买点" / "B1底仓买点" / ""（无买点）
- signals:       触发的微观信号名列表（用于 UI「微观特征诊断」列与进池原因）
- detail:        各信号是否触发的明细（{信号名: bool}，便于诊断对账）

## 设计语义（对齐模具库「经典B1缩量回踩」的形态哲学）
B1 买点 = 上升趋势中（白线>黄线）的缩量回踩不破支撑；SB1 核心买点 = 在 B1 基础上
同时满足「缩量回踩 + 回踩支撑不破 + 均线多头」三重结构（得分修正 ≥ 10）。
微观规则对"放量启动/突破平台"等进攻性结构加分，对"长上影/放量滞涨/破位/巨量阴线"
等风险结构减分，最终综合分 = clip(DTW骨架分 + score_modifier, 0, 100)。

所有规则均基于 DNAFeatureExtractor.calculate_indicators 产出的指标帧
（含 yl 黄线 / wl 白线 / ma14..ma114），只需最近 ~20 行即可计算。
"""
import numpy as np
import pandas as pd


class MicroRuleEvaluator:
    """微观特征加减分 + 买点定级器。"""

    MIN_MODIFIER = -20.0
    MAX_MODIFIER = 20.0

    # 信号名 → 分值（正=加分结构，负=减分风险）
    RULES = {
        "缩量回踩": 6,
        "回踩支撑不破": 4,
        "均线多头排列": 3,
        "放量启动": 5,
        "突破10日平台": 4,
        "小实体企稳": 2,
        "长上影承压": -4,
        "放量滞涨": -5,
        "跌破黄白线": -6,
        "巨量阴线": -5,
        "短期偏离过大": -3,
    }

    def __init__(self):
        pass

    # ------------------------------------------------------------ 主入口
    def evaluate(self, bars, fdf, best=None):
        """计算微观加减分与买点定级。

        bars: 原始日K DataFrame（date/open/high/low/close/vol/amount，时间正序）
        fdf:  DNAFeatureExtractor.calculate_indicators(bars) 产出的指标帧
              （含 yl/wl/ma14..ma114）
        best: 可选 dict，DNA 匹配结果 {template,best_len,best_start,best_end}
        返回 {"score_modifier": float, "buy_level": str,
              "signals": [str,...], "detail": {str: bool}}
        """
        if fdf is None or len(fdf) < 8:
            return {"score_modifier": 0.0, "buy_level": "",
                    "signals": [], "detail": {}}

        c = fdf["close"].values.astype(float)
        o = fdf["open"].values.astype(float)
        h = fdf["high"].values.astype(float)
        l = fdf["low"].values.astype(float)
        v = fdf["vol"].values.astype(float)
        yl = fdf["yl"].values.astype(float)
        wl = fdf["wl"].values.astype(float)
        n = len(fdf)
        last = n - 1

        v5 = pd.Series(v).rolling(5, min_periods=1).mean().values
        v20 = pd.Series(v).rolling(20, min_periods=1).mean().values
        vol_r = v[last] / (v5[last] + 1e-8)

        # 动态支撑线（与特征维度3口径一致：白线优先，破位切黄线）
        ref = wl[last] if c[last] >= wl[last] else yl[last]
        bias = (c[last] - ref) / (ref + 1e-8)

        detail = {}

        def _set(name, cond):
            detail[name] = bool(cond)
            return bool(cond)

        modifier = 0.0
        signals = []

        # 1) 缩量回踩：量能萎缩（5日均量 < 20日均量×0.85）且价格回踩到黄线附近
        if _set("缩量回踩",
                n >= 8 and v5[last] < v20[last] * 0.85
                and 0.99 <= c[last] / (yl[last] + 1e-8) <= 1.06):
            modifier += self.RULES["缩量回踩"]
            signals.append("缩量回踩")

        # 2) 回踩支撑不破：近3日最低价不破动态支撑线
        low3 = float(np.min(l[max(0, last - 2):last + 1]))
        if _set("回踩支撑不破", n >= 4 and low3 >= ref * 0.99 and c[last] >= ref):
            modifier += self.RULES["回踩支撑不破"]
            signals.append("回踩支撑不破")

        # 3) 均线多头排列：白线>黄线 且收盘站上白线
        if _set("均线多头排列", n >= 3 and wl[last] > yl[last] and c[last] > wl[last]):
            modifier += self.RULES["均线多头排列"]
            signals.append("均线多头排列")

        # 4) 放量启动：今日量 > 5日均量×1.8 且收阳
        if _set("放量启动", n >= 6 and vol_r > 1.8 and c[last] > o[last]):
            modifier += self.RULES["放量启动"]
            signals.append("放量启动")

        # 5) 突破10日平台：收盘突破此前10日最高价
        if n >= 11:
            hi10 = float(np.max(h[last - 10:last]))
        else:
            hi10 = 0.0
        if _set("突破10日平台", n >= 11 and c[last] > hi10 and c[last] > o[last]):
            modifier += self.RULES["突破10日平台"]
            signals.append("突破10日平台")

        # 6) 小实体企稳：小实体阳线（实体 ≤ 振幅×0.3）
        body = abs(c[last] - o[last])
        rng = h[last] - l[last]
        if _set("小实体企稳",
                n >= 2 and c[last] >= o[last] and rng > 0
                and body <= 0.3 * rng):
            modifier += self.RULES["小实体企稳"]
            signals.append("小实体企稳")

        # 7) 长上影承压（减分）：上影 > 2×实体
        up_shadow = h[last] - max(c[last], o[last])
        if _set("长上影承压", n >= 2 and up_shadow > 2 * (body + 1e-8)):
            modifier += self.RULES["长上影承压"]
            signals.append("长上影承压")

        # 8) 放量滞涨（减分）：放巨量但涨幅 ≤ ±1.5%
        pct = (c[last] / c[last - 1] - 1) * 100 if (n >= 2 and c[last - 1]) else 0.0
        if _set("放量滞涨", n >= 6 and vol_r > 2.0 and abs(pct) <= 1.5):
            modifier += self.RULES["放量滞涨"]
            signals.append("放量滞涨")

        # 9) 跌破黄白线（减分）：收盘同时跌破黄线白线
        if _set("跌破黄白线", n >= 3 and c[last] < yl[last] and c[last] < wl[last]):
            modifier += self.RULES["跌破黄白线"]
            signals.append("跌破黄白线")

        # 10) 巨量阴线（减分）：放巨量且收阴
        if _set("巨量阴线", n >= 6 and vol_r > 2.0 and c[last] < o[last]):
            modifier += self.RULES["巨量阴线"]
            signals.append("巨量阴线")

        # 11) 短期偏离过大（减分）：距动态支撑线 > +15%
        if _set("短期偏离过大", n >= 3 and bias > 0.15):
            modifier += self.RULES["短期偏离过大"]
            signals.append("短期偏离过大")

        modifier = float(np.clip(round(modifier, 1), self.MIN_MODIFIER, self.MAX_MODIFIER))

        # ---- 买点定级 ----
        has_pullback = ("缩量回踩" in signals) or ("回踩支撑不破" in signals)
        if has_pullback and "均线多头排列" in signals and modifier >= 10:
            buy_level = "SB1核心买点"
        elif has_pullback or modifier >= 6:
            buy_level = "B1底仓买点"
        else:
            buy_level = ""

        return {"score_modifier": modifier, "buy_level": buy_level,
                "signals": signals, "detail": detail}
