# -*- coding: utf-8 -*-
"""决定性实验：直接调用 auto_sync_codes_to_tdx 用真实数据库, 捕获 (ok, msg)"""
import sys, os, datetime
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, r"G:\Deepseek workplace\trade_assistant")

from database import Database
from core.auto_tdx_sync import auto_sync_codes_to_tdx

DB_PATH = r"G:\Deepseek workplace\trade_assistant\trade_assistant.db"

print("=" * 60)
print("  通达信同步决定性诊断")
print("=" * 60)

# 1. TDX 进程
print("\n[1] 通达信进程:")
import subprocess
tdx_check = subprocess.run(
    ['powershell', '-NoProfile', '-Command',
     'Get-Process -ErrorAction SilentlyContinue | Where-Object {$_.ProcessName -match "tdxw"} | Measure-Object | Select-Object -ExpandProperty Count'],
    capture_output=True, text=True
)
print(f"    tdxw 进程数: {tdx_check.stdout.strip()}")

# 2. db 连接 + settings
print("\n[2] 打开真实数据库:")
print(f"    DB: {DB_PATH}")
print(f"    文件存在: {os.path.exists(DB_PATH)}, 大小: {os.path.getsize(DB_PATH)/1024/1024:.1f} MB")
db = Database(DB_PATH)

# 3. 直接调用 auto_sync_codes_to_tdx
print("\n[3] 调用 auto_sync_codes_to_tdx (测试用策略名『诊断测试』, 2 个真实代码):")
codes = ["600000", "000001"]
strategy_name = "诊断测试"
date = datetime.date.today()
print(f"    strategy={strategy_name}, codes={codes}, date={date}")

try:
    ok, msg = auto_sync_codes_to_tdx(db, strategy_name, codes, date=date)
    print(f"\n[4] 返回结果:")
    print(f"    ok  = {ok}")
    print(f"    msg =")
    for line in msg.splitlines():
        print(f"        {line}")
except Exception as e:
    print(f"\n[!] 调用本身抛出异常: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 5. 检查文件是否真的写出来
print("\n[5] blocknew 目录现状 (脚本执行后):")
import glob
for f in sorted(glob.glob(r"E:\TDX\T0002\blocknew\91诊断*"), key=os.path.getmtime):
    print(f"    {f}  ({os.path.getsize(f)} bytes, mtime={datetime.datetime.fromtimestamp(os.path.getmtime(f))})")

# 6. 清理测试产物 (如果在的话)
test_blk = r"E:\TDX\T0002\blocknew\91诊断.blk"
test_cfg = r"E:\TDX\T0002\blocknew\blocknew.cfg"
if ok and os.path.exists(test_blk):
    print("\n[6] 测试成功, 正在清理测试产物...")
    try:
        os.remove(test_blk)
        print(f"    已删除 {test_blk}")
    except Exception as e:
        print(f"    [!] 删除 .blk 失败: {e}  请您在通达信里手动删除『91诊断』板块")

print("\n" + "=" * 60)
print("  诊断完成 - 请把上面所有输出贴给我")
print("=" * 60)