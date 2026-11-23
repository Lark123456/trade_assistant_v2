# -*- coding: utf-8 -*-
"""check_tdx_block.py —— 通达信板块 vs 主程序扫描结果 对账工具

用途：当你在通达信里看到某只票"不该在某个板块里"（或该在却不在）时，
用这个脚本一次性核对"通达信板块文件内容"与"scan_history 里保存的命中"，
快速定位是【看错了板块】还是【真的写错了】。

用法：
    python check_tdx_block.py                      # 列最近 10 个板块 + 数量
    python check_tdx_block.py 600051               # 查 600051 在哪些板块里 + 它的历史信号日
    python check_tdx_block.py 600051 2026-09-18    # 同时核对指定信号日的 B1 判定

输出的三块内容：
  [1] 通达信 blocknew.cfg 注册表（cfg 显示名 → .blk 文件名）
      —— 通达信"板块管理"里看到的是 cfg 名（8 字节，例 0918B1），
         而 T0002\\blocknew 文件夹里的文件名是完整名（例 2026-9-18·B1海选.blk）。
         两个名字混看极易认错日期，本表把它们一一对应出来。
  [2] 各 .blk 文件行数 / 是否含目标代码 / 修改时间
  [3] 该代码在 scan_history 里的信号日（trade_date）与所属策略

提示：控制台若中文乱码，先执行 `chcp 65001`（本脚本按 UTF-8 输出）。
"""
from __future__ import annotations

import datetime
import os
import sqlite3
import sys

try:  # 统一按 UTF-8 输出，避免 GBK 控制台把中文/特殊符号打成乱码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DB_PATH = os.path.join(HERE, "trade_assistant.db")
CFG_RECORD_SIZE = 120
NAME_FIELD = 8
FNAME_OFFSET = 50        # cfg 布局：0-7 名称 / 8-9 类型 / 10-49 保留 / 50-119 文件名


def default_tdx_root() -> str:
    """优先用 settings.tdx.path，其次常见路径探测。"""
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        row = conn.execute("SELECT value FROM settings WHERE key='tdx.path'").fetchone()
        conn.close()
        if row and row[0] and os.path.isdir(row[0]):
            return row[0].strip()
    except Exception:
        pass
    for root in (r"E:\TDX", r"C:\new_tdx", r"D:\new_tdx", r"E:\new_tdx",
                 r"C:\TDX", r"D:\TDX"):
        if os.path.isdir(os.path.join(root, "T0002", "blocknew")):
            return root
    return ""


def read_cfg(root: str):
    """解析 blocknew.cfg → [(cfg显示名, 文件名), ...]"""
    path = os.path.join(root, "T0002", "blocknew", "blocknew.cfg")
    out = []
    if not os.path.exists(path):
        return out
    with open(path, "rb") as f:
        data = f.read()
    for off in range(0, len(data) - CFG_RECORD_SIZE + 1, CFG_RECORD_SIZE):
        rec = data[off:off + CFG_RECORD_SIZE]
        name = rec[:NAME_FIELD].decode("gbk", errors="ignore").rstrip("\x00 ").strip()
        fname = rec[FNAME_OFFSET:].decode("gbk", errors="ignore").rstrip("\x00 ").strip()
        if name:
            out.append((name, fname))
    return out


def blk_codes(path: str):
    """读 .blk 的 6 位代码列表（兼容 1 位市场位前缀）。"""
    codes = []
    if not os.path.exists(path):
        return codes
    with open(path, "rb") as f:
        for line in f:
            s = line.strip().decode("ascii", errors="ignore").strip()
            if len(s) == 7 and s.isdigit():
                codes.append(s[1:])
            elif len(s) == 6 and s.isdigit():
                codes.append(s)
    return codes


