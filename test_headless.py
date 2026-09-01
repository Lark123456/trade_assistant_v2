# -*- coding: utf-8 -*-
"""无头集成测试：验证数据层、公式解析、扫描与提醒逻辑（不依赖 GUI）。

运行：python test_headless.py
"""
import datetime
import logging
import os
import sys
import tempfile

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 测试归档写入临时目录，避免污染真实 archive/strategies
import config  # noqa: E402
config.ARCHIVE_DIR = os.path.join(tempfile.gettempdir(), "ta_archive_test")

from alert_engine import check_bar_alerts, check_quote_alerts, describe_alert  # noqa: E402
from database import Database  # noqa: E402
from data_feed import DataFeed, infer_market  # noqa: E402
from formula_explain import explain_formula, format_explain_lines  # noqa: E402
from indicators import (PRESET_FORMULAS, check_formula,  # noqa: E402
                        compute_indicators, evaluate_formula,
                        evaluate_script, formula_last_signal,
                        formula_signal, snapshot_indicators)
from scanner import scan_by_quotes, scan_formula, strategy_formula  # noqa: E402
from strategy_presets import all_presets  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def test_formula_parser(df):
    print("[1] 公式解析器")
    tests = [
        ("C >= O OR C < O", True),
        ("(C > O) OR (C <= O)", True),
        ("MA(C,5) > MA(C,10)", None),
        ("CROSS(MA(C,5),MA(C,10))", None),
        ("V > REF(V,1) * 1.5", None),
        ("C > REF(HHV(H,20),1)", None),
        ("COUNT(C>O,5) >= 3", None),
        ("BARSLAST(CROSS(KDJ_K,KDJ_D)) <= 3", None),
        ("ABS(C-O) >= 0", True),
        ("L < LLV(L,60)", None),
        ("NOT (C < O) AND V > 0", None),
        ("MAX(C,O) - MIN(C,O) >= 0", True),
        # 新增函数
        ("IF(C > O, C, O) >= O", True),
        ("SMA(C,5,1) >= 0", True),
        ("WMA(C,5) > 0", True),
        ("DMA(C,0.2) > 0", True),
        ("SLOPE(C,5) >= -1000", None),
        ("EXIST(C > O, 5)", None),
        ("EVERY(C > 0, 3)", True),
        ("BETWEEN(C, 0, 100000)", True),
        ("FILTER(C > O, 2) OR C <= O", True),
        ("IF(CROSS(MA(C,5),MA(C,10)), 1, 0) >= 0", True),
    ]
    for expr, expected in tests:
        try:
            val = evaluate_formula(df, expr)
            ok = (expected is None) or (bool(val[-1]) == expected)
            check(expr, ok, f"last={val[-1]}")
        except Exception as e:  # noqa: BLE001
            check(expr, False, str(e))
    # 预设公式都能跑通
    for name, expr in PRESET_FORMULAS.items():
        try:
            formula_last_signal(df, expr)
            check(f"预设公式 {name}", True)
        except Exception as e:  # noqa: BLE001
            check(f"预设公式 {name}", False, str(e))


def test_database():
    print("[2] 数据库")
    tmp = os.path.join(tempfile.gettempdir(), "ta_test.db")
    if os.path.exists(tmp):
        os.remove(tmp)
    db = Database(tmp)
    wid = db.add_watch("600000", "浦发银行", 1, trigger_indicator="MACD金叉",
                       entry_price=9.0, reason="测试")
    check("添加观察池", wid is not None)
    tid = db.add_trade(wid, "600000", "浦发银行", "开仓", "MACD金叉",
                       9.1, 1000, "测试备注")
    check("添加交易记录", tid is not None)
    sid = db.add_snapshot(tid, "600000", 1, {"MACD_DIF": 0.1, "KDJ_K": 60.0},
                          market_index=3200.5, volume=12345)
    check("添加因子快照", sid is not None)
    snap = db.get_snapshot(sid)
    check("读取因子快照", snap is not None and "MACD_DIF" in snap["indicators"])
    aid = db.add_alert("600000", "浦发银行", 1, "price", ">=", 9.5)
    db.mark_alert_triggered(aid, "测试触发")
    check("提醒触发计数", db.list_alerts()[0]["triggered_count"] == 1)
    check("提醒日志", len(db.list_alert_logs()) == 1)
    trades = db.list_trades(code="600000")
    check("交易查询", len(trades) == 1)
    db.update_watch(wid, status="已开仓")
    check("状态更新", db.get_watch(wid)["status"] == "已开仓")
    db.close()
    os.remove(tmp)


