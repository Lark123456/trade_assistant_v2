"""查 太辰光 9月附近的除权除息事件 + 实际盘中数据"""
import sqlite3
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
conn = sqlite3.connect('trade_assistant.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print('=== xdxr_events 太辰光 2025年 ===')
rows = cur.execute("""
    SELECT * FROM xdxr_events
    WHERE code='300570' AND date LIKE '2025%'
    ORDER BY date
""").fetchall()
for r in rows:
    print(dict(r))
print(f"共 {len(rows)} 条")

print()
print('=== 太辰光所有 scan_history 中 pct 不为 None 的，按 pct 排序看分布 ===')
rows = cur.execute("""
    SELECT id, scan_time, signal_date, trade_date, price, pct
    FROM scan_history WHERE code='300570' AND pct IS NOT NULL
    ORDER BY scan_time DESC LIMIT 30
""").fetchall()
for r in rows:
    print(dict(r))

print()
print('=== 太辰光 daily_bars_cache 全部 2025 年数据 ===')
rows = cur.execute("""
    SELECT date, open, high, low, close, vol
    FROM daily_bars_cache WHERE code='300570'
    ORDER BY date DESC LIMIT 30
""").fetchall()
for r in rows:
    d = dict(r)
    # 算一下次日涨跌幅
    print(d)

conn.close()
