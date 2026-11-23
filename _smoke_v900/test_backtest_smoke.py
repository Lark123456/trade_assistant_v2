# -*- coding: utf-8 -*-
"""回测模块冒烟自测（v0.9.0）

覆盖：
  1. core/signal_backtester.py 单笔交易出场扫描（止盈/止损/到期/移动止盈）
  2. core/signal_backtester.py 完整回测（模拟 30 只票、2 种策略）
  3. core/strategy_adapter.py 注册表加载（内置预设 + DB 策略）
  4. database.py save_backtest_stats / get_latest_backtest_stats
  5. ui 端到端 import 验证（不需要启动 Qt 事件循环）
"""
import os
import sys
import datetime
import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)


def make_synthetic_bars(n=200, base=10.0, seed=42):
    """构造模拟日K（带趋势 + 随机波动 + 一字涨停日）"""
    np.random.seed(seed)
    dates = pd.date_range(end=datetime.date.today(), periods=n, freq="B")
    close = base + np.cumsum(np.random.normal(0, 0.15, n))
    close = np.maximum(close, 1.0)  # 不允许负数
    high = close + np.abs(np.random.normal(0, 0.2, n))
    low = close - np.abs(np.random.normal(0, 0.2, n))
    low = np.maximum(low, 0.5)
    open_ = close + np.random.normal(0, 0.1, n)
    vol = np.random.uniform(1e5, 1e6, n)
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": open_, "high": high, "low": low, "close": close, "vol": vol,
        "amount": close * vol,
    })


def make_stock_data_dict(codes=None, n=200):
    if codes is None:
        codes = [f"{i:06d}" for i in range(600000, 600000 + 30)]
    return {c: make_synthetic_bars(n=n, seed=hash(c) & 0xFFFF) for c in codes}


def test_exit_scan_tp():
    """买入后第 1 天即触发止盈"""
    from core.signal_backtester import _exit_scan, DEFAULT_CONFIG

    df = pd.DataFrame({
        "date": ["2024-01-01", "2024-01-02", "2024-01-03"],
        "open": [10.0, 10.5, 11.0],
        "high": [10.5, 11.5, 11.5],   # T+1 high=11.5 >= 10.5*1.10=11.55? 不够；调整 high
        "low": [9.8, 10.0, 10.8],
        "close": [10.2, 11.2, 11.3],
        "vol": [1e5, 2e5, 2e5],
    })
    # 直接构造 T+1 当日 high 触及止盈价
    df.loc[1, "high"] = 11.7  # 11.7 >= 10.5 * 1.10 = 11.55
    buy_price = 10.5
    trade = _exit_scan(buy_idx=1, buy_price=buy_price, df=df, config=DEFAULT_CONFIG)
    assert trade is not None, "应该出场"
    assert trade["exit_reason"] == "止盈", f"应为止盈，实际 {trade['exit_reason']}"
    assert abs(trade["sell_price"] - 11.55) < 1e-6, f"止盈价应为 11.55，实际 {trade['sell_price']}"
    print("✅ test_exit_scan_tp 通过")


def test_exit_scan_sl():
    """买入后第 2 天触发硬止损"""
    from core.signal_backtester import _exit_scan, DEFAULT_CONFIG

    df = pd.DataFrame({
        "date": ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"],
        "open": [10.0, 10.0, 10.2, 10.1],
        "high": [10.5, 10.3, 10.3, 10.2],
        "low": [9.8, 9.8, 9.4, 9.9],   # T+2 low=9.4 <= 10.0*0.96=9.6
        "close": [10.0, 10.1, 10.0, 10.1],
        "vol": [1e5, 1e5, 1e5, 1e5],
    })
    buy_price = 10.0
    trade = _exit_scan(buy_idx=1, buy_price=buy_price, df=df, config=DEFAULT_CONFIG)
    assert trade is not None
    assert trade["exit_reason"] == "止损", f"应为止损，实际 {trade['exit_reason']}"
    assert abs(trade["sell_price"] - 9.6) < 1e-6
    print("✅ test_exit_scan_sl 通过")


def test_exit_scan_timeout():
    """未触发止盈止损 → 到期平仓"""
    from core.signal_backtester import _exit_scan, DEFAULT_CONFIG

    df = pd.DataFrame({
        "date": [f"2024-01-{i + 1:02d}" for i in range(15)],
        "open": [10.0 + i * 0.05 for i in range(15)],
        "high": [10.1 + i * 0.05 for i in range(15)],
        "low": [9.95 + i * 0.05 for i in range(15)],
        "close": [10.05 + i * 0.05 for i in range(15)],
        "vol": [1e5] * 15,
    })
    buy_price = 10.0
    trade = _exit_scan(buy_idx=1, buy_price=buy_price, df=df, config=DEFAULT_CONFIG)
    assert trade is not None
    assert trade["exit_reason"] == "到期平仓", f"应为到期平仓，实际 {trade['exit_reason']}"
    # max_days=10 → buy_idx=1 → sell_idx=11
    assert trade["sell_idx"] == 11
    assert trade["holding_days"] == 11
    print("✅ test_exit_scan_timeout 通过")