def test_alert_engine():
    print("[3] 提醒引擎")
    alert = {"id": 1, "code": "600000", "name": "浦发银行", "alert_type": "price",
             "operator": ">=", "threshold": 9.5, "enabled": 1,
             "params": {"cooldown": 3600}, "last_triggered_at": None}
    quote = {"code": "600000", "name": "浦发银行", "price": 9.6, "pct": 2.0, "vol": 1000}
    ev = check_quote_alerts([alert], quote)
    check("价格提醒触发", len(ev) == 1 and "9.60" in ev[0]["message"])
    quote2 = dict(quote, price=9.4)
    check("价格未达阈值不触发", len(check_quote_alerts([alert], quote2)) == 0)
    alert2 = dict(alert, alert_type="percent", operator="<=", threshold=-3)
    quote3 = dict(quote, pct=-4.2)
    check("涨跌幅提醒", len(check_quote_alerts([alert2], quote3)) == 1)

    # 策略信号提醒（v0.6.5）：公式快照 + 编号/版本，信号命中即提醒
    from indicators import compute_indicators
    dfi = compute_indicators(_synthetic_bars())
    base = {"id": 99, "code": "600000", "name": "浦发银行",
            "alert_type": "strategy_signal", "operator": "signal",
            "threshold": 0, "enabled": 1, "last_triggered_at": None}
    a_true = dict(base, params={"formula": "C>=0", "strategy_name": "恒真策略",
                                "strategy_code": "S0001", "strategy_version": 2,
                                "cooldown": 3600})
    ev_ss = check_bar_alerts([a_true], "600000", dfi)
    check("策略信号提醒（恒真公式触发）",
          len(ev_ss) == 1 and "恒真策略" in ev_ss[0]["message"], ev_ss)
    a_false = dict(base, params={"formula": "C<0", "strategy_name": "恒假策略",
                                 "strategy_code": "S0002", "strategy_version": 1,
                                 "cooldown": 3600})
    check("策略信号提醒（恒假公式不触发）",
          len(check_bar_alerts([a_false], "600000", dfi)) == 0)
    check("策略信号描述",
          "策略信号 · 恒真策略" in describe_alert(a_true) and "v2" in describe_alert(a_true),
          describe_alert(a_true))


def test_data_layer():
    print("[4] pytdx 数据层")
    feed = DataFeed()
    lst = feed.get_stock_list()
    check("全市场股票列表 > 3000", lst is not None and len(lst) > 3000, f"got {len(lst) if lst is not None else 0}")
    quotes = feed.get_quotes([(1, "600000"), (0, "000001"), (0, "300750")])
    check("实时行情", len(quotes) == 3 and quotes["price"].notna().all())
    bars = feed.get_daily_bars("600000", market=1, count=300)
    check("日K线 ≥ 200 根", bars is not None and len(bars) >= 200, f"got {len(bars) if bars is not None else 0}")
    if bars is not None and len(bars) >= 2:
        check("日K线按日期升序（时间正序）",
              bars["date"].iloc[-1] >= bars["date"].iloc[0],
              f"first={bars['date'].iloc[0]} last={bars['date'].iloc[-1]}")
    idx = feed.get_index_quotes()
    check("指数行情", len(idx) == 3)
    feed.disconnect()

    # 交易分钟数
    from data_feed import trading_minutes_elapsed
    import datetime as _dt
    t930 = _dt.datetime(2026, 8, 14, 10, 0)   # 周一 10:00 -> 45分钟(9:15起)
    check("交易分钟(10:00=45)", abs(trading_minutes_elapsed(t930) - 45) < 0.01,
          str(trading_minutes_elapsed(t930)))
    t_after = _dt.datetime(2026, 8, 14, 16, 0)  # 收盘后 -> 240
    check("交易分钟(收盘后=240)", trading_minutes_elapsed(t_after) == 240)
    t_sat = _dt.datetime(2026, 8, 15, 10, 0)    # 周六 -> 0
    check("交易分钟(周末=0)", trading_minutes_elapsed(t_sat) == 0)


def test_scan():
    print("[5] 扫描器")
    feed = DataFeed()
    lst = feed.get_stock_list()
    quotes = feed.get_quotes([(r.market, r.code) for r in lst.head(200).itertuples()])
    feed.disconnect()
    r1 = scan_by_quotes(quotes, min_pct=0, max_pct=10, min_amount=1e8)
    check("行情过滤扫描", len(r1) >= 0)
    # 公式扫描用真实小样本（10 只），验证不崩溃且命中逻辑正确
    feed2 = DataFeed()
    sub = lst.head(10)
    # v0.8.1 测试健壮性：scan_formula 的 quote_filter 会剔除 |pct|>9.9 的标的
    # （近涨停/跌停票不参与候选，属设计行为）。用与内部一致的口径预估候选数，
    # 恒真公式应对"通过行情过滤的所有候选"全命中，而不是固定 10 只。
    sub_quotes = quotes[quotes["code"].isin(sub["code"])]
    sub_quotes = sub_quotes[(sub_quotes["pct"] >= -9.9) & (sub_quotes["pct"] <= 9.9)]
    expected = len(sub_quotes)
    res = scan_formula(feed2, sub, "C >= O OR C < O", max_stocks=10,
                       quote_filter=sub_quotes, bars_count=120)
    feed2.disconnect()
    check("公式扫描完成且全部命中（恒真公式）", len(res) == expected and expected > 0,
          f"got {len(res)} expected {expected}")
    print(f"      公式扫描命中 {len(res)} 只（候选 {expected} 只，恒真公式应全部命中）")
    # 策略 -> 公式
    st_f = {"kind": "formula", "formula": "C > O"}
    check("公式策略解析", strategy_formula(st_f) == "C > O")
    check("空公式策略解析为空", strategy_formula({"kind": "formula", "formula": "  "}) == "")