def main():
    args = [a for a in sys.argv[1:]]
    code = ""
    date = ""
    for a in args:
        if len(a) == 6 and a.isdigit():
            code = a
        elif len(a) == 10 and a[4] == "-":
            date = a

    root = default_tdx_root()
    if not root:
        print("未找到通达信目录（可在 settings 里设 tdx.path）")
        return 1
    blocknew = os.path.join(root, "T0002", "blocknew")
    print(f"通达信根目录: {root}")

    entries = read_cfg(root)
    print(f"\n[1] blocknew.cfg 注册表（共 {len(entries)} 条，仅显示最近 12 条）")
    print(f"    {'cfg显示名(TDX里看到的)':<22} -> .blk 文件名")
    for name, fname in entries[-12:]:
        print(f"    {name!r:<22} -> {fname!r}")

    print(f"\n[2] .blk 文件核查（按修改时间倒序，前 15 个）")
    files = [os.path.join(blocknew, f) for f in os.listdir(blocknew)
             if f.lower().endswith(".blk")]
    files.sort(key=os.path.getmtime, reverse=True)
    for p in files[:15]:
        codes = blk_codes(p)
        mt = datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%m-%d %H:%M")
        mark = ""
        if code:
            mark = "  ★含目标代码" if code in codes else ""
        print(f"    [{mt}] {len(codes):>5} 只  {os.path.basename(p)!r}{mark}")

    if code:
        print(f"\n[3] 代码 {code} 出现在以下板块中：")
        hit_any = False
        for p in files:
            codes = blk_codes(p)
            if code in codes:
                hit_any = True
                mt = datetime.datetime.fromtimestamp(os.path.getmtime(p)).strftime("%m-%d %H:%M")
                # 找它对应的 cfg 显示名
                base = os.path.basename(p)[:-4]
                cfg_names = [n for n, f in entries if f == base]
                print(f"    {os.path.basename(p)!r} ({len(codes)} 只, mtime {mt})"
                      f"  TDX显示名: {cfg_names or '（未注册，TDX里看不到）'}")
        if not hit_any:
            print("    （不在任何 .blk 里）")

        print(f"\n[4] {code} 在 scan_history 里的信号日（最近 20 条）")
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT scan_time, trade_date, strategy_name, layer, price, reason "
                "FROM scan_history WHERE code=? ORDER BY id DESC LIMIT 20", (code,)
            ).fetchall()
            if not rows:
                print("    （无记录）")
            for r in rows:
                print(f"    scan={r['scan_time']} 信号日={r['trade_date']} "
                      f"策略={r['strategy_name']!r} L{r['layer']} 价={r['price']}")
            conn.close()
        except Exception as e:
            print("    查询失败:", e)

        if date:
            print(f"\n[5] 用项目公式引擎核对 {code} 在 {date} 是否满足「B1海选」")
            try:
                import pandas as pd
                from indicators import compute_indicators
                conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
                formula = conn.execute(
                    "SELECT formula FROM strategies WHERE name LIKE 'B1海选%' LIMIT 1"
                ).fetchone()
                rows = conn.execute(
                    "SELECT date, open, high, low, close, vol, amount FROM daily_bars_cache "
                    "WHERE code=? ORDER BY date", (code,)).fetchall()
                conn.close()
                if not formula or not rows:
                    print("    （缺公式或日K缓存）")
                else:
                    df = pd.DataFrame(rows, columns=["date", "open", "high", "low",
                                                     "close", "vol", "amount"])
                    dfi = compute_indicators(df)
                    t = dfi[dfi["date"].astype(str) <= date]
                    if t.empty:
                        print(f"    （{date} 无K线数据：缓存只到 "
                              f"{str(dfi['date'].iloc[-1])}）")
                    else:
                        from indicators import formula_signal
                        hit, labels = formula_signal(t, formula[0])
                        last = t.iloc[-1]
                        closes = t["close"].reset_index(drop=True)
                        e1 = closes.ewm(span=10, adjust=False).mean()
                        white = e1.ewm(span=10, adjust=False).mean().iloc[-1]
                        yellow = sum(closes.tail(n).mean()
                                     for n in (14, 28, 57, 114)) / 4
                        j = 3 * last["KDJ_K"] - 2 * last["KDJ_D"]
                        print(f"    截止 {str(last['date'])}: 白线={white:.3f} "
                              f"黄线={yellow:.3f} ({'白>黄 满足' if white > yellow else '白>黄 不满足'}) "
                              f"J={j:.2f} ({'J<13 满足' if j < 13 else 'J<13 不满足'})")
                        print(f"    B1海选信号: {'命中 [YES]' if hit else '不命中 [NO]'}")
            except Exception as e:
                print("    核对失败:", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
