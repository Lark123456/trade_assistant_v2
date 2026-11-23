"""核对其他几只票的加入价/现价/当日涨跌"""
import sqlite3
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
conn = sqlite3.connect('trade_assistant.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# 截图里的票
codes = ['301345', '688146', '301183', '300567', '300570']
print('=== 每只票的加入价(命中日 close) 和 当前最新日 K ===')
for code in codes:
    print(f"\n--- {code} ---")
    # scan_history 最新命中
    rows = cur.execute("""
        SELECT id, scan_time, signal_date, trade_date, price, pct
        FROM scan_history WHERE code=? AND signal_date LIKE '2026-09%'
        ORDER BY id DESC LIMIT 3
    """, (code,)).fetchall()
    for r in rows:
        print(f"  scan_history: {dict(r)}")
    # 日 K 9月
    rows = cur.execute("""
        SELECT date, open, high, low, close, vol
        FROM daily_bars_cache WHERE code=?
        AND date BETWEEN '2026-08-30' AND '2026-09-10'
        ORDER BY date
    """, (code,)).fetchall()
    for r in rows:
        print(f"  K: {dict(r)}")
    # 实时行情
    rows = cur.execute("""
        SELECT code, price, last_close, open, high, low, pct, update_time
        FROM market_snapshot WHERE code=?
    """, (code,)).fetchall()
    for r in rows:
        print(f"  market_snapshot: {dict(r)}")

conn.close()