def test_new_db_features():
    print("[6] 新数据库功能（策略/扫描历史/K线缓存）")
    tmp = os.path.join(tempfile.gettempdir(), "ta_db2_test.db")
    if os.path.exists(tmp):
        os.remove(tmp)
    db = Database(tmp)
    sid = db.add_strategy("测试策略", kind="formula", formula="C>O", scan_mode="batch")
    check("添加策略", sid is not None)
    check("策略列表", len(db.list_strategies()) == 1
          and db.list_strategies()[0]["name"] == "测试策略")
    db.update_strategy(sid, enabled=0, scan_mode="intraday")
    check("策略更新", db.get_strategy(sid)["scan_mode"] == "intraday"
          and not db.get_strategy(sid)["enabled"])
    db.delete_strategy(sid)
    check("策略删除", len(db.list_strategies()) == 0)

    # 策略编号 + 版本迭代（v0.6.5）
    sid2 = db.add_strategy("编号测试", kind="formula", formula="C>O", scan_mode="batch")
    st2 = db.get_strategy(sid2)
    check("策略编号生成", (st2.get("code") or "").startswith("S"), str(st2.get("code")))
    code_before = st2["code"]
    vers1 = db.list_strategy_versions(sid2)
    check("新增即 v1", len(vers1) == 1 and vers1[0]["version"] == 1)
    db.update_strategy(sid2, name="编号测试-改名", formula="C>=O")
    st2b = db.get_strategy(sid2)
    check("改名后编号不变", st2b["code"] == code_before,
          f"{st2b.get('code')} vs {code_before}")
    vers2 = db.list_strategy_versions(sid2)
    check("公式变更产生 v2", len(vers2) == 2 and vers2[0]["version"] == 2,
          [v["version"] for v in vers2])
    db.update_strategy(sid2, enabled=0)
    check("启停不产生新版本", len(db.list_strategy_versions(sid2)) == 2)
    v1 = db.get_strategy_version(sid2, 1)
    check("版本内容可查（v1 保留旧公式）", v1 and v1["formula"] == "C>O",
          str(v1.get("formula") if v1 else None))
    check("最新版本号查询", db.get_strategy_latest_version(sid2) == 2)
    db.delete_strategy(sid2)

    # 提醒 params JSON 解析（B003 回归）
    aid2 = db.add_alert("600000", "浦发银行", 1, "price", ">=", 9.0,
                        params={"cooldown": 3600})
    row_a = [r for r in db.list_alerts() if r["id"] == aid2][0]
    check("提醒 params 解析为 dict（B003）",
          isinstance(row_a.get("params"), dict) and row_a["params"].get("cooldown") == 3600,
          type(row_a.get("params")).__name__)

    # 扫描历史
    from scanner import ScanResult
    hits = [ScanResult(code="600000", name="浦发银行", market=1, price=9.1,
                       pct=1.0, match_reason="C>O")]
    db.add_scan_hits("sess1", 1, "策略A", hits)
    db.add_scan_hits("sess2", 1, "策略A", hits)
    check("扫描历史写入", len(db.list_scan_history(day=datetime.date.today().isoformat())) == 2)
    check("扫描会话去重", len(db.list_scan_sessions()) == 2)
    check("当日命中去重", len(db.hits_today()) == 1)
    check("排除会话后仍含另一会话", len(db.hits_today(session_exclude="sess2")) == 1
          and db.hits_today(session_exclude="sess2")[0]["code"] == "600000")

    # K线缓存
    import pandas as pd
    bars = pd.DataFrame({
        "date": ["2026-01-01", "2026-01-02"], "open": [1.0, 1.1],
        "high": [1.2, 1.3], "low": [0.9, 1.0], "close": [1.1, 1.2],
        "vol": [100, 200], "amount": [1e6, 2e6],
    })
    db.cache_daily_bars("600000", 1, bars)
    cached = db.get_cached_daily_bars("600000")
    check("K线缓存写入读取", len(cached) == 2 and cached["close"].iloc[-1] == 1.2)
    check("缓存最新日期", db.cache_latest_date("600000") == "2026-01-02")

    # 行情日历：命中日期聚合（v0.6.6）
    import datetime as _dt
    today = _dt.date.today()
    db.add_scan_hits("sess3", 1, "策略A", hits)
    dates = db.list_scan_dates(today.year, today.month)
    check("命中日期聚合（含今天）", today.isoformat() in dates, str(dates[:5]))
    check("其他月份为空", db.list_scan_dates(1999, 1) == [])

    db.close()
    os.remove(tmp)


def _synthetic_bars(n=160, seed=7):
    """构造合成日K（随机游走），用于公式引擎/内置库测试。"""
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(seed)
    close = 10 + np.cumsum(rng.uniform(-0.15, 0.15, n))
    close = np.clip(close, 5, 20)
    open_ = close + rng.uniform(-0.1, 0.1, n)
    high = np.maximum(open_, close) + rng.uniform(0.05, 0.4, n)
    low = np.minimum(open_, close) - rng.uniform(0.05, 0.4, n)
    return pd.DataFrame({
        "date": [f"2026-01-{(i % 28) + 1:02d}" for i in range(n)],
        "open": open_, "high": high, "low": low, "close": close,
        "vol": rng.uniform(1e5, 2e5, n), "amount": rng.uniform(1e8, 2e8, n),
    })


