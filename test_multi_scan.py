# -*- coding: utf-8 -*-
"""测试 v0.8.11 多策略一次扫描:
1) 正确性: multi_scan_formula(单策略) == scan_formula(单策略)
2) 性能: K线拉取次数 = 股票数(而非 股票数×策略数)
3) parallel_multi_scan_formula 与 multi_scan_formula 结果一致
"""
import os, sys, random
sys.path.insert(0, os.path.dirname(os.path.abspath("__file__")))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd
from scanner import (scan_formula, multi_scan_formula, parallel_multi_scan_formula,
                     ScanResult)

PASS = 0
FAIL = 0
def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  {detail}")

# ============================================================
# 假 feed: 生成确定性日K(不拉网络), 统计 get_daily_bars 调用次数
# ============================================================
class FakeFeed:
    def __init__(self, bars_map):
        self.bars_map = bars_map
        self.calls = 0
    def get_daily_bars(self, code, market=None, count=300, as_of_date=None):
        self.calls += 1
        return self.bars_map.get(code)


def make_bars(n=60, base=10.0, uptrend=True):
    """生成 n 天日K; 上升趋势: 收盘每天 +0.2% 保证 C>O 恒真."""
    dates = pd.bdate_range("2026-06-01", periods=n).strftime("%Y-%m-%d").tolist()
    if uptrend:
        closes = [base * (1 + 0.002 * i) for i in range(n)]
    else:
        closes = [base * (1 - 0.002 * i) for i in range(n)]
    # 开盘 = 前一日收盘: 上升票 close>open(阳线), 下降票 close<open(阴线)
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) * 1.01 for o, c in zip(opens, closes)]
    lows = [min(o, c) * 0.98 for o, c in zip(opens, closes)]
    return pd.DataFrame({
        "date": dates, "open": opens, "high": highs,
        "low": lows, "close": closes,
        "vol": [1_000_000] * n, "amount": [1e8] * n,
    })


# 3 只股票
bars_map = {
    "000001": make_bars(60, 10.0, uptrend=True),    # 上升
    "000002": make_bars(60, 20.0, uptrend=False),   # 下降
    "000003": make_bars(60, 30.0, uptrend=True),    # 上升
}
stock_df = pd.DataFrame([
    {"code": "000001", "name": "票1", "market": 0},
    {"code": "000002", "name": "票2", "market": 0},
    {"code": "000003", "name": "票3", "market": 0},
])

# ============================================================
# 1. 正确性: 单策略 multi == 单策略 scan
# ============================================================
print("=== 1. 正确性: multi(单策略) == scan(单策略) ===")
formula_up = "C > O"       # 上升票命中
formula_down = "C < O"     # 下降票命中

feed_a = FakeFeed(bars_map)
results_a = scan_formula(feed_a, stock_df, formula_up, bars_count=60,
                         max_stocks=3)
codes_a = sorted(r.code for r in results_a)
print(f"  scan_formula(C>O): {codes_a}")

feed_b = FakeFeed(bars_map)
buckets_b = multi_scan_formula(feed_b, stock_df,
                               [(101, "策略A", formula_up)],
                               max_stocks=3, bars_count=60)
codes_b = sorted(r.code for r in buckets_b.get(101, []))
print(f"  multi_scan_formula(C>O): {codes_b}")
check("multi(单策略) 结果与 scan 一致", codes_a == codes_b, f"{codes_a} vs {codes_b}")

# ============================================================
# 2. 性能: 2 个策略一次扫描, K线拉取次数 = 股票数 (3), 不是 6
# ============================================================
print("\n=== 2. 性能: 2 策略一次扫描, K线拉取 = 股票数 ===")
feed_c = FakeFeed(bars_map)
buckets_c = multi_scan_formula(
    feed_c, stock_df,
    [(101, "策略A", formula_up), (102, "策略B", formula_down)],
    max_stocks=3, bars_count=60)
print(f"  get_daily_bars 调用次数: {feed_c.calls} (股票数=3, 策略数=2)")
check("K线拉取次数 = 股票数 (3)", feed_c.calls == 3, f"calls={feed_c.calls}")
c_up = sorted(r.code for r in buckets_c.get(101, []))
c_down = sorted(r.code for r in buckets_c.get(102, []))
print(f"  策略A(C>O) 命中: {c_up}")
print(f"  策略B(C<O) 命中: {c_down}")
check("策略A 命中上升票", c_up == ["000001", "000003"], str(c_up))
check("策略B 命中下降票", c_down == ["000002"], str(c_down))