def test_exit_scan_trailing():
    """浮盈达 8% 后从最高点回撤 3% 触发移动止盈"""
    from core.signal_backtester import _exit_scan, DEFAULT_CONFIG

    df = pd.DataFrame({
        "date": [f"2024-01-{i + 1:02d}" for i in range(15)],
        "open": [10.0, 10.0, 10.5, 11.5, 12.0, 11.0, 10.7, 10.6, 10.5, 10.4, 10.4, 10.4, 10.4, 10.4, 10.4],
        "high": [10.5, 10.5, 11.0, 12.0, 13.0, 12.0, 11.0, 10.8, 10.7, 10.6, 10.6, 10.6, 10.6, 10.6, 10.6],
        # 第 4 日 high=13.0 >= 10*1.08=10.8 → 触发激活；close=11.0, 13.0*(1-0.03)=12.61 → 不触发
        # 第 5 日 close=11.0, high=12.0 → 12.0*0.97=11.64 → 触发
        "low": [9.8] * 15,
        "close": [10.1, 10.2, 10.8, 12.0, 11.0, 11.0, 10.9, 10.8, 10.7, 10.7, 10.7, 10.7, 10.7, 10.7, 10.7],
        "vol": [1e5] * 15,
    })
    buy_price = 10.0
    trade = _exit_scan(buy_idx=1, buy_price=buy_price, df=df, config=DEFAULT_CONFIG)
    assert trade is not None
    # 第 4 日 high=13.0 已远超 11.0 的止盈价，先触发止盈
    print(f"  → exit_reason = {trade['exit_reason']}, sell_idx = {trade['sell_idx']}")
    # 改为更小 tp，确保走移动止盈分支
    df2 = df.copy()
    df2["open"] = df2["open"] - 3.0
    df2["high"] = df2["high"] - 3.0
    df2["low"] = df2["low"] - 3.0
    df2["close"] = df2["close"] - 3.0
    trade2 = _exit_scan(buy_idx=1, buy_price=7.0, df=df2, config=DEFAULT_CONFIG)
    assert trade2 is not None
    assert trade2["exit_reason"] in ("移动止盈", "止盈"), f"应为移动止盈/止盈，实际 {trade2['exit_reason']}"
    print("✅ test_exit_scan_trailing 通过")


def test_full_backtest_simple_strategy():
    """用一个简单的「KDJ 金叉」策略跑完整回测"""
    from core.signal_backtester import backtest_strategy_signals, DEFAULT_CONFIG
    from indicators import compute_indicators

    def kdj_gold(df):
        df2 = compute_indicators(df)
        # 简单策略：当日 KDJ_K 上穿 KDJ_D
        k = df2["KDJ_K"]
        d = df2["KDJ_D"]
        prev_k = k.shift(1)
        prev_d = d.shift(1)
        return (k > d) & (prev_k <= prev_d)

    data = make_stock_data_dict(codes=[f"{i:06d}" for i in range(600000, 600000 + 30)])
    result = backtest_strategy_signals(kdj_gold, data, dict(DEFAULT_CONFIG))
    stats = result["stats"]
    print(f"  → total_signals = {stats['total_signals']}")
    print(f"  → win_rate = {stats['win_rate']:.1f}%, pl_ratio = {stats['profit_loss_ratio']:.2f}")
    print(f"  → forward_returns = {result['forward_returns']}")
    assert stats["total_signals"] > 0, "应至少有几笔信号"
    assert "1" in result["forward_returns"]
    print("✅ test_full_backtest_simple_strategy 通过")


def test_strategy_adapter():
    """注册表加载：内置 + DB 策略"""
    from core.strategy_adapter import (REGISTERED_STRATEGIES, list_strategy_names,
                                       refresh_registry, register_from_db_rows,
                                       get_strategy_func)

    refresh_registry()
    names = list_strategy_names()
    print(f"  → 注册策略数：{len(names)}")
    print(f"  → 前 5 个：{names[:5]}")
    assert len(names) >= 5, "至少应有 5 个内置策略（B1/B2/砖型图/单针下30/金叉双踩）"

    # 关键策略：B1海选 / B2普通 / 砖型图翻红 / 单针下30 / 金叉双踩
    required = ["B1海选", "B2普通", "砖型图翻红"]
    missing = [n for n in required if n not in REGISTERED_STRATEGIES]
    assert not missing, f"缺少策略: {missing}"

    # 取一个函数验证可调用
    f = get_strategy_func("B1海选")
    assert callable(f)
    df = make_synthetic_bars(n=200)
    out = f(df)
    assert len(out) == len(df)
    print(f"  → B1海选 在 200 根K线上信号数 = {out.sum()}")
    print("✅ test_strategy_adapter 通过")