def test_formula_script():
    print("[7] 公式引擎升级（脚本/中文变量/多输出/序列REF）")
    df = compute_indicators(_synthetic_bars())
    # 赋值 + 中文变量 + 注释 + 多输出
    f1 = ("{ 大前提 }"
          "白线:=EMA(EMA(C,10),10);"
          "黄线:=(MA(C,14)+MA(C,28)+MA(C,57)+MA(C,114))/4;"
          "趋势好:=白线>黄线;"
          "昨天B1:=REF(KDJ_J,1)<13;"
          "完美:趋势好 AND KDJ_J>13;"
          "稍欠:趋势好 AND KDJ_J>0;"
          "B2普通:趋势好 AND 昨天B1;")
    sig, labels = formula_signal(df, f1)
    check("多输出公式信号与标注", isinstance(labels, dict)
          and "完美" in labels and "稍欠" in labels)
    outputs, env, last, lines = evaluate_script(df, f1)
    check("命名输出为 dict 且顺序保持", outputs is not None
          and list(outputs) == ["完美", "稍欠", "B2普通"])
    check("赋值进入环境", "趋势好" in env and "白线" in env)
    check("最后输出作为信号", sig == bool(
        np.nan_to_num(np.asarray(outputs["B2普通"])[-1]) > 0))
    # 序列 REF（灾后重建核心）
    f2 = ("金叉:=CROSS(EMA(EMA(C,10),10),(MA(C,14)+MA(C,28)+MA(C,57)+MA(C,114))/4);"
          "金叉日:=BARSLAST(金叉);"
          "建波均量:=REF(MA(V,5),金叉日);"
          "缩量达标:=MA(V,5)<建波均量*0.75;"
          "XG:EXIST(金叉,30) AND 缩量达标;")
    check("序列参数 REF 可执行", formula_signal(df, f2) is not None)
    # 单针下30 完整公式（与通达信副图一致）
    f4 = ("N1:=3; N2:=21;"
          "D1:=HHV(C,N1)-LLV(L,N1); D2:=HHV(C,N2)-LLV(L,N2);"
          "D10:=HHV(C,10)-LLV(L,10); D20:=HHV(C,20)-LLV(L,20);"
          "短期:=100*(C-LLV(L,N1))/MAX(D1,0.0001);"
          "中期:=100*(C-LLV(L,10))/MAX(D10,0.0001);"
          "中长期:=100*(C-LLV(L,20))/MAX(D20,0.0001);"
          "长期:=100*(C-LLV(L,N2))/MAX(D2,0.0001);"
          "四线归零买:=IF(短期<=6 AND 中期<=6 AND 中长期<=6 AND 长期<=6,-30,0);"
          "白线下20买:=IF(短期<=20 AND 长期>=60,-30,0);"
          "白穿红线买:=IF(CROSS(短期,长期) AND 长期<20,-30,0);"
          "白穿黄线买:=IF(CROSS(短期,中期) AND 中期<30,-30,0);"
          "参考线85:=85; 参考线30:=30;"
          "单针下30信号:长期>70 AND 短期<30;")
    sig4, labels4 = formula_signal(df, f4)
    outputs4, env4, _last4, _lines4 = evaluate_script(df, f4)
    check("单针下30 完整公式可执行", isinstance(sig4, bool))
    check("单针下30 环境含中期/中长期", "中期" in env4 and "中长期" in env4)
    # 旧式单表达式兼容
    check("旧式单表达式仍兼容",
          isinstance(formula_last_signal(df, "CROSS(MA(C,5),MA(C,10))"), bool))
    # 砖型图
    f5 = ("VAR1A:=(HHV(H,4)-C)/(HHV(H,4)-LLV(L,4))*100-90;"
          "VAR2A:=SMA(VAR1A,4,1)+100;"
          "VAR3A:=(C-LLV(L,4))/(HHV(H,4)-LLV(L,4))*100;"
          "VAR4A:=SMA(VAR3A,6,1);"
          "VAR5A:=SMA(VAR4A,6,1)+100;"
          "VAR6A:=VAR5A-VAR2A;"
          "砖型图:=IF(VAR6A>4,VAR6A-4,0);"
          "AA:=REF(砖型图,1)<砖型图;"
          "CC:=REF(AA,1)=0 AND AA=1;"
          "XG:CC>0;")
    check("砖型图公式可执行", isinstance(formula_signal(df, f5)[0], bool))


def test_preset_library():
    print("[8] 内置策略/指标库与中文释义")
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "ta_preset_test.db")
    if os.path.exists(tmp):
        os.remove(tmp)
    db = Database(tmp)
    n_ok = 0
    for p in all_presets():
        ok, err = check_formula(p["formula"])
        check(f"预设公式 {p['name']}", ok, err)
        if ok:
            n_ok += 1
    check("全部预设公式通过校验", n_ok == len(all_presets()), f"{n_ok}/{len(all_presets())}")
    # 导入 + description 字段
    for p in all_presets():
        db.add_strategy(p["name"], kind=p["kind"], formula=p["formula"],
                        scan_mode=p.get("scan_mode", "manual"),
                        description=p.get("description", ""))
    all_rows = db.list_strategies()
    check("导入后数量正确", len(all_rows) == len(all_presets()))
    check("description 落库", all(d.get("description") for d in all_rows))
    inds = db.list_strategies(kind="indicator")
    check("指标库按 kind 过滤", len(inds) == 4)
    # 中文释义
    b2 = [p for p in all_presets() if p["name"] == "B2普通"][0]
    lines = explain_formula(b2["formula"])
    check("B2普通 逐条释义", len(lines) >= 10 and "基础达标" in [l["name"] for l in lines])
    check("释义包含人话关键词", any("收盘价" in l["text"] and "大于" in l["text"]
                                    for l in lines if l["name"] == "条件7_站上黄线"),
          format_explain_lines(b2["formula"])[:120])
    db.close()
    os.remove(tmp)


