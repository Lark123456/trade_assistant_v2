# -*- coding: utf-8 -*-
"""通用信号级回测引擎（v0.9.0）

设计目标（信号事件独立回测 / Signal-Level Event Study）：
  - **不模拟资金账户**，无资金上限、无并发持仓限制；
    回测区间内全市场触发的「每一个策略信号」都被视为一次独立的
    交易事件，从而得到最客观的概率学统计（胜率、盈亏比、前瞻收益）。
  - **开仓机制**：信号触发当日为决策日 T，默认以次日开盘价 Open_{t+1}
    买入；若次日一字涨停（low >= open）则剔除该信号（无法买入）。
  - **出场规则（全部由 config 暴露的通用参数控制）**：
      ① 目标止盈 TP%：持仓期最高价 ≥ 买入价 × (1 + TP%)
      ② 硬止损 SL%：  持仓期最低价 ≤ 买入价 × (1 − SL%)
      ③ 最长跟踪 N 天：达到 N 天按第 N 天收盘价平仓
      ④ 移动跟踪止盈（可选）：浮盈达到 trigger% 后，若从持仓期最高
         点回撤 ≥ trailing_pct%，触发止盈出场
  - **KPI 产出**：总信号数、胜率、盈亏比、平均持股周期、
    前瞻收益 T+1/T+3/T+5/T+10/T+20 矩阵。

本模块被 ``ui/backtest_tab.py`` 调用；
策略侧通过 ``core/strategy_adapter.py`` 的 ``REGISTERED_STRATEGIES``
暴露成 ``strategy_func(df) -> Series[bool]``，即可直接对接。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

log = logging.getLogger("signal_backtester")


# --------------------------------------------------------------------------
# 默认回测配置（与 UI 默认值保持一致）
# --------------------------------------------------------------------------
DEFAULT_CONFIG: Dict[str, Any] = {
    "tp_pct": 0.10,            # 目标止盈  +10%
    "sl_pct": 0.04,            # 硬止损    -4%
    "max_days": 10,            # 最长持股天数
    "trailing_pct": 0.03,      # 移动止盈回撤 3%（None=关闭）
    "trailing_trigger": 0.08,  # 移动止盈激活阈值 +8%（None=关闭）
    "forward_periods": [1, 3, 5, 10, 20],   # 前瞻收益周期（交易日）
    "skip_limit_up": True,     # 次日一字涨停剔除
    "limit_up_pct": 0.099,     # 主板一字涨停判定的容差
}


# --------------------------------------------------------------------------
# 信号扫描：在每只票的日K上找到所有信号点（True 的位置）
# --------------------------------------------------------------------------
def find_signals(strategy_func: Callable[[pd.DataFrame], Any],
                 df: pd.DataFrame) -> np.ndarray:
    """在单只票的日K上调用策略函数，返回所有信号触发位置（整数索引数组）。

    strategy_func: 接收 DataFrame（已包含 open/high/low/close/vol/amount），
                   返回长度与 df 相同的 Series[bool] / ndarray[bool] / list[bool]
    返回的整数索引数组表示「决策日 T」的位置（即信号成立的那根 K 线）。
    """
    if df is None or df.empty:
        return np.array([], dtype=int)
    try:
        sig = strategy_func(df)
    except Exception as e:  # noqa: BLE001
        log.debug("策略函数求值失败: %s", e)
        return np.array([], dtype=int)
    arr = np.asarray(sig, dtype=bool)
    if arr.shape[0] != len(df):
        # 兼容只返回最后一根信号的情况
        if arr.ndim == 0:
            arr = np.zeros(len(df), dtype=bool)
            arr[-1] = bool(sig)
        else:
            return np.array([], dtype=int)
    return np.where(arr)[0]


# --------------------------------------------------------------------------
# 单笔交易出场扫描
# --------------------------------------------------------------------------
def _exit_scan(buy_idx: int, buy_price: float, df: pd.DataFrame,
               config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """对单笔交易沿时间轴扫描出场，返回交易记录 dict 或 None（数据不足）。

    df: 时间正序的日K，索引 0..N-1；buy_idx 为买入日（T+1）的索引。
    返回字段：
        buy_idx / sell_idx / buy_price / sell_price
        holding_days / ret_pct / exit_reason
        forward_returns: {period: (pct, win)}
    """
    n = len(df)
    max_days = max(1, int(config.get("max_days", 10)))
    tp_pct = float(config.get("tp_pct", 0.10))
    sl_pct = float(config.get("sl_pct", 0.04))
    trailing_pct = config.get("trailing_pct")
    trailing_trigger = config.get("trailing_trigger")
    fwd_periods = list(config.get("forward_periods", [1, 3, 5, 10, 20]))

    # 持仓区间：[buy_idx, buy_idx + max_days - 1]，最后卖出日 buy_idx+max_days
    sell_window_end = min(buy_idx + max_days, n - 1)
    if sell_window_end <= buy_idx:
        return None  # 数据不足以模拟最大持仓

    high_so_far = -np.inf
    high_idx = buy_idx

    for j in range(buy_idx, sell_window_end + 1):
        row = df.iloc[j]
        h = float(row["high"])
        l = float(row["low"])
        c = float(row["close"])

        # 当日新高
        if h > high_so_far:
            high_so_far = h
            high_idx = j

        # ① 目标止盈（最高价触发）
        tp_price = buy_price * (1.0 + tp_pct)
        if h >= tp_price:
            return _make_trade(
                df, buy_idx, buy_price, j, tp_price,
                exit_reason="止盈",
                high_so_far=high_so_far, high_idx=high_idx,
                fwd_periods=fwd_periods, trailing_pct=trailing_pct,
                trailing_trigger=trailing_trigger,
            )

        # ② 硬止损（最低价触发）
        sl_price = buy_price * (1.0 - sl_pct)
        if l <= sl_price:
            return _make_trade(
                df, buy_idx, buy_price, j, sl_price,
                exit_reason="止损",
                high_so_far=high_so_far, high_idx=high_idx,
                fwd_periods=fwd_periods, trailing_pct=trailing_pct,
                trailing_trigger=trailing_trigger,
            )

        # ③ 移动跟踪止盈（可选；只在收盘时判断）
        if (trailing_pct is not None and trailing_trigger is not None
                and high_so_far >= buy_price * (1.0 + float(trailing_trigger))):
            trailing_exit = high_so_far * (1.0 - float(trailing_pct))
            if c <= trailing_exit:
                return _make_trade(
                    df, buy_idx, buy_price, j, c,
                    exit_reason="移动止盈",
                    high_so_far=high_so_far, high_idx=high_idx,
                    fwd_periods=fwd_periods, trailing_pct=trailing_pct,
                    trailing_trigger=trailing_trigger,
                )

    # ④ 最长跟踪：按 sell_window_end 收盘价平仓
    sell_idx = sell_window_end
    sell_price = float(df.iloc[sell_idx]["close"])
    return _make_trade(
        df, buy_idx, buy_price, sell_idx, sell_price,
        exit_reason="到期平仓",
        high_so_far=high_so_far, high_idx=high_idx,
        fwd_periods=fwd_periods, trailing_pct=trailing_pct,
        trailing_trigger=trailing_trigger,
    )


def _make_trade(df, buy_idx, buy_price, sell_idx, sell_price, exit_reason,
                high_so_far, high_idx, fwd_periods, trailing_pct, trailing_trigger):
    """组装单笔交易记录 dict（含前瞻收益）。"""
    holding_days = max(1, sell_idx - buy_idx + 1)
    ret_pct = (sell_price - buy_price) / buy_price * 100.0 if buy_price else 0.0
    peak_ret = (high_so_far - buy_price) / buy_price * 100.0 if buy_price else 0.0

    # 前瞻收益：按「买入日起算」T+N 日相对买入价的涨跌幅 & 是否盈利
    fwd: Dict[str, Dict[str, float]] = {}
    n = len(df)
    for k in fwd_periods:
        target = buy_idx + int(k)
        if 0 <= target < n:
            close_k = float(df.iloc[target]["close"])
            pct = (close_k - buy_price) / buy_price * 100.0 if buy_price else 0.0
            fwd[str(k)] = {
                "pct": pct,
                "win": 1 if pct > 0 else 0,
                "date": str(df.iloc[target]["date"]) if "date" in df.columns else "",
            }
        else:
            fwd[str(k)] = {"pct": None, "win": None, "date": ""}

    return {
        "buy_idx": int(buy_idx),
        "sell_idx": int(sell_idx),
        "buy_date": str(df.iloc[buy_idx]["date"]) if "date" in df.columns else "",
        "sell_date": str(df.iloc[sell_idx]["date"]) if "date" in df.columns else "",
        "buy_price": float(buy_price),
        "sell_price": float(sell_price),
        "holding_days": int(holding_days),
        "ret_pct": float(ret_pct),
        "peak_ret_pct": float(peak_ret),
        "exit_reason": exit_reason,
        "forward_returns": fwd,
    }


# --------------------------------------------------------------------------
# 主回测入口
# --------------------------------------------------------------------------
def backtest_strategy_signals(strategy_func: Callable[[pd.DataFrame], Any],
                              stock_data_dict: Dict[str, pd.DataFrame],
                              config: Optional[Dict[str, Any]] = None,
                              progress_cb: Optional[Callable[[int, int, str], None]] = None,
                              cancel_cb: Optional[Callable[[], bool]] = None,
                              ) -> Dict[str, Any]:
    """通用信号级回测主入口。

    参数
    ----------
    strategy_func : callable(df) -> array[bool]
        策略函数，接收单只票日K DataFrame，返回信号布尔序列。
        约定：长度必须与 df 行数相同。
    stock_data_dict : {code: DataFrame}
        回测标的字典。每只票的 DataFrame 至少包含列
        ``date, open, high, low, close, vol``，按日期升序排列。
    config : dict
        回测配置（见 DEFAULT_CONFIG）。任何缺失字段用默认值补齐。
    progress_cb : callable(i, total, code_name) -> None, optional
        进度回调（每只票处理一次）。
    cancel_cb : callable() -> bool, optional
        取消标记查询函数；返回 True 时立刻中止回测。

    返回
    -------
    dict 包含：
        - ``trades``: [trade_record, ...] 每笔交易明细
        - ``stats``: 总信号数 / 胜率 / 盈亏比 / 平均收益 / 平均持股周期
        - ``forward_returns``: {period: {pct_avg, win_rate, count}}
        - ``by_stock``: {code: {'signals': int, 'wins': int, 'avg_ret': float, 'win_rate': float}}
        - ``config``: 实际使用的配置（已合并默认值）
        - ``cancelled``: 是否被用户取消
    """
    cfg = dict(DEFAULT_CONFIG)
    if config:
        cfg.update({k: v for k, v in config.items() if v is not None})

    trades: List[Dict[str, Any]] = []
    skipped_limit_up = 0
    total = len(stock_data_dict)
    by_stock: Dict[str, Dict[str, Any]] = {}

    for i, (code, df) in enumerate(stock_data_dict.items()):
        if cancel_cb and cancel_cb():
            log.info("回测被取消（已处理 %d/%d）", i, total)
            return _build_result(trades, by_stock, cfg, cancelled=True)

        name = ""
        if df is not None and not df.empty and "name" in df.columns:
            name = str(df["name"].iloc[0]) if df["name"].iloc[0] else ""
        if progress_cb:
            try:
                progress_cb(i, total, f"{code} {name}".strip())
            except Exception:  # noqa: BLE001
                pass

        if df is None or df.empty or len(df) < 30:
            continue

        # 信号定位
        sig_idx = find_signals(strategy_func, df)
        if sig_idx.size == 0:
            by_stock[code] = {"name": name, "signals": 0, "wins": 0,
                              "avg_ret": 0.0, "win_rate": 0.0}
            continue

        open_arr = df["open"].to_numpy(dtype=float)
        low_arr = df["low"].to_numpy(dtype=float)
        high_arr = df["high"].to_numpy(dtype=float)
        close_arr = df["close"].to_numpy(dtype=float)
        dates = df["date"].astype(str).to_numpy() if "date" in df.columns \
            else np.array([""] * len(df))

        stock_signals = 0
        stock_wins = 0
        stock_ret_sum = 0.0

        # 同票相邻信号至少间隔 max_days，防止重复触发挤在同一段
        last_buy_idx = -10**9
        for t in sig_idx:
            t = int(t)
            buy_idx = t + 1  # 次日开盘买入
            if buy_idx <= last_buy_idx:
                continue
            if buy_idx >= len(df):
                continue

            buy_price = float(open_arr[buy_idx])
            if not np.isfinite(buy_price) or buy_price <= 0:
                continue

            # 次日一字涨停剔除：low >= open 即开盘即涨停、无法买入
            if cfg.get("skip_limit_up", True):
                if low_arr[buy_idx] >= buy_price * (1.0 - 1e-9):
                    skipped_limit_up += 1
                    continue

            trade = _exit_scan(buy_idx, buy_price, df, cfg)
            if trade is None:
                continue

            trade["code"] = str(code)
            trade["name"] = name
            trade["signal_date"] = str(dates[t]) if t < len(dates) else ""
            trade["buy_date"] = str(dates[buy_idx]) if buy_idx < len(dates) else ""
            trades.append(trade)
            last_buy_idx = buy_idx

            stock_signals += 1
            stock_ret_sum += trade["ret_pct"]
            if trade["ret_pct"] > 0:
                stock_wins += 1

        by_stock[code] = {
            "name": name,
            "signals": stock_signals,
            "wins": stock_wins,
            "avg_ret": (stock_ret_sum / stock_signals) if stock_signals else 0.0,
            "win_rate": (stock_wins / stock_signals * 100.0) if stock_signals else 0.0,
        }

    return _build_result(trades, by_stock, cfg, cancelled=False,
                          skipped_limit_up=skipped_limit_up)


def _build_result(trades: List[Dict[str, Any]],
                  by_stock: Dict[str, Dict[str, Any]],
                  cfg: Dict[str, Any],
                  cancelled: bool = False,
                  skipped_limit_up: int = 0) -> Dict[str, Any]:
    """根据交易列表汇总统计指标。"""
    n = len(trades)
    if n == 0:
        return {
            "trades": trades,
            "stats": {
                "total_signals": 0, "wins": 0, "losses": 0, "flat": 0,
                "win_rate": 0.0, "profit_loss_ratio": 0.0,
                "avg_ret": 0.0, "avg_holding_days": 0.0,
                "median_ret": 0.0, "best_ret": 0.0, "worst_ret": 0.0,
                "expectancy": 0.0,
            },
            "forward_returns": {p: {"pct_avg": 0.0, "win_rate": 0.0, "count": 0}
                                for p in cfg.get("forward_periods", [1, 3, 5, 10, 20])},
            "by_stock": by_stock,
            "config": cfg,
            "cancelled": cancelled,
            "skipped_limit_up": skipped_limit_up,
            "exit_reason_dist": {},
        }

    rets = np.array([t["ret_pct"] for t in trades], dtype=float)
    wins = rets > 0
    losses = rets < 0
    flats = rets == 0

    avg_win = float(rets[wins].mean()) if wins.any() else 0.0
    avg_loss = float(rets[losses].mean()) if losses.any() else 0.0
    # 盈亏比 = 平均盈利 / 平均亏损绝对值
    pl_ratio = (avg_win / abs(avg_loss)) if losses.any() else 0.0

    win_rate = wins.sum() / n * 100.0
    avg_ret = float(rets.mean())
    median_ret = float(np.median(rets))
    avg_holding = float(np.mean([t["holding_days"] for t in trades]))

    # 期望 = 胜率 × 平均盈利 − 败率 × 平均亏损绝对值（百分比口径）
    expectancy = win_rate / 100.0 * avg_win + (1 - win_rate / 100.0) * avg_loss

    # 前瞻收益
    fwd_summary: Dict[str, Dict[str, float]] = {}
    for p in cfg.get("forward_periods", [1, 3, 5, 10, 20]):
        vals = [t["forward_returns"].get(str(p), {}).get("pct") for t in trades]
        wins_p = [t["forward_returns"].get(str(p), {}).get("win") for t in trades]
        vals = [v for v in vals if v is not None]
        wins_p = [w for w in wins_p if w is not None]
        if vals:
            fwd_summary[str(p)] = {
                "pct_avg": float(np.mean(vals)),
                "win_rate": float(np.mean(wins_p) * 100.0) if wins_p else 0.0,
                "count": len(vals),
            }
        else:
            fwd_summary[str(p)] = {"pct_avg": 0.0, "win_rate": 0.0, "count": 0}

    # 出场原因分布
    exit_dist: Dict[str, int] = {}
    for t in trades:
        r = t.get("exit_reason", "未知")
        exit_dist[r] = exit_dist.get(r, 0) + 1

    return {
        "trades": trades,
        "stats": {
            "total_signals": n,
            "wins": int(wins.sum()),
            "losses": int(losses.sum()),
            "flat": int(flats.sum()),
            "win_rate": win_rate,
            "profit_loss_ratio": pl_ratio,
            "avg_ret": avg_ret,
            "avg_holding_days": avg_holding,
            "median_ret": median_ret,
            "best_ret": float(rets.max()),
            "worst_ret": float(rets.min()),
            "expectancy": expectancy,
        },
        "forward_returns": fwd_summary,
        "by_stock": by_stock,
        "config": cfg,
        "cancelled": cancelled,
        "skipped_limit_up": skipped_limit_up,
        "exit_reason_dist": exit_dist,
    }


# --------------------------------------------------------------------------
# 给策略管理 / 回测 UI 用的工具函数
# --------------------------------------------------------------------------
def trades_to_dataframe(trades: List[Dict[str, Any]]) -> pd.DataFrame:
    """将交易记录列表转为展示用的 DataFrame（按买入日期倒序）。"""
    if not trades:
        return pd.DataFrame()
    rows = []
    for t in trades:
        rows.append({
            "代码": t.get("code", ""),
            "名称": t.get("name", ""),
            "信号日": t.get("signal_date", ""),
            "买入日": t.get("buy_date", ""),
            "卖出日": t.get("sell_date", ""),
            "买入价": round(float(t.get("buy_price", 0)), 4),
            "卖出价": round(float(t.get("sell_price", 0)), 4),
            "持股天数": int(t.get("holding_days", 0)),
            "盈亏%": round(float(t.get("ret_pct", 0)), 2),
            "峰值盈亏%": round(float(t.get("peak_ret_pct", 0)), 2),
            "出场原因": t.get("exit_reason", ""),
        })
    df = pd.DataFrame(rows)
    return df.sort_values("买入日", ascending=False).reset_index(drop=True)


def format_summary_text(result: Dict[str, Any]) -> str:
    """把回测结果格式化成一段可读的中文摘要文本（用于状态栏 / 日志）。"""
    stats = result.get("stats", {})
    if stats.get("total_signals", 0) == 0:
        return ("⚠️ 回测区间内无信号"
                + ("（已取消）" if result.get("cancelled") else ""))
    return (
        f"总信号 {stats['total_signals']}｜"
        f"胜率 {stats['win_rate']:.1f}%｜"
        f"盈亏比 {stats['profit_loss_ratio']:.2f}｜"
        f"平均收益 {stats['avg_ret']:+.2f}%｜"
        f"平均持股 {stats['avg_holding_days']:.1f} 天"
    )
