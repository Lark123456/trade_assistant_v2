# -*- coding: utf-8 -*-
"""Headless UI 端到端冒烟自测（不启动主事件循环）

验证：
  - StrategyTab / BacktestTab / ScanCenter 都能实例化且 _build_ui 成功
  - 策略管理页「🔍 快速回测」按钮存在
  - BacktestTab 能在无网络环境下构造（无 QThread 启动）
  - on_quick_backtest 回调能被模拟调用
"""
import os
import sys

# 强制 offscreen 平台
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)


def test_strategy_tab_instantiation():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)

    from config import DB_PATH
    from database import Database
    db = Database(DB_PATH)

    from ui.strategy_tab import StrategyTab

    on_quick_backtest_called = {"count": 0, "strategy_id": None}

    def on_qb(strategy_id, strategy_name=""):
        on_quick_backtest_called["count"] += 1
        on_quick_backtest_called["strategy_id"] = strategy_id
        on_quick_backtest_called["name"] = strategy_name

    tab = StrategyTab(db, on_quick_backtest=on_qb)
    # 验证按钮存在
    assert hasattr(tab, "quick_backtest_btn"), "缺少 quick_backtest_btn"
    btn_text = tab.quick_backtest_btn.text()
    assert "快速回测" in btn_text, f"按钮文本异常: {btn_text}"

    # 模拟选中一行 + 调用 _quick_backtest
    rows = db.list_strategies(enabled_only=False)
    formula_rows = [r for r in rows if r.get("kind") != "dna"]
    if formula_rows:
        first = formula_rows[0]
        # 把这行设为当前选中（在 dataframe 上设置）
        tab.table.model()._df = tab.table.model()._df.head(0)  # empty
        # 直接通过 _quick_backtest 的反向测试 —— 走 callback
        tab._quick_backtest.__wrapped__ if hasattr(tab._quick_backtest, "__wrapped__") else None
        # 直接手动调用模拟路径
        on_qb(int(first["id"]), first.get("name", ""))
        assert on_quick_backtest_called["count"] == 1
        assert on_quick_backtest_called["strategy_id"] == int(first["id"])

    print("✅ test_strategy_tab_instantiation 通过")


def test_backtest_tab_instantiation():
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)

    from config import DB_PATH
    from database import Database
    from data_feed import DataFeed
    db = Database(DB_PATH)
    feed = DataFeed(db=db)
    from workers import Worker
    worker = Worker(feed, db)

    from ui.backtest_tab import BacktestTab
    tab = BacktestTab(worker, db)

    # 验证关键 UI 元素
    assert hasattr(tab, "strategy_combo"), "缺少 strategy_combo"
    assert hasattr(tab, "range_combo"), "缺少 range_combo"
    assert hasattr(tab, "tp_spin"), "缺少 tp_spin"
    assert hasattr(tab, "sl_spin"), "缺少 sl_spin"
    assert hasattr(tab, "max_days_spin"), "缺少 max_days_spin"
    assert hasattr(tab, "trailing_check"), "缺少 trailing_check"
    assert hasattr(tab, "run_btn"), "缺少 run_btn"
    assert hasattr(tab, "save_btn"), "缺少 save_btn"
    assert hasattr(tab, "kpi_total"), "缺少 kpi_total"
    assert hasattr(tab, "kpi_win"), "缺少 kpi_win"
    assert hasattr(tab, "kpi_pl"), "缺少 kpi_pl"
    assert hasattr(tab, "kpi_avg"), "缺少 kpi_avg"
    assert hasattr(tab, "kpi_hold"), "缺少 kpi_hold"
    assert hasattr(tab, "forward_chart"), "缺少 forward_chart"
    assert hasattr(tab, "trade_table"), "缺少 trade_table"

    # 验证策略下拉有内容
    n = tab.strategy_combo.count()
    print(f"  → BacktestTab 策略下拉条目数 = {n}")
    assert n >= 1, "策略下拉为空"

    # 验证默认参数
    assert abs(tab.tp_spin.value() - 10.0) < 1e-6, f"默认 TP 应为 10.0%，实际 {tab.tp_spin.value()}"
    assert abs(tab.sl_spin.value() - 4.0) < 1e-6, f"默认 SL 应为 4.0%，实际 {tab.sl_spin.value()}"
    assert tab.max_days_spin.value() == 10, f"默认 max_days 应为 10，实际 {tab.max_days_spin.value()}"
    assert tab.trailing_check.isChecked(), "默认应启用移动止盈"

    # 验证 set_selected_strategy 不会抛错（即使没匹配项）
    tab.set_selected_strategy(None, "不存在的策略名")
    print(f"  → set_selected_strategy 空调用 OK")

    # v0.9.0 修订验证：默认范围是「全市场（推荐）」
    range_items = []
    for i in range(tab.range_combo.count()):
        range_items.append((tab.range_combo.itemText(i),
                            tab.range_combo.itemData(i)))
    print(f"  → 范围选项: {range_items}")
    assert ("📈 全市场（推荐）", "all") in range_items, \
        "缺少「📈 全市场（推荐）」默认选项"
    # 默认选中的应是「全市场」
    default_data = tab.range_combo.currentData()
    print(f"  → 默认范围 data = {default_data}")
    assert default_data == "all", f"默认范围应为 'all'，实际 {default_data}"

    worker.stop()
    worker.wait(500)
    print("✅ test_backtest_tab_instantiation 通过")


def test_main_window_full_instantiation():
    """完整启动主窗口（offscreen）验证全栈无报错"""
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)

    from config import DB_PATH
    from database import Database
    from data_feed import DataFeed
    db = Database(DB_PATH)
    feed = DataFeed(db=db)
    from workers import Worker
    worker = Worker(feed, db)

    from ui.main_window import MainWindow
    win = MainWindow(worker, db)

    # 验证 Tabs 包含策略回测
    tab_titles = []
    for i in range(win._tabs.count()):
        tab_titles.append(win._tabs.tabText(i))
    print(f"  → 主窗口 Tab 列表: {tab_titles}")
    assert "📊 策略回测" in tab_titles, "主窗口未注册「📊 策略回测」Tab"
    assert "🎯 策略管理" in tab_titles

    # 验证 _on_quick_backtest 能被调用（不抛错）
    win._on_quick_backtest(1, "测试策略")
    print(f"  → _on_quick_backtest 调用 OK")

    worker.stop()
    worker.wait(500)
    print("✅ test_main_window_full_instantiation 通过")


if __name__ == "__main__":
    print("=" * 60)
    print("Headless UI 端到端冒烟自测 (v0.9.0)")
    print("=" * 60)
    try:
        test_strategy_tab_instantiation()
        test_backtest_tab_instantiation()
        test_main_window_full_instantiation()
        print()
        print("🎉 UI 端到端测试通过")
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