def test_adjust_engine():
    print("[9] 前复权引擎（合成数据，验证算法方向与口径）")
    import numpy as np
    import pandas as pd
    from core.adjust import compute_qfq_prices, filter_price_events

    # pytdx xdxr 口径：每10股数值 → 每股
    pe = filter_price_events([
        {"date": "2025-06-26", "category": 1, "fenhong": 280.24,
         "peigujia": 0, "songzhuangu": 0, "peigu": 0}])
    check("fenhong 每10股→每股", len(pe) == 1 and abs(pe[0]["fenhong"] - 28.024) < 1e-6)
    pe2 = filter_price_events([
        {"date": "1999-01-27", "category": 1, "fenhong": 0, "peigujia": 5.3,
         "songzhuangu": 0, "peigu": 3.0}])
    check("配股口径（股价不除/股数除10）",
          abs(pe2[0]["peigujia"] - 5.3) < 1e-6 and abs(pe2[0]["peigu"] - 0.3) < 1e-6)
    # 非价格事件（股本变化）被过滤
    pe3 = filter_price_events([{"date": "2025-01-01", "category": 5,
                                "fenhong": 0, "peigujia": 0, "songzhuangu": 0, "peigu": 0}])
    check("非除权事件过滤", pe3 == [])

    # 合成 30 天序列，第 15 天 10送10（rate≈0.5），验证复权后无跳空
    n = 30
    closes = 20.0 + np.arange(n) * 0.1
    closes[15:] = closes[15:] / 2.0
    df = pd.DataFrame({
        "date": [f"2025-01-{i + 1:02d}" for i in range(n)],
        "open": closes, "high": closes + 0.1, "low": closes - 0.1,
        "close": closes, "vol": 100.0, "amount": 1000.0,
    })
    events = [{"date": "2025-01-16", "category": 1, "fenhong": 0, "peigujia": 0,
               "songzhuangu": 10.0, "peigu": 0}]
    out, issues = compute_qfq_prices(df, events)
    chg = out["close"].iloc[15] / out["close"].iloc[14] - 1
    check("除权日复权后连续（无跳空）", abs(chg) < 0.01, f"chg={chg * 100:.2f}%")
    check("最新价锚定", abs(out["close"].iloc[-1] - closes[-1]) < 1e-6)
    # 无事件时原样返回
    out2, _ = compute_qfq_prices(df, [])
    check("无事件原样返回", float(out2["close"].iloc[5]) == closes[5])


def test_dna_engine():
    print("[10] DNA 特征与匹配引擎（合成数据）")
    import shutil
    import tempfile
    import numpy as np
    import pandas as pd
    from core.dna_engine import (DNAFeatureExtractor, DNAPatternMatcher,
                                 TemplateManager)

    n = 60
    seq = np.linspace(10, 20, n) + np.sin(np.arange(n) / 3.0)
    df = pd.DataFrame({
        "date": [f"2025-01-{i + 1:02d}" for i in range(n)],
        "close": seq,
        "vol": np.linspace(1000, 300, n),
    })
    fdf = DNAFeatureExtractor.calculate_indicators(df)
    check("黄白线计算", "yl" in fdf.columns and "wl" in fdf.columns)
    mat = DNAFeatureExtractor.extract_matrix(fdf)
    check("特征矩阵 Nx3", mat.shape == (n, 3))
    check("特征值域 [0,1]", float(mat.min()) >= -1e-9 and float(mat.max()) <= 1 + 1e-9)

    tmpdir = os.path.join(tempfile.gettempdir(), "ta_dna_test")
    shutil.rmtree(tmpdir, ignore_errors=True)
    tm = TemplateManager(tmpdir)
    tm.save_template("测试模具", mat, {"code": "000000", "name": "测试",
                                       "strategy": "测试", "start": "2025-01-01",
                                       "end": "2025-01-30", "len_days": n})
    tm2 = TemplateManager(tmpdir)
    check("模具存取往返", "测试模具" in tm2.templates
          and tm2.templates["测试模具"].shape == (n, 3))
    meta = tm2.metadata["测试模具"]
    check("元数据含起止与天数", meta.get("len_days") == n and meta.get("start") == "2025-01-01")

    m = DNAPatternMatcher(tm2, length_scan=False)
    r = m.match_candidate(mat, mat, dates=df["date"].astype(str).tolist())
    check("同形匹配≈100", r["score"] > 99.9, r["score"])
    check("窗口=模具长度（末端锚定）", r["best_len"] == n)

    m2 = DNAPatternMatcher(tm2, length_scan=True)
    r2 = m2.match_candidate(mat[:40], mat,
                            dates=df["date"].astype(str).tolist()[:40])
    check("长度弹性：短序列用短档位匹配", r2["score"] > 0 and r2["best_len"] <= 40,
          str(r2))

    r3 = m.match_candidate(mat * 0 + 0.5, mat, dates=df["date"].astype(str).tolist())
    check("完全不同形态低分", r3["score"] < 50, r3["score"])

    # ---- v2 维度3（支撑线贴合度）灵敏度：强势拉升后回踩白线的动作必须可见 ----
    prices = np.concatenate([
        np.linspace(10, 40, 30),                       # 强势拉升（3倍）
        [39.5, 38.8, 39.2, 38.5, 38.0, 37.5, 38.2, 38.9, 39.5, 40.2],  # 回踩
    ])
    df2 = pd.DataFrame({
        "date": [f"2026-01-{i + 1:02d}" for i in range(len(prices))],
        "close": prices,
        "vol": np.linspace(2000, 500, len(prices)),
    })
    fdf2 = DNAFeatureExtractor.calculate_indicators(df2)
    mat2 = DNAFeatureExtractor.extract_matrix(fdf2)
    d3 = mat2[:, 2]
    peak = float(d3[:20].max())
    trough = float(d3[-6:].min())
    check("v2 绿线：回踩动作可见（区分度>0.05）", peak - trough > 0.05,
          f"peak={peak:.3f} trough={trough:.3f}")
    check("v2 绿线：值域合法", float(d3.min()) >= 0 and float(d3.max()) <= 1)
    # 横盘踩线语义：0.5 = 严丝合缝踩在支撑线上
    flat = np.full(60, 20.0)
    df3 = pd.DataFrame({
        "date": [f"2026-02-{i + 1:02d}" for i in range(60)],
        "close": flat, "vol": np.full(60, 1000.0),
    })
    fdf3 = DNAFeatureExtractor.calculate_indicators(df3)
    mat3 = DNAFeatureExtractor.extract_matrix(fdf3)
    check("v2 绿线：横盘踩线≈0.5", abs(float(mat3[-1, 2]) - 0.5) < 0.05,
          float(mat3[-1, 2]))

    shutil.rmtree(tmpdir, ignore_errors=True)


