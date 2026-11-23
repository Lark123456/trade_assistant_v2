"""复算太辰光 2026-07-10 除权事件对 9 月 K 线的影响"""
import os
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from data_feed import DataFeed, infer_market
from core.adjust import compute_qfq_prices, filter_price_events
from database import Database

db = Database("trade_assistant.db")
events = db.get_xdxr_events("300570")
print("=== 太辰光 2026 年除权事件 ===")
for e in events:
    if e["date"].startswith("2026"):
        print(e)

print()
print("=== filter_price_events 处理后 ===")
filtered = filter_price_events(events)
for e in filtered:
    print(e)

print()
print("=== 2026-07-10 事件复算 ===")
event_2026 = [e for e in filtered if e["date"] == "2026-07-10"]
print(filtered)

# 假设 7月9日（除权前一天）close = X
# 7月10日除权：每股分红 10 元（per_share = 10/10 = 1.0，因为 pytdx 是每10股)
# 等下，前面 filter_price_events 把 fenhong/10，所以 fenhong_per_share = 8/10 = 0.8 ?
# 等等，2026-07-10 fenhong=10.0，per pytdx 是每10股 10 元 = 每股 1 元
# filter 后 fenhong = 10/10 = 1.0

# 但是！我们看到 xdxr_events 里 2025-06-10 fenhong=8.0 对应的是「每股 0.8 元」
# 这跟通达信的「每10股」习惯一致
# 所以 2026-07-10 fenhong=10.0 = 每10股 10 元 = **每股 1 元**

print()
print("=== 计算 2026-07-10 除权参考价 ===")
# pre_close = 7月9日 close (假设为 P)
# ref = (P - 1) / (1 + 0) = P - 1
# 然后前复权：rate = ref / pre_close = (P-1)/P

# 假设 7月9日 原始 close = 210
# ref = 210 - 1 = 209
# rate = 209/210 = 0.9952
# 7月10日 开盘 qfq = 原始开盘 * 0.9952

# 我们查 daily_bars_cache里 7月9日和 7月10日的数据
import sqlite3
conn = sqlite3.connect("trade_assistant.db")
conn.row_factory = sqlite3.Row
cur = conn.cursor()
print()
print("=== 太辰光 2026-07 附近 K 线（看除权前后）===")
rows = cur.execute("""
    SELECT date, open, high, low, close, vol
    FROM daily_bars_cache WHERE code='300570'
    AND date BETWEEN '2026-07-01' AND '2026-07-20'
    ORDER BY date
""").fetchall()
for r in rows:
    print(dict(r))

# 关键测试：手动复算 9月8日的价格
print()
print("=== 9月8日原始价 vs qfq 价对比 ===")
# 取 daily_bars_cache 里 9月8日数据 (qfq)
rows = cur.execute("""
    SELECT date, open, high, low, close
    FROM daily_bars_cache WHERE code='300570'
    AND date BETWEEN '2026-09-04' AND '2026-09-10'
    ORDER BY date
""").fetchall()
for r in rows:
    print(dict(r))

conn.close()