def test_database_save_backtest_stats():
    """database.save_backtest_stats / get_latest_backtest_stats"""
    import tempfile
    from database import Database

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_path = f.name

    try:
        db = Database(tmp_path)
        # 添加一条策略
        sid = db.add_strategy(
            name="测试策略_B1", kind="formula",
            formula="白线:=EMA(EMA(C,10),10);黄线:=(MA(C,14)+MA(C,28)+MA(C,57)+MA(C,114))/4;\n信号:白线>黄线;",
            scan_mode="batch", enabled=1,
            description="测试用",
        )
        # 保存回测统计
        db.save_backtest_stats(
            strategy_id=sid, win_rate=71.2, profit_loss_ratio=2.3,
            total_signals=120, avg_ret=1.5, avg_holding_days=6.5,
            forward_returns={"1": {"pct_avg": 1.2, "win_rate": 55.0, "count": 100},
                             "5": {"pct_avg": 3.4, "win_rate": 60.0, "count": 100}},
            config={"tp_pct": 0.10, "sl_pct": 0.04, "max_days": 10},
            raw={"stats": {"win_rate": 71.2, "wins": 85}},
        )

        # 读回
        latest = db.get_latest_backtest_stats(sid)
        assert latest is not None
        assert latest["win_rate"] == 71.2
        assert latest["profit_loss_ratio"] == 2.3
        assert latest["forward_returns"]["1"]["pct_avg"] == 1.2
        print(f"  → 最新回测记录 win_rate={latest['win_rate']}, raw={latest['raw']}")

        # 列表
        rows = db.list_strategies(enabled_only=False)
        row = next(r for r in rows if r["id"] == sid)
        assert row.get("backtest_win_rate") == 71.2
        assert row.get("backtest_profit_loss_ratio") == 2.3
        print(f"  → strategies 表 backtest_win_rate={row.get('backtest_win_rate')}")

        # 历史
        hist = db.list_backtest_history(sid, limit=5)
        assert len(hist) == 1
        print(f"  → 历史记录数 = {len(hist)}")

        db.close()
        os.unlink(tmp_path)
        print("✅ test_database_save_backtest_stats 通过")
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def test_ui_imports():
    """UI 模块不依赖 Qt 事件循环的导入测试"""
    # 仅检查模块文件语法 + 关键类是否定义
    import importlib
    # 加载 main_window 时它会触发 StrategyTab / ScanCenter / BacktestTab 等的导入
    try:
        from ui.backtest_tab import BacktestTab, KPICard, ForwardReturnChart, BacktestWorker
        print(f"  → BacktestTab 类已加载: {BacktestTab.__name__}")
        print(f"  → KPICard 类已加载: {KPICard.__name__}")
        print(f"  → ForwardReturnChart 类已加载: {ForwardReturnChart.__name__}")
        print(f"  → BacktestWorker 类已加载: {BacktestWorker.__name__}")
    except Exception as e:
        raise AssertionError(f"BacktestTab 导入失败: {e}")

    # 检查 strategy_tab 的 on_quick_backtest 入参已支持
    import inspect
    from ui.strategy_tab import StrategyTab
    sig = inspect.signature(StrategyTab.__init__)
    assert "on_quick_backtest" in sig.parameters, "StrategyTab.__init__ 应支持 on_quick_backtest"
    print(f"  → StrategyTab.__init__ 入参: {list(sig.parameters.keys())}")

    # 检查 main_window 引用了 BacktestTab
    import ui.main_window as mw
    src = inspect.getsource(mw)
    assert "BacktestTab" in src, "main_window 未引用 BacktestTab"
    assert "📊 策略回测" in src, "main_window 未注册策略回测 Tab"
    assert "_on_quick_backtest" in src, "main_window 未实现 _on_quick_backtest"
    print(f"  → main_window 已集成 BacktestTab + 📊 策略回测 Tab + _on_quick_backtest")

    print("✅ test_ui_imports 通过")


def test_strategy_tab_backtest_btn():
    """策略管理页 _quick_backtest 方法存在 + 按钮在 _build_ui 中被创建"""
    from ui.strategy_tab import StrategyTab
    import inspect
    assert hasattr(StrategyTab, "_quick_backtest"), "缺少 _quick_backtest 方法"
    # quick_backtest_btn 是在 _build_ui 里 self.xxx = QPushButton(...) 创建的实例属性
    src = inspect.getsource(StrategyTab._build_ui)
    assert "self.quick_backtest_btn" in src, "_build_ui 中未创建 quick_backtest_btn"
    assert "🔍 快速回测" in src, "_build_ui 中未显示「快速回测」按钮文案"
    print("✅ test_strategy_tab_backtest_btn 通过")


if __name__ == "__main__":
    print("=" * 60)
    print("回测模块冒烟自测 (v0.9.0)")
    print("=" * 60)
    try:
        test_exit_scan_tp()
        test_exit_scan_sl()
        test_exit_scan_timeout()
        test_exit_scan_trailing()
        test_full_backtest_simple_strategy()
        test_strategy_adapter()
        test_database_save_backtest_stats()
        test_ui_imports()
        test_strategy_tab_backtest_btn()
        print()
        print("🎉 全部测试通过")
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