def test_micro_rules():
    print("[11] 微观规则引擎（MicroRuleEvaluator）")
    import pandas as pd
    import numpy as np
    from core.micro_rules import MicroRuleEvaluator

    mre = MicroRuleEvaluator()
    # 构造确定性的多头缩量回踩场景：收盘缓涨、量能阶梯萎缩、黄白线在收盘下方
    n = 40
    close = np.linspace(10, 15, n)
    df = pd.DataFrame({
        "close": close,
        "open": close * 0.995,
        "high": close * 1.01,
        "low": close * 0.99,
        "vol": np.concatenate([np.linspace(3000, 1500, n - 5), [500, 480, 460, 450, 440]]),
    })
    fdf = df.copy()
    fdf["yl"] = np.linspace(9, 14.5, n)    # 黄线低于收盘（多头）
    fdf["wl"] = np.linspace(9.5, 14.7, n)  # 白线亦低于收盘
    r = mre.evaluate(df, fdf, None)
    check("返回结构完整",
          set(r) >= {"score_modifier", "buy_level", "signals", "detail"})
    check("修正量在 [-20,20] 内", -20 <= r["score_modifier"] <= 20, str(r["score_modifier"]))
    check("多头缩量回踩触发缩量回踩/支撑/多头信号",
          "缩量回踩" in r["signals"] and "回踩支撑不破" in r["signals"]
          and "均线多头排列" in r["signals"], str(r["signals"]))
    check("三重结构 → SB1核心买点", r["buy_level"] == "SB1核心买点", r["buy_level"])

    # 破位下行场景：收盘低于黄线白线 → 减分 + 无买点
    df2 = df.copy()
    fdf2 = fdf.copy()
    fdf2["yl"] = np.linspace(9, 16, n)
    fdf2["wl"] = np.linspace(9.5, 16.2, n)  # 收盘(15) 低于黄线(16)与白线(16.2)
    r2 = mre.evaluate(df2, fdf2, None)
    check("破位触发「跌破黄白线」减分", "跌破黄白线" in r2["signals"]
          and r2["score_modifier"] < 0, f"{r2['signals']} {r2['score_modifier']}")
    check("破位无买点定级", r2["buy_level"] == "", r2["buy_level"])

    # 空数据兜底
    r3 = mre.evaluate(None, pd.DataFrame(), None)
    check("空数据兜底", r3["score_modifier"] == 0 and r3["buy_level"] == ""
          and r3["signals"] == [], str(r3))


