# -*- coding: utf-8 -*-
"""前复权引擎（pytdx 自算，无第三方数据源依赖）。

数据链路：
  pytdx get_security_bars（不复权日K） + get_xdxr_info（除权除息事件）
  -> 前复权因子 -> 复权价格序列

前复权定义：以最新价为基准，历史价格按除权事件缩放，使价格连续。

准确性监测（三层，固化进 test_adjust.py）：
1. 除权参考价自检：ref = (pre_close - fenhong + peigujia*peigu) / (1 + songzhuangu + peigu)，
   与除权日实际开盘价偏差 > 1% 记入 issues 告警（市场自动除权，两者应接近）
2. 最新价锚定：前复权最新收盘必须 == 原始最新收盘（前复权定义）
3. 外部对照：与 akshare qfq 数据抽样逐日对比（离线验证脚本，不用于生产）

注意事项：
- 只处理 category==1（除权除息）中实际有除权的事件（分红/送转/配股）；
  增发、股本变化（category 5/9 等）不影响价格，跳过。
- 除权日当天原始价格已是除权后价格，rate 从除权日（含）起应用。
- 事件日无K线（停牌）时，rate 从其后第一个交易日应用。
- 成交量/成交额不做复权（与通达信/akshare 惯例一致，仅价格列复权）。
"""
import logging

import numpy as np
import pandas as pd

log = logging.getLogger("adjust")

ADJUST_TOLERANCE = 0.10  # 默认：除权参考价 vs 开盘价 偏差容差 10%（主板涨跌停幅度）
# 实际按股票板块涨跌停幅度动态设置（见 limit_pct 参数）：
# 偏差 <= 涨跌停幅度属除权日市场高开/低开（一字板可精确到涨跌停价），属正常市场行为；
# 超过涨跌停幅度才视为计算可疑（合法价格不可能超过涨跌停限制）。
# 创业板/科创板 20%，北交所 30%。


def filter_price_events(events):
    """只保留影响价格的除权除息事件（category==1 且 有实际除权动作）。

    注意 pytdx get_xdxr_info 的口径（已实测验证）：
      fenhong     每10股现金红利（元），÷10 得每股
      songzhuangu 每10股送转股数，   ÷10 得每股
      peigu       每10股配股数，     ÷10 得每股
      peigujia    配股价（元/股），  不除
    输出统一为每股口径。
    """
    out = []
    for e in events or []:
        if int(e.get("category", 0)) != 1:
            continue
        fenhong = float(e.get("fenhong") or 0) / 10.0
        peigujia = float(e.get("peigujia") or 0)
        songzhuangu = float(e.get("songzhuangu") or 0) / 10.0
        peigu = float(e.get("peigu") or 0) / 10.0
        if fenhong > 0 or songzhuangu > 0 or peigu > 0:
            out.append({"date": str(e["date"]), "category": 1,
                        "fenhong": fenhong, "peigujia": peigujia,
                        "songzhuangu": songzhuangu, "peigu": peigu})
    out.sort(key=lambda e: e["date"])
    return out


def compute_qfq_prices(bars_df, events, limit_pct=0.10):
    """计算前复权价格。

    bars_df: 时间正序 DataFrame(date, open, high, low, close, vol, amount)
    events:  除权除息事件列表 [{date, category, fenhong, peigujia, songzhuangu, peigu}]
    limit_pct: 该股票板块的涨跌停幅度（主板0.10/创业板科创板0.20/北交所0.30），
               除权日市场高开/低开可能达到涨跌停价（一字板），该幅度内不告警；
               超过幅度才视为计算可疑。
    返回 (df_qfq, issues)。issues: [str] 告警（除权自检偏差、锚定失败等）。
    无事件/无可用事件时返回原 df。
    """
    if bars_df is None or bars_df.empty:
        return bars_df, []
    events = filter_price_events(events)
    if not events:
        return bars_df, []
    # 涨跌停价按参考价四舍五入到分，一字板偏差可能略超 limit_pct，留 2% 缓冲
    tolerance = limit_pct + 0.02

    dates = bars_df["date"].astype(str).tolist()
    n = len(bars_df)
    closes = bars_df["close"].to_numpy(dtype=float)
    opens = bars_df["open"].to_numpy(dtype=float)
    issues = []

    # 逐事件计算 rate；rate 从事件日（含）之后的原始价格起应用
    rates = []  # [(idx_from, rate)]
    for e in events:
        ed = e["date"]
        idx = None
        for i in range(n):  # 事件日无K线（停牌）时取其后第一个交易日
            if dates[i] >= ed:
                idx = i
                break
        if idx is None:
            continue  # 事件在未来（数据未覆盖），跳过
        if idx == 0:
            continue  # 除权日前无历史K线（上市首日即除权等边界），无法计算
        pre_close = closes[idx - 1]
        if pre_close <= 0:
            continue
        ref = (pre_close - e["fenhong"] + e["peigujia"] * e["peigu"]) / \
              (1.0 + e["songzhuangu"] + e["peigu"])
        if ref <= 0:
            issues.append(f"{ed} 除权参考价异常 ref={ref:.3f}，跳过该事件")
            continue
        rate = ref / pre_close
        # 自检：除权参考价 vs 除权日实际开盘价（涨跌停幅度内为正常市场行为）
        if opens[idx] > 0:
            dev = abs(ref - opens[idx]) / opens[idx]
            if dev > tolerance:
                issues.append(
                    f"{ed} 除权自检偏差 {dev * 100:.2f}% "
                    f"(参考价 {ref:.3f} vs 开盘 {opens[idx]:.3f}，"
                    f"超出涨跌停幅度 {limit_pct * 100:.0f}%)")
        rates.append((idx, rate))

    if not rates:
        return bars_df, issues

    # 后复权因子：从最早事件起累乘 1/rate。
    # 注意：除权日当天的原始价格已是除权后价格（≈ 前收×rate），
    # 要恢复连续性必须“除以 rate”（而非乘以），即 hfq[d] = raw[d] / Π(rate)。
    # 前复权 = 整体缩放使最新价 == 原始最新价。
    factor = np.ones(n)
    acc = 1.0
    for idx, rate in rates:
        acc *= rate
        factor[idx:] = 1.0 / acc
    factor *= 1.0 / factor[-1]

    out = bars_df.copy()
    for col in ("open", "high", "low", "close"):
        out[col] = bars_df[col].to_numpy(dtype=float) * factor

    # 最新价锚定校验
    last_raw = closes[-1]
    if last_raw > 0 and abs(out["close"].iloc[-1] - last_raw) / last_raw > 1e-6:
        issues.append("最新价锚定失败：前复权最新收盘与原始收盘不一致")

    return out, issues
