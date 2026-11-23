"""查 太辰光 2026-09 全月日 K"""
import sqlite3
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
conn = sqlite3.connect('trade_assistant.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print('=== 太辰光 2026-09 全月 daily_bars_cache ===')
rows = cur.execute("""
    SELECT date, open, high, low, close, vol
    FROM daily_bars_cache WHERE code='300570'
    AND date BETWEEN '2026-09-01' AND '2026-09-30'
    ORDER BY date
""").fetchall()
prev_close = None
for r in rows:
    d = dict(r)
    pct = None
    if prev_close is not None and prev_close > 0:
        pct = (d['close'] - prev_close) / prev_close * 100
    print(f"{d['date']}  O={d['open']:.2f} H={d['high']:.2f} L={d['low']:.2f} "
          f"C={d['close']:.2f}  V={d['vol']:.0f}  涨跌={pct:+.2f}%" if pct is not None
          else f"{d['date']}  O={d['open']:.2f} H={d['high']:.2f} L={d['low']:.2f} "
               f"C={d['close']:.2f}  V={d['vol']:.0f}")
    prev_close = d['close']

print()
print('=== scan_history 中太辰光 9月所有记录（按 trade_date 排序）===')
rows = cur.execute("""
    SELECT id, scan_time, signal_date, trade_date, price, pct, reason
    FROM scan_history WHERE code='300570'
    AND signal_date BETWEEN '2026-09-01' AND '2026-09-10'
    ORDER BY trade_date, id
""").fetchall()
for r in rows:
    print(dict(r))

print()
print('=== market_snapshot 中太辰光所有记录 ===')
rows = cur.execute("""
    SELECT * FROM market_snapshot WHERE code='300570'
    ORDER BY update_time DESC LIMIT 5
""").fetchall()
for r in rows:
    d = dict(r)
    # 截掉 fields 显示完整
    print({k: v for k, v in d.items()})

conn.close()