def test_layer2_screen():
    print("[12] 二层精筛链路（layer2_screen：DTW骨架 + 微观综合分）")
    import shutil
    import tempfile
    import pandas as pd
    import numpy as np
    from core.dna_engine import DNAFeatureExtractor, TemplateManager
    from core.micro_rules import MicroRuleEvaluator
    from scanner import ScanResult, layer2_screen
    from database import Database

    n = 60
    rng = np.random.default_rng(3)
    seq = np.linspace(10, 20, n) + np.sin(np.arange(n) / 3.0) * 0.4
    bars = pd.DataFrame({
        "date": [f"2026-03-{i % 28 + 1:02d}" for i in range(n)],
        "open": seq, "high": seq + 0.3, "low": seq - 0.3,
        "close": seq,
        "vol": np.linspace(2000, 400, n),
        "amount": np.linspace(2e8, 4e7, n),
    })
    fdf = DNAFeatureExtractor.calculate_indicators(bars)
    feat = DNAFeatureExtractor.extract_matrix(fdf)

    # 临时模具库：与合成序列同形的模具
    tmpdir = os.path.join(tempfile.gettempdir(), "ta_layer2_test")
    shutil.rmtree(tmpdir, ignore_errors=True)
    tm = TemplateManager(tmpdir)
    tm.save_template("测试模具", feat, {"code": "000000", "name": "测试",
                                        "strategy": "测试", "start": "2026-03-01",
                                        "end": "2026-03-30", "len_days": n})

    class StubFeed:
        """离线行情源：对指定代码返回合成日K。"""
        def __init__(self, bars_map):
            self.bars_map = bars_map
            self.db = None
        def get_daily_bars(self, code, market=None, count=300):
            b = self.bars_map.get(str(code))
            return b.tail(count).reset_index(drop=True) if b is not None else None

    # 候选1：与模具同形（高分）；候选2：随机游走（低分）
    flat = pd.DataFrame({
        "date": [f"2026-03-{i % 28 + 1:02d}" for i in range(n)],
        "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0,
        "vol": 1000.0, "amount": 1e8,
    })
    feed = StubFeed({"000001": bars, "000002": flat})
    candidates = [{"code": "000001", "name": "同形", "market": 0},
                  {"code": "000002", "name": "横盘", "market": 0}]

    hits = layer2_screen(feed, candidates, ["测试模具"], threshold=0, top_n=None,
                         length_scan=False, template_manager=tm)
    check("二层精筛返回 2 只", len(hits) == 2, str(len(hits)))
    by_code = {h.code: h for h in hits}
    check("同形候选综合分最高", by_code["000001"].final_score >= by_code["000002"].final_score,
          f"{by_code['000001'].final_score} vs {by_code['000002'].final_score}")
    # 综合分 = clip(骨架 + 微观修正, 0, 100)
    fdf1 = DNAFeatureExtractor.calculate_indicators(bars)
    mod = MicroRuleEvaluator().evaluate(bars, fdf1)["score_modifier"]
    expect = float(np.clip(by_code["000001"].dna_score + mod, 0, 100))
    check("综合分=clip(骨架+修正,0,100)", abs(by_code["000001"].final_score - expect) < 1e-6,
          f"final={by_code['000001'].final_score} expect={expect}")
    check("骨架分/模具/买点字段填充",
          by_code["000001"].dna_score > 0 and by_code["000001"].dna_template == "测试模具"
          and isinstance(by_code["000001"].buy_level, str)
          and isinstance(by_code["000001"].signals, str), str(vars(by_code["000001"])))

    # 阈值 + TopN 兜底：threshold=9999（全不达标）→ top_n=1 兜底取最高分
    hits2 = layer2_screen(feed, candidates, ["测试模具"], threshold=9999, top_n=1,
                          length_scan=False, template_manager=tm)
    check("Top15 兜底逻辑：阈值不达标时取前 TopN", len(hits2) == 1
          and hits2[0].code == "000001", f"{[(h.code, h.final_score) for h in hits2]}")

    # 持久化：layer=2 写入后字段齐全（无 as_of_date → trade_date 为空，按会话查）
    tmpdb = os.path.join(tempfile.gettempdir(), "ta_layer2_db.db")
    if os.path.exists(tmpdb):
        os.remove(tmpdb)
    db = Database(tmpdb)
    db.add_scan_hits("l2sess", None, "自动二层精筛", hits, layer=2)
    rows = [r for r in db.list_scan_history(limit=1000) if r["session_id"] == "l2sess"]
    check("layer2 落库 2 条", len(rows) == 2, str(len(rows)))
    r0 = rows[0]
    check("layer2 落库字段齐全（dtw/final/模具/买点/信号/in_watchlist/trade_date）",
          r0.get("layer") == 2 and r0.get("dtw_score") is not None
          and r0.get("final_score") is not None and r0.get("matched_template")
          and r0.get("buy_level") is not None and r0.get("in_watchlist") is not None,
          str({k: r0.get(k) for k in ("layer", "dtw_score", "final_score",
                                      "matched_template", "buy_level",
                                      "in_watchlist")}))
    # in_watchlist 联动：入池后标记
    db.add_watch(code="000001", name="同形", market=0, reason="t")
    db.update_scan_in_watchlist("000001", flag=1)
    row1 = [r for r in db.list_scan_history(limit=1000)
            if r["code"] == "000001"][0]
    check("update_scan_in_watchlist 回写", row1["in_watchlist"] == 1,
          str(row1["in_watchlist"]))
    # trade_date 有值时按信号日过滤（COALESCE 优先）
    db.add_scan_hits("l2sess2", None, "自动二层精筛", hits, layer=2)
    from scanner import ScanResult as _SR
    # 手工构造带 signal_date 的记录验证 trade_date 落库
    h2 = _SR(code="000003", name="带日期", market=0, dna_score=80.0,
             dna_template="T", final_score=82.0, buy_level="B1底仓买点",
             signals="缩量回踩", signal_date="2026-03-15")
    db.add_scan_hits("l2sess3", None, "自动二层精筛", [h2], layer=2)
    row_d = db.list_scan_history(day="2026-03-15", limit=100)
    check("trade_date 按信号日可查（COALESCE）", any(r["code"] == "000003" for r in row_d),
          str([(r["code"], r.get("trade_date")) for r in row_d[:5]]))
    db.close()
    os.remove(tmpdb)
    shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    print("========== 无头集成测试 ==========")
    feed = DataFeed()
    bars = feed.get_daily_bars("600000", market=1, count=300)
    feed.disconnect()
    if bars is None or len(bars) < 100:
        print("  无法获取足够的行情数据，测试中止（请检查网络）")
        sys.exit(2)
    df = compute_indicators(bars)
    check("指标计算（MACD/KDJ/RSI/BOLL/ATR）",
          all(c in df.columns for c in ["MACD_DIF", "MACD_DEA", "KDJ_K", "KDJ_D",
                                        "RSI6", "BOLL_UPPER", "ATR14"]))
    snap = snapshot_indicators(bars)
    check("因子快照", len(snap) > 10)

    test_formula_parser(df)
    test_database()
    test_alert_engine()
    test_data_layer()
    test_scan()
    test_new_db_features()
    test_formula_script()
    test_preset_library()
    test_adjust_engine()
    test_dna_engine()
    test_micro_rules()
    test_layer2_screen()
    test_candlestick_item()
    test_chart_box_select_stats()

    print(f"\n========== 结果：{PASS} 通过 / {FAIL} 失败 ==========")
    sys.exit(1 if FAIL else 0)


