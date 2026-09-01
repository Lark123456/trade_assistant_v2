# -*- coding: utf-8 -*-
"""二层精筛手动回填工具（v0.7.4）：对指定交易日的粗筛命中执行二层精筛并落库。

用途：
- 修复历史日期「二层精筛为 0」：`python backfill_layer2.py 2026-08-17`
- 复现 Worker._chain_layer2 的完整链路（只读缓存日K，不联网）：
  DTW骨架分 → 微观规则加减分/买点定级 → final_score=clip(骨架+修正,0,100)
  → 过滤 final_score>=75 或 Top15 → 持久化 layer=2 并回写观察池 DNA 标注。

用法：
  python backfill_layer2.py [YYYY-MM-DD] [阈值] [TopN]
  默认日期 = 最近一次一层粗筛会话所在日；默认阈值 75；默认 TopN 15。
"""
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import Database
from scanner import layer2_screen

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_assistant.db")


class CacheOnlyFeed:
    """离线行情源：只读 SQLite 日K缓存（不触网）。"""

    def __init__(self, db):
        self.db = db

    def get_daily_bars(self, code, market=None, count=300):
        df = self.db.get_cached_daily_bars(code)
        if df is None or df.empty:
            return None
        if count and len(df) > count:
            df = df.tail(count).reset_index(drop=True)
        return df


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else None
    threshold = float(sys.argv[2]) if len(sys.argv) > 2 else 75.0
    top_n = int(sys.argv[3]) if len(sys.argv) > 3 else 15

    db = Database(DB)
    feed = CacheOnlyFeed(db)

    from core.dna_engine import FEATURE_VERSION, TemplateManager
    tm = TemplateManager()
    names = [n for n in tm.templates
             if tm.metadata.get(n, {}).get("_feature_version") == FEATURE_VERSION]
    print(f"模具库: {len(tm.templates)} 个，当前版本可用 {len(names)} 个 -> {names}")
    if not names:
        print("!! 模具库为空或版本不符，无法执行二层精筛")
        return

    # 目标日期：默认取最近一次一层粗筛会话的交易日
    if not day:
        rows = db.list_scan_history(limit=1000)
        day = next((r.get("trade_date") or r.get("signal_date")
                    or (r.get("scan_time") or "")[:10]
                    for r in reversed(rows) if r.get("layer", 1) == 1), None)
    if not day:
        print("!! 未找到一层粗筛数据，请先运行扫描或显式指定日期")
        return
    print(f"目标交易日: {day}")

    rows = db.list_scan_history(day=day, limit=5000)
    cands = [r for r in rows if r.get("layer", 1) == 1]
    uniq = {}
    for r in cands:
        uniq.setdefault(str(r["code"]), r)
    cand_items = [{"code": r["code"], "name": r["name"], "market": r["market"]}
                  for r in uniq.values()]
    print(f"一层粗筛候选（按代码去重）: {len(cand_items)} 只 (原始 {len(cands)} 条)")
    if not cand_items:
        print("!! 无候选")
        return

    t0 = time.time()
    hits = layer2_screen(feed, cand_items, names, threshold=threshold, top_n=top_n,
                         window_mode="template_len", length_scan=True,
                         workers=1, as_of_date=day)
    print(f"二层精筛完成: {len(cands)} -> {len(hits)} 只通过, 耗时 {time.time() - t0:.1f}s")

    session_id = uuid.uuid4().hex[:12]
    if hits:
        db.add_scan_hits(session_id, None, "自动二层精筛", hits, layer=2)
        print(f"已写入 scan_history: session={session_id}, layer=2, {len(hits)} 条")

    active = {str(w["code"]) for w in db.list_watch()
              if w["status"] in ("待审核", "观察中", "已开仓")}
    upd = 0
    for h in hits:
        note = f"；精筛[{h.dna_template}] {h.dna_score}分"
        if h.final_score is not None:
            note += f"；综合{round(float(h.final_score), 1)}分"
        if h.buy_level:
            note += f"；{h.buy_level}"
        if h.signals:
            note += f"；{h.signals}"
        if str(h.code) in active and db.update_watch_dna(h.code, h.dna_score,
                                                         h.dna_template, note=note):
            upd += 1
    db.mark_session_in_watchlist(session_id)
    print(f"观察池 DNA 标注更新: {upd} 只；session 全部标记 in_watchlist=1")

    print(f"\n=== {day} 二层精筛排行榜（前 20）===")
    print(f"{'代码':<8}{'名称':<10}{'综合':>6}{'骨架':>6}  {'买点定级':<10}"
          f"{'模具':<22}{'现价':>7}{'涨幅%':>7}  微观诊断")
    for h in hits[:20]:
        print(f"{h.code:<8}{h.name:<10}{h.final_score:>6.1f}{h.dna_score:>6.1f}  "
              f"{h.buy_level or '-':<10}{h.dna_template:<22}{h.price:>7.2f}{h.pct:>7.2f}  "
              f"{h.signals or '-'}")
    print(f"\n通过 {len(hits)} 只；SB1核心买点: "
          f"{sum(1 for h in hits if h.buy_level == 'SB1核心买点')} 只, "
          f"B1底仓买点: {sum(1 for h in hits if h.buy_level == 'B1底仓买点')} 只")

    n2 = sum(1 for r in db.list_scan_history(day=day, limit=5000) if r.get("layer") == 2)
    nw = sum(1 for w in db.list_watch() if (w.get("add_date") or "") == day)
    print(f"\n日历计数核对: 粗筛 {len(cands)} 条 / 精筛 {n2} 条 / 观察池(当日) {nw} 只")

    db.close()


if __name__ == "__main__":
    main()
