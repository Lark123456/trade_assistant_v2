"""核对太辰光 9月4日-9月10日的 K 线连续性"""
import sqlite3
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
conn = sqlite3.connect('trade_assistant.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print('=== 太辰光 2026-09 全月 K 线（看连续性）===')
rows = cur.execute("""
    SELECT date, open, high, low, close, vol
    FROM daily_bars_cache WHERE code='300570'
    AND date BETWEEN '2026-08-25' AND '2026-10-15'
    ORDER BY date
""").fetchall()
prev = None
for r in rows:
    d = dict(r)
    sign = ''
    if prev is not None:
        chg = (d['close'] - prev) / prev * 100
        sign = f"  日涨跌 {chg:+.2f}%  "
    print(f"  {d['date']}  O={d['open']:.2f} H={d['high']:.2f} L={d['low']:.2f} "
          f"C={d['close']:.2f}  V={d['vol']:.0f}{sign}")
    prev = d['close']

# 检查实时接口给的 last_close 和 price
print()
print('=== market_snapshot（实时快照）===')
row = cur.execute("""
    SELECT * FROM market_snapshot WHERE code='300570'
""").fetchone()
print(dict(row))

# 看下太辰光 2026 年所有 xdxr 事件
print()
print('=== 太辰光 所有 xdxr 事件 ===')
rows = cur.execute("""
    SELECT date, category, fenhong, peigujia, songzhuangu, peigu
    FROM xdxr_events WHERE code='300570' ORDER BY date
""").fetchall()
for r in rows:
    print(dict(r))

conn.close()