def test_candlestick_item():
    """v0.7.7：K线图修复 —— 实体宽度自适应 + hover 索引。"""
    print("\n[13] K线图 CandlestickItem（v0.7.7：实体宽度/hover/框选）")
    try:
        from PyQt5.QtCore import QPointF
    except Exception:
        print("  跳过（PyQt5 未安装）")
        return
    from PyQt5.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])

    from ui.chart_widget import CandlestickItem, BODY_PX, BODY_MIN_PX

    data = np.array([
        [0, 10.0, 10.5, 9.5, 10.2],   # 阳
        [1, 10.2, 10.8, 10.1, 10.7],  # 阳
        [2, 10.7, 10.8, 10.6, 10.7],  # 十字星（开=收）
        [3, 10.7, 10.8, 10.5, 10.55], # 阴
        [4, 10.55, 10.6, 10.3, 10.4], # 阴
    ])
    item = CandlestickItem(data, date_labels=["d0", "d1", "d2", "d3", "d4"],
                           name="测试股", code="000000")
    check("CandlestickItem 构造", item is not None)
    check("BODY_PX/BODY_MIN_PX 常量存在", BODY_PX > 0 and BODY_MIN_PX > 0)
    check("indexAt(0.4) = 0", item.indexAt(QPointF(0.4, 10.0)) == 0)
    check("indexAt(2.6) = 3", item.indexAt(QPointF(2.6, 10.0)) == 3)
    check("indexAt(2.0) = 2", item.indexAt(QPointF(2.0, 10.0)) == 2)
    br = item.boundingRect()
    check("boundingRect 包含所有 X", br.left() <= 0 and br.right() >= 4)
    check("boundingRect 包含最高", br.top() <= 10.8)
    check("boundingRect 包含最低", br.bottom() >= 9.5)
    check("_body_width_data 返回正数", item._body_width_data(None) > 0)
    check("_min_body_height_data 返回正数", item._min_body_height_data(None) > 0)

    # 验证 v0.7.7 重构后的动态宽度逻辑：实体总宽 = clamp(间距×70%, 下限2px, 上限12px)
    # 模拟"1 数据单位 = 0.5 像素"（缩小 2 倍）：spacing_px = 0.5
    #   spacing=0.5 < 3 → 不强制 min 2px，total = min(0.35, 12) = 0.35px → w_data = 0.7
    class _FakeTransform:
        def map(self, pt):
            from PyQt5.QtCore import QPointF as _QPF
            return _QPF(pt.x() * 0.5, pt.y() * 0.5)
    class _FakePainter:
        def combinedTransform(self):
            return _FakeTransform()
    fp = _FakePainter()
    w_data = item._body_width_data(fp)
    h_data = item._min_body_height_data(fp)
    # v0.8.0：动态宽度，密集场景优先不重叠
    check("宽度换算正确（密集场景：spacing=0.5px, 总宽=0.35px → w_data=0.35）",
          abs(w_data - 0.35) < 1e-3)
    check("高度换算正确（1px = 2 数据单位）",
          abs(h_data - 2.0) < 1e-3)
    flat = np.array([[i, 10.0, 10.0, 10.0, 10.0] for i in range(5)])
    flat_item = CandlestickItem(flat, date_labels=[f"d{i}" for i in range(5)])
    check("纯十字星 CandlestickItem 构造不抛错", flat_item is not None)
    check("OHLC 缓存可读", item._opens[2] == 10.7 and item._closes[2] == 10.7)


def test_chart_box_select_stats():
    """v0.7.7：右键框选统计逻辑。"""
    print("\n[14] K线图右键框选统计")
    try:
        from PyQt5.QtWidgets import QApplication
    except Exception:
        print("  跳过（PyQt5 未安装）")
        return
    _app = QApplication.instance() or QApplication([])
    from ui.chart_widget import ChartDialog
    import pandas as pd
    dlg = ChartDialog("000000", "测试股", market=0, db=None)
    dlg._df = pd.DataFrame({
        "date": [f"2026-01-0{i}" for i in range(1, 6)] + ["2026-01-06"],
        "open":  [10.0, 10.2, 10.7, 10.7, 10.55, 10.4],
        "high":  [10.5, 10.8, 10.8, 10.8, 10.6, 10.5],
        "low":   [9.5, 10.1, 10.6, 10.5, 10.3, 10.3],
        "close": [10.2, 10.7, 10.7, 10.55, 10.4, 10.6],
        "vol":   [1000, 1200, 800, 900, 700, 1100],
        "amount":[10000, 12000, 8000, 9000, 7000, 11000],
    })
    dlg._show_box_summary(1.0, 4.0)
    check("_show_box_summary 不抛错", True)


if __name__ == "__main__":
    main()