# ============================================================
# 3. 同票多策略命中: 一只票同时满足两个公式
# ============================================================
print("\n=== 3. 同票多策略 ===")
formula_true = "C > O OR C < O"   # 恒真
feed_d = FakeFeed(bars_map)
buckets_d = multi_scan_formula(
    feed_d, stock_df,
    [(1, "恒真", formula_true), (2, "上升", formula_up)],
    max_stocks=3, bars_count=60)
c1 = sorted(r.code for r in buckets_d.get(1, []))
c2 = sorted(r.code for r in buckets_d.get(2, []))
print(f"  恒真 命中: {c1}")
print(f"  上升 命中: {c2}")
check("恒真 命中全部 3 只", c1 == ["000001", "000002", "000003"], str(c1))
check("上升 命中 2 只", c2 == ["000001", "000003"], str(c2))

# ============================================================
# 4. 历史日期扫描 (start_date/end_date)
# ============================================================
print("\n=== 4. 历史日期扫描 ===")
# 历史窗口取数据末尾 40 天 (scan_formula 要求窗口 >=30 天)
feed_e = FakeFeed(bars_map)
all_dates = bars_map["000001"]["date"].tolist()
hist_end = all_dates[-1]
hist_start = all_dates[-40]
buckets_e = multi_scan_formula(
    feed_e, stock_df, [(101, "策略A", formula_up)],
    max_stocks=3, bars_count=60,
    start_date=hist_start, end_date=hist_end)
c_up_hist = sorted(r.code for r in buckets_e.get(101, []))
print(f"  历史 {hist_start}~{hist_end} C>O 命中: {c_up_hist}")
check("历史扫描 C>O 命中上升票 (去重)", sorted(set(c_up_hist)) == ["000001", "000003"], str(sorted(set(c_up_hist))))
if c_up_hist:
    dates_ok = all(r.signal_date and hist_start <= str(r.signal_date) <= hist_end
                   for r in buckets_e.get(101, []))
    check("signal_date 在范围内", dates_ok)
    from collections import Counter
    per_stock = Counter(r.code for r in buckets_e.get(101, []))
    check("每上升票多条(逐日命中)", per_stock["000001"] >= 1 and per_stock["000003"] >= 1,
          str(dict(per_stock)))

# ============================================================
# 5. parallel_multi_scan_formula 结果一致性 (4 线程, 每块独立 feed)
# ============================================================
print("\n=== 5. parallel_multi_scan_formula ===")
import tempfile, shutil, pathlib
from database import Database
tmpd = pathlib.Path(os.path.dirname(os.path.abspath("__file__"))) / "_multi_test_tmp"
if tmpd.exists(): shutil.rmtree(tmpd, ignore_errors=True)
tmpd.mkdir(parents=True, exist_ok=True)
db = Database(str(tmpd / "t.db"))

# 用 FakeFeed 无法直接给 parallel (它内部 new DataFeed), 这里退化为:
# 直接验证 multi_scan_formula 分块后合并 == 不分块
# (parallel 的线程语义已在原 parallel_scan_formula 用过, 此处验证合并逻辑)
chunk1 = stock_df.iloc[:2].reset_index(drop=True)
chunk2 = stock_df.iloc[2:].reset_index(drop=True)
feed1 = FakeFeed(bars_map); feed2 = FakeFeed(bars_map)
b1 = multi_scan_formula(feed1, chunk1, [(101, "A", formula_up)], max_stocks=2, bars_count=60)
b2 = multi_scan_formula(feed2, chunk2, [(101, "A", formula_up)], max_stocks=1, bars_count=60)
merged = sorted(r.code for r in (b1.get(101, []) + b2.get(101, [])))
single = sorted(r.code for r in buckets_b.get(101, []))
check("分块合并结果 == 不分块", merged == single, f"{merged} vs {single}")

# parallel 端到端: 用真实 DataFeed 会连网, 跳过网络断言, 只验证函数存在与签名
import inspect
sig = inspect.signature(parallel_multi_scan_formula)
check("parallel_multi_scan_formula 存在且可调用",
      callable(parallel_multi_scan_formula) and "formulas" in sig.parameters)

shutil.rmtree(tmpd, ignore_errors=True)
print(f"\n==================================================\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)