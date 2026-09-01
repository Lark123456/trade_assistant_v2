# -*- coding: utf-8 -*-
"""测试 tdx_sync 核心模块（真实 TDX 目录只读 + 项目内临时目录读写回环）。

v0.8.2：通达信自选股板块双向同步（.blk 纯文本 + blocknew.cfg 注册）。
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from tdx_sync import (TdxBlockSync, TdxStock, parse_blk_line, read_blk,
                      tdx_market_digit, write_blk)

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


print("=== 1. 市场位推断 ===")
for code, expect in [("600000", 1), ("688799", 1), ("000001", 0), ("002940", 0),
                     ("300200", 0), ("920992", 2), ("832000", 2), ("430047", 2)]:
    check(f"{code}→{'沪' if expect == 1 else '深' if expect == 0 else '北'}({expect})",
          tdx_market_digit(code) == expect, f"got {tdx_market_digit(code)}")

print("\n=== 2. 行解析 ===")
st = parse_blk_line("0002940")
check("0002940 → 002940/深", st is not None and st.code == "002940" and st.market == 0)
st = parse_blk_line("1600009")
check("1600009 → 600009/沪", st is not None and st.code == "600009" and st.market == 1)
check("非法行 None", parse_blk_line("abc1234") is None)
check("空行 None", parse_blk_line("") is None)
check("长度错 None", parse_blk_line("123") is None)
check("市场位错 None", parse_blk_line("9000001") is None)

print("\n=== 3. 读写回环（项目内临时目录） ===")
test_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tdx_test_tmp")
if os.path.exists(test_root):
    shutil.rmtree(test_root, ignore_errors=True)
os.makedirs(os.path.join(test_root, "T0002", "blocknew"), exist_ok=True)

sync = TdxBlockSync(test_root)
check("is_valid_root 有效", TdxBlockSync.is_valid_root(test_root))
check("is_valid_root 无效", not TdxBlockSync.is_valid_root(os.path.join(test_root, "no-such")))

codes = ["600000", "002940", "688799", "300200", "920992"]
# "程序选股" = 4 汉字 = 8 字节 GBK，正好不截断
BLOCK_NAME = "程序选股"
n, path = sync.export_block(codes, BLOCK_NAME)
check(f"导出 {n} 只", n == 5, f"n={n}")
check("板块文件已生成", os.path.exists(path), path)
with open(path, "rb") as f:
    raw = f.read()
check("CRLF 行尾", raw.endswith(b"\r\n"), raw[-10:])
check("含 1600000 行", b"1600000" in raw)
check("含 0002940 行", b"0002940" in raw)

blocks = sync.list_blocks()
names = [b[0] for b in blocks]
check("cfg 已注册新板块", BLOCK_NAME in names, f"blocks={blocks}")
check("cfg 记录数 = 1（新目录只有我们注册的板块）", len(blocks) == 1, f"n={len(blocks)}")

back = sync.import_block(BLOCK_NAME)
check(f"导入 {len(back)} 只", len(back) == 5, f"n={len(back)}")
codes_back = sorted({s.code for s in back})
check("导入代码一致", codes_back == sorted(codes), f"{codes_back}")

# 追加模式
n2, _ = sync.export_block(["000001"], BLOCK_NAME, overwrite=False)
check("追加后共 6 只", n2 == 6, f"n={n2}")
# 重复导出（覆盖模式）回 5 只
n3, _ = sync.export_block(codes, BLOCK_NAME, overwrite=True)
check("覆盖后回 5 只", n3 == 5, f"n={n3}")

# v0.8.10 行为变更：板块名不再截断——完整名写入 .blk 文件名与 cfg filename 字段；
# cfg 8 字节 name 字段仍截断(TDX 硬限制), 但文件系统里是完整名。
n4, path4 = sync.export_block(["000001"], "PYTHON选股")
fname4 = os.path.basename(path4)
check("超长板块名保留完整名（不再截断）", fname4 == "PYTHON选股.blk", fname4)
back4 = sync.import_block(fname4[:-4])
check("完整名板块可正常导入", len(back4) == 1, f"n={len(back4)}")
# cfg 注册记录: name 字段 8 字节截断, filename 字段 70 字节存完整名
if os.path.exists(sync.cfg_path):
    with open(sync.cfg_path, "rb") as f:
        cfg = f.read()
    check("cfg 记录数 * 120 = 文件大小", len(cfg) % 120 == 0)
    full_gbk = "PYTHON选股".encode("gbk")
    short_gbk = "PYTHON选".encode("gbk").ljust(8, b"\x00")
    found_full = found_short = False
    for off in range(0, len(cfg) - 119, 120):
        rec = cfg[off:off + 120]
        if rec[50:120].rstrip(b"\x00") == full_gbk:
            found_full = True
        if rec[:8] == short_gbk:
            found_short = True
    check("cfg filename 字段含完整板块名", found_full)
    check("cfg name 字段截断为 8 字节", found_short)

# 清理
shutil.rmtree(test_root, ignore_errors=True)

print("\n=== 4. 真实 TDX 目录（只读，E:\\TDX） ===")
REAL_TDX = r"E:\TDX"
if os.path.exists(REAL_TDX) and TdxBlockSync.is_valid_root(REAL_TDX):
    real = TdxBlockSync(REAL_TDX)
    blocks = real.list_blocks()
    check("识别到板块（>0）", len(blocks) > 0, f"n={len(blocks)}")
    if blocks:
        name0, fname0 = blocks[0]
        check("板块名/文件名非空", bool(name0) and bool(fname0), f"{name0}/{fname0}")
        stocks = real.import_block(fname0)
        check(f"读取板块「{name0}」成功", len(stocks) > 0, f"n={len(stocks)}")
        if stocks:
            s0 = stocks[0]
            check("股票字段合法", len(s0.code) == 6 and s0.market in (0, 1, 2),
                  f"{s0.line}")
else:
    print("  [SKIP] 未找到 E:\\TDX（跳过真实目录只读检查）")

print(f"\n==================================================\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
