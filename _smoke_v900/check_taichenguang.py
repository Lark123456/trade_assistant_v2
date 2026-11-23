"""查 太辰光 9月4号的数据"""
import sqlite3
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
conn = sqlite3.connect('trade_assistant.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print('=== stock_list 中匹配太辰光的记录 ===')
rows = cur.execute("SELECT code, name, market FROM stock_list WHERE name LIKE '%太辰光%'").fetchall()
for r in rows:
    print(dict(r))

print()
print('=== scan_history 太辰光 9月4号附近所有记录 ===')
rows = cur.execute("""
    SELECT id, strategy_id, strategy_name, scan_time, code, name, market,
           price, pct, reason, layer, signal_date, trade_date
    FROM scan_history
    WHERE name LIKE '%太辰光%'
      AND (scan_time LIKE '2025-09-04%' OR trade_date = '2025-09-04'
           OR scan_time LIKE '2025-09-03%')
    ORDER BY id DESC
    LIMIT 30
""").fetchall()
for r in rows:
    d = dict(r)
    print(d)

print()
print('=== scan_history 太辰光最近 30 条 ===')
rows = cur.execute("""
    SELECT id, scan_time, code, name, price, pct, signal_date, trade_date, layer
    FROM scan_history
    WHERE name LIKE '%太辰光%'
    ORDER BY id DESC
    LIMIT 30
""").fetchall()
for r in rows:
    print(dict(r))

print()
print('=== daily_bars_cache 太辰光 9月附近 ===')
rows = cur.execute("""
    SELECT code, date, open, high, low, close, vol
    FROM daily_bars_cache
    WHERE code IN (SELECT code FROM stock_list WHERE name LIKE '%太辰光%')
      AND date BETWEEN '2025-08-25' AND '2025-09-10'
    ORDER BY date
""").fetchall()
for r in rows:
    print(dict(r))

conn.close()
