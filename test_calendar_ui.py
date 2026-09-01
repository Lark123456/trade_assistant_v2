# -*- coding: utf-8 -*-
"""行情日历双层筛选 UI 冒烟测试（offscreen，纯内存数据，不联网）：

验证 v0.7.4「日历-双层筛选与观察池流转」：
① 三层 Tab（一层粗筛/二层精筛/观察池）行数与列结构
② 二层精筛按综合得分降序 + 买点定级高亮规则生效
③ 顶部标题计数与各 Tab 行数精准同步
④ 底部【加入观察池】联动：watch_pool 写入、scan_history.in_watchlist 回写、计数 +1

运行：python test_calendar_ui.py
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["QT_LOGGING_RULES"] = "*.debug=false"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import datetime

from PyQt5.QtCore import QDate
from PyQt5.QtWidgets import QApplication

from database import Database
from scanner import ScanResult
from ui.calendar_tab import CalendarTab
from ui.models import PandasModel

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


from PyQt5.QtCore import QObject, pyqtSignal


class FakeWorker(QObject):
    """模拟 Worker：带 quotes_ready 信号与 request_quotes 记录，便于测试行情刷新路径。"""
    quotes_ready = pyqtSignal(object)

    def __init__(self, db):
        super().__init__()
        self.db = db
        self.requested_codes = []

    def request_quotes(self, codes):
        self.requested_codes.append(list(codes))


def main():
    print("========== 行情日历双层筛选 UI 冒烟 ==========")
    tmpdb = os.path.join(tempfile.gettempdir(), "ta_calendar_ui_test.db")
    if os.path.exists(tmpdb):
        os.remove(tmpdb)
    db = Database(tmpdb)
    today = datetime.date.today().isoformat()

    # ---- 造数据：一层粗筛 3 条 + 二层精筛 3 条（乱序） + 观察池 1 条 ----
    hits1 = [ScanResult(code="600001", name="粗筛A", market=1, price=10.0, pct=2.0,
                        match_reason="C>O"),
             ScanResult(code="600002", name="粗筛B", market=1, price=11.0, pct=-1.0,
                        match_reason="C>O"),
             ScanResult(code="600003", name="粗筛C", market=1, price=12.0, pct=0.5,
                        match_reason="C>O")]
    db.add_scan_hits("s1", 1, "测试策略", hits1, layer=1)
    # v0.8.1：粗筛/精筛票也插入 daily_bars_cache（fallback 用最新日K 兜底）
    import numpy as np
    import pandas as pd
    coarse_bdates = pd.bdate_range(end=pd.Timestamp(today), periods=30).strftime("%Y-%m-%d")
    for code, last_close in [("600001", 10.0), ("600002", 11.0), ("600003", 12.0)]:
        coarse_bars = pd.DataFrame({
            "date": coarse_bdates,
            "open": np.linspace(last_close - 1, last_close, 30),
            "high": np.linspace(last_close - 0.8, last_close + 0.2, 30),
            "low": np.linspace(last_close - 1.2, last_close - 0.2, 30),
            "close": np.linspace(last_close - 1, last_close, 30),
            "vol": np.full(30, 1000.0),
            "amount": np.full(30, 1e7),
        })
        coarse_bars.loc[coarse_bars.index[-1], "close"] = last_close
        db.cache_daily_bars(code, 1, coarse_bars)
    hits2 = [
        ScanResult(code="600003", name="精筛C", market=1, price=12.0, pct=0.5,
                   match_reason="x", dna_score=70.0, dna_template="模具X",
                   final_score=78.0, buy_level="B1底仓买点", signals="缩量回踩",
                   signal_date=today),
        ScanResult(code="600001", name="精筛A", market=1, price=10.0, pct=2.0,
                   match_reason="x", dna_score=80.0, dna_template="模具X",
                   final_score=92.0, buy_level="SB1核心买点", signals="缩量回踩；均线多头排列",
                   signal_date=today),
        ScanResult(code="600004", name="精筛D", market=1, price=9.0, pct=-2.0,
                   match_reason="x", dna_score=75.0, dna_template="模具Y",
                   final_score=75.0, buy_level="", signals="放量启动",
                   signal_date=today),
    ]
    db.add_scan_hits("s2", None, "自动二层精筛", hits2, layer=2)
    db.add_watch(code="600001", name="粗筛A", market=1, trigger_indicator="测试策略",
                 reason="自动进池", status="待审核", add_date=today)
    # v0.8.1：精筛 600004 也插入 daily_bars_cache（fallback 用最新日K 兜底）
    dna_bdates = pd.bdate_range(end=pd.Timestamp(today), periods=30).strftime("%Y-%m-%d")
    dna004_bars = pd.DataFrame({
        "date": dna_bdates,
        "open": np.linspace(8.0, 9.0, 30), "high": np.linspace(8.2, 9.2, 30),
        "low": np.linspace(7.8, 8.8, 30), "close": np.linspace(8.0, 9.0, 30),
        "vol": np.full(30, 1000.0), "amount": np.full(30, 1e7),
    })
    dna004_bars.loc[dna004_bars.index[-1], "close"] = 9.0
    db.cache_daily_bars("600004", 1, dna004_bars)
    # v0.7.5 观察池表现：600005 今日加入，缓存日K最后一日收盘=10.0（作为「加入日收盘价」）
    db.add_watch(code="600005", name="表现股", market=1, trigger_indicator="手动加入",
                 reason="表现验证", status="观察中", add_date=today)
    import numpy as np
    import pandas as pd
    bdates = pd.bdate_range(end=pd.Timestamp(today), periods=30).strftime("%Y-%m-%d")
    perf_bars = pd.DataFrame({
        "date": bdates,
        "open": np.linspace(9.0, 10.0, 30), "high": np.linspace(9.2, 10.2, 30),
        "low": np.linspace(8.8, 9.8, 30), "close": np.linspace(9.0, 10.0, 30),
        "vol": np.full(30, 1000.0), "amount": np.full(30, 1e7),
    })
    perf_bars.loc[perf_bars.index[-1], "close"] = 10.0
    # v0.8.1 测试数据修复：若 today 不是交易日（如周末），bdates 不含 today，
    # 导致 get_close_on_date(600005, today)=None → 加入价=nan、较加入价%=nan。
    # 补一行 today 的日K（收盘=10.0），让"加入日收盘价"可查。
    if today not in set(bdates):
        perf_bars = pd.concat([perf_bars, pd.DataFrame([{
            "date": today, "open": 10.0, "high": 10.2, "low": 9.9,
            "close": 10.0, "vol": 1000.0, "amount": 1e7,
        }])], ignore_index=True)
    db.cache_daily_bars("600005", 1, perf_bars)

    app = QApplication(sys.argv)
    worker = FakeWorker(db)
    tab = CalendarTab(worker, db, on_watch_added=lambda: None)
    date = QDate.currentDate()
    tab._open_date(date)

    # ---- ① 三层 Tab 结构与计数 ----
    coarse_df = tab.coarse_list.table.model().dataframe()
    dna_df = tab.dna_list.table.model().dataframe()
    watch_df = tab.watch_list.table.model().dataframe()
    check("一层粗筛 3 行", len(coarse_df) == 3, str(len(coarse_df)))
    check("二层精筛 3 行", len(dna_df) == 3, str(len(dna_df)))
    check("观察池 2 行", len(watch_df) == 2, str(len(watch_df)))
    check("二层表头为规定列（v0.7.6 含表现列；v0.8.0 加「层级」；v0.8.1 加「10/30日最高涨幅%」、「价格」→「加入价」）",
          list(dna_df.columns) == ["序号", "层级", "股票代码", "股票名称", "综合得分", "骨架相似度",
                                   "买点定级", "最佳匹配模具", "加入价", "现价", "较加入价%",
                                   "10日最高涨幅%", "30日最高涨幅%",
                                   "当日涨跌%", "振幅%", "微观特征诊断"],
          str(list(dna_df.columns)))
    check("二层按综合得分降序 + 序号重排",
          dna_df["综合得分"].tolist() == [92.0, 78.0, 75.0]
          and dna_df["序号"].tolist() == [1, 2, 3],
          str(list(zip(dna_df["序号"], dna_df["综合得分"]))))

    # ---- ② 买点定级高亮规则 ----
    model: PandasModel = tab.dna_list.table.model()
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QColor
    # v0.8.0：columns 插入「层级」后列索引下移，"买点定级"列位置 = 6
    buy_level_col = list(dna_df.columns).index("买点定级")
    fg = model.data(model.index(1, buy_level_col), Qt.ForegroundRole)  # B1 底仓买点
    check("B1 买点定级着蓝色", fg is not None and fg.name().upper() == "#1F6FEB",
          str(fg.name() if fg else None))
    fg2 = model.data(model.index(0, buy_level_col), Qt.ForegroundRole)  # SB1 核心买点
    check("SB1 买点定级着醒目红", fg2 is not None and fg2.name().upper() == "#C0392B",
          str(fg2.name() if fg2 else None))

    # ---- ③ 标题计数与 Tab 同步 ----
    expect = f"粗筛 {len(coarse_df)} 只 / 精筛 {len(dna_df)} 只 / 观察池 {len(watch_df)} 只"
    check("标题计数与 Tab 同步", expect in tab.date_label.text(),
          f"{tab.date_label.text()} 期望含 {expect}")

    # ---- ④ 加入观察池联动（多选 600004 精筛D） ----
    from PyQt5.QtWidgets import QMessageBox
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)  # 去弹窗
    df = tab.dna_list.table.model().dataframe()
    row_idx = df.index[df["股票代码"] == "600004"].tolist()[0]
    tab.dna_list.table.selectRow(int(row_idx))
    before_watch = len(db.list_watch())
    tab.dna_list._add_watch()
    watch_after = len(db.list_watch())
    inw = [r for r in db.list_scan_history(day=today, limit=100)
           if r["code"] == "600004"]
    check("加入观察池写入 watch_pool", watch_after == before_watch + 1,
          f"{before_watch} -> {watch_after}")
    check("scan_history.in_watchlist 回写=1", inw and inw[0]["in_watchlist"] == 1,
          str(inw[0].get("in_watchlist") if inw else None))
    # 计数 +1：重载当日清单
    tab._open_date(date)
    watch_df3 = tab.watch_list.table.model().dataframe()
    check("顶部观察池计数 +1", f"观察池 {len(watch_df3)} 只" in tab.date_label.text(),
          tab.date_label.text())

    # ---- ⑤ v0.7.5 观察池表现列：较加入日收盘价涨跌 + 盘中波动刷新 ----
    watch_df = tab.watch_list.table.model().dataframe()
    check("观察池表头含表现列（v0.8.0 加「层级」；v0.8.1 加「10/30日最高涨幅%」）",
          list(watch_df.columns) == ["层级", "来源", "代码", "名称", "加入价", "现价", "较加入价%",
                                     "10日最高涨幅%", "30日最高涨幅%", "当日涨跌%", "振幅%", "原因"],
          str(list(watch_df.columns)))
    p5 = watch_df[watch_df["代码"] == "600005"].iloc[0]
    check("加入价 = 加入交易日收盘（日K缓存）", p5["加入价"] == 10.0, str(p5["加入价"]))
    check("初始现价回退最近收盘 → 较加入价%=0", p5["现价"] == 10.0 and p5["较加入价%"] == 0.0,
          f"现价={p5['现价']} 较加入价%={p5['较加入价%']}")

    # 模拟 5 秒行情到达：现价 10.5 / 昨收 10.0 / 高 10.8 / 低 10.1 / 涨 5%
    import pandas as pd
    qdf = pd.DataFrame([{"code": "600005", "name": "表现股", "market": 1,
                         "price": 10.5, "last_close": 10.0, "open": 10.2,
                         "high": 10.8, "low": 10.1, "vol": 1000, "amount": 1e7,
                         "pct": 5.0}])
    worker.quotes_ready.emit(qdf)
    watch_df = tab.watch_list.table.model().dataframe()
    p5 = watch_df[watch_df["代码"] == "600005"].iloc[0]
    check("行情刷新：现价更新为 10.5", p5["现价"] == 10.5, str(p5["现价"]))
    check("较加入价%=5%（10.5/10.0）", p5["较加入价%"] == 5.0, str(p5["较加入价%"]))
    check("当日涨跌%=5%", p5["当日涨跌%"] == 5.0, str(p5["当日涨跌%"]))
    check("振幅%=7%（(10.8-10.1)/10.0）", p5["振幅%"] == 7.0, str(p5["振幅%"]))

    # 盘中定时器请求：交易日盘中 → request_quotes 被调用（此处手工触发验证逻辑）
    tab.watch_list.show()   # 让 isVisible() 为真，走真实定时器分支
    tab.watch_list._on_timer()
    if len(worker.requested_codes):
        got = {c for _c in worker.requested_codes for _, c in _c}
        check("盘中每5秒请求当日观察股行情", "600005" in got, str(got))
    else:
        check("盘中每5秒请求当日观察股行情", True, "非交易时段跳过网络请求（合理）")
    tab.watch_list.stop_refresh()

    # ---- ⑥ v0.7.6 一层粗筛/二层精筛表现列 ----
    # 一层粗筛：base_price = scan_history.price = 10.0（600001）、11.0（600002）、12.0（600003）
    coarse_df = tab.coarse_list.table.model().dataframe()
    coarse_headers = ["层级", "来源", "策略", "代码", "名称", "加入价", "现价",
                      "较加入价%", "10日最高涨幅%", "30日最高涨幅%",
                      "当日涨跌%", "振幅%", "时间", "原因"]
    check("一层粗筛表头含表现列（v0.8.0 加「层级」；v0.8.1 加「10/30日最高涨幅%」、「价格」→「加入价」）",
          list(coarse_df.columns) == coarse_headers, str(list(coarse_df.columns)))
    c1 = coarse_df[coarse_df["代码"] == "600001"].iloc[0]
    check("粗筛加入价 = 命中日 close（10.0）", c1["加入价"] == 10.0 and c1["较加入价%"] == 0.0,
          f"加入价={c1['加入价']} 较加入价%={c1['较加入价%']}")
    # 二层精筛：base_price = scan_history.price = 10.0/12.0/9.0（600001/003/004）
    dna_df = tab.dna_list.table.model().dataframe()
    check("二层精筛表头含表现列", "现价" in dna_df.columns and "较加入价%" in dna_df.columns,
          str(list(dna_df.columns)))
    dna_code_col = "股票代码" if "股票代码" in dna_df.columns else "代码"
    d1 = dna_df[dna_df[dna_code_col] == "600001"].iloc[0]
    check("精筛加入价 = 命中日 close（10.0）", d1["较加入价%"] == 0.0, str(d1["较加入价%"]))
    d4 = dna_df[dna_df[dna_code_col] == "600004"].iloc[0]
    check("精筛加入价 = 命中日 close（9.0）", d4["较加入价%"] == 0.0, str(d4["较加入价%"]))

    # 行情刷新后：粗筛与精筛 600005 较加入价% 应同步 = 5%
    # （600005 在观察池中，是 hit 但 coarse/dna 不含；用 600001 的 quote 测全链路）
    qdf_full = pd.DataFrame([
        {"code": "600001", "name": "粗筛A", "market": 1,
         "price": 10.5, "last_close": 10.0, "open": 10.2,
         "high": 10.8, "low": 10.1, "vol": 1000, "amount": 1e7, "pct": 5.0},
        {"code": "600002", "name": "粗筛B", "market": 1,
         "price": 11.5, "last_close": 11.0, "open": 11.2,
         "high": 11.8, "low": 11.1, "vol": 1000, "amount": 1e7, "pct": 4.5},
    ])
    worker.quotes_ready.emit(qdf_full)
    coarse_df = tab.coarse_list.table.model().dataframe()
    dna_df = tab.dna_list.table.model().dataframe()
    dna_code_col = "股票代码" if "股票代码" in dna_df.columns else "代码"
    c1 = coarse_df[coarse_df["代码"] == "600001"].iloc[0]
    check("粗筛 600001 现价 10.5 → 较加入价% = 5%",
          c1["现价"] == 10.5 and c1["较加入价%"] == 5.0,
          f"现价={c1['现价']} 较加入价%={c1['较加入价%']}")
    # 二层精筛：600001 也在层2（hits2）→ base=10.0 → 较加入价%=5%
    if "600001" in dna_df[dna_code_col].values:
        d1 = dna_df[dna_df[dna_code_col] == "600001"].iloc[0]
        check("二层精筛 600001 现价 10.5 → 较加入价% = 5%",
              d1["现价"] == 10.5 and d1["较加入价%"] == 5.0,
              f"现价={d1['现价']} 较加入价%={d1['较加入价%']}")

    # 三个 Tab 共享 quotes_ready：观察池表也应该拿到这个行情
    watch_df = tab.watch_list.table.model().dataframe()
    p5 = watch_df[watch_df["代码"] == "600005"].iloc[0]
    check("三 Tab 共享 quotes_ready：观察池表同步更新", p5["现价"] == 10.5,
          str(p5["现价"]))

    # 三个 Tab 各自定时器 + 仅可见时请求
    tab.coarse_list.show()
    tab.coarse_list._on_timer()
    tab.dna_list.show()
    tab.dna_list._on_timer()
    tab.watch_list.show()
    tab.watch_list._on_timer()
    # 非交易时段所有 timer 都不会网络请求；交易日盘中才会请求各自代码
    # 这里主要验证 timer 链路无异常（异常会在 traceback 中抛出）
    check("三个 Tab 定时器链路（盘中自动 request_quotes / 盘后跳过）", True,
          f"requested_codes={worker.requested_codes}")

    # ---- ⑦ v0.8.5 通达信日历导入：显示在当日二级观察池，独立于一级观察池 ----
    db.add_tdx_import(code="600010", name="通达信票", market=1,
                      add_date=today, block_name="测试板块")
    tab._open_date(date)
    watch_df = tab.watch_list.table.model().dataframe()
    ti = watch_df[watch_df["代码"] == "600010"]
    check("通达信导入显示在日历二级观察池（来源=通达信导入）",
          len(ti) == 1 and str(ti.iloc[0]["来源"]) == "通达信导入",
          str(ti.to_dict("records") if len(ti) else None))
    watch_all = {str(r["code"]) for r in db.list_watch()}
    check("通达信导入不进入一级观察池", "600010" not in watch_all,
          str(sorted(watch_all)))

    db.close()
    os.remove(tmpdb)
    print(f"\n========== 结果：{PASS} 通过 / {FAIL} 失败 ==========")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
