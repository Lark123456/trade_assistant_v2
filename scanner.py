# -*- coding: utf-8 -*-
"""策略扫描引擎：策略→公式、日K本地缓存、全市场并行扫描、盘中重点范围扫描。"""
import datetime
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

import pandas as pd
import numpy as np

from data_feed import DataFeed, is_trading_time
from indicators import compute_indicators, formula_signal

log = logging.getLogger("scanner")

ProgressCb = Optional[Callable[[int, int, str], None]]
CancelFlag = Optional[Callable[[], bool]]


@dataclass
class ScanResult:
    code: str
    name: str
    market: int
    price: float = 0.0
    pct: float = 0.0
    vol: float = 0.0
    amount: float = 0.0
    indicators: dict = field(default_factory=dict)
    match_reason: str = ""
    # v0.8.1：strategy_id（跨层级候选流转时按父策略 ID 索引）
    strategy_id: int = 0
    # v0.8.0：选股方向（与 strategy.direction 对齐）；v0.8.1 起已废弃
    direction: str = ""
    # v0.7.0：二层DNA精筛字段（layer=2 时填充）
    dna_score: float = None
    dna_template: str = ""
    dna_detail: str = ""      # JSON 字符串：{score,template,len,start,end,all}
    # v0.7.4：二层综合评分（DTW骨架分 + 微观规则修正）
    final_score: float = None   # 综合得分 = clip(dtw_score + score_modifier, 0, 100)
    buy_level: str = ""         # 买点定级：SB1核心买点 / B1底仓买点 / ""
    signals: str = ""           # 触发的微观信号（"；"分隔）
    # v0.7.3：历史日期扫描的信号日期（None=最新K线）
    signal_date: str = ""


def strategy_formula(strategy: dict) -> str:
    """从策略记录解析出可执行公式（v0.6.3 起统一为公式表达）。"""
    return (strategy.get("formula") or "").strip()


def scan_by_quotes(quotes: pd.DataFrame, min_pct=-10.0, max_pct=10.0,
                   min_amount=0.0, max_price=0.0, min_price=0.0,
                   top_n=0, sort_by="pct"):
    """基于实时行情的快速过滤（全市场秒级）。返回 ScanResult 列表。"""
    df = quotes.copy()
    if "name" not in df.columns:
        df["name"] = ""
    mask = pd.Series(True, index=df.index)
    mask &= df["pct"] >= min_pct
    mask &= df["pct"] <= max_pct
    if min_amount > 0:
        mask &= df["amount"] >= min_amount
    if min_price > 0:
        mask &= df["price"] >= min_price
    if max_price > 0:
        mask &= df["price"] <= max_price
    df = df[mask]
    if sort_by in df.columns:
        df = df.sort_values(sort_by, ascending=False)
    if top_n > 0:
        df = df.head(top_n)
    return [ScanResult(code=r.code, name=r.name, market=r.market,
                       price=r.price, pct=r.pct, vol=r.vol, amount=r.amount)
            for r in df.itertuples()]


def scan_formula(feed, stock_df: pd.DataFrame, formula: str, max_stocks=100,
                 quote_filter: Optional[pd.DataFrame] = None, bars_count=300,
                 start_date=None, end_date=None, direction="", strategy_id=0,
                 progress: ProgressCb = None, cancelled: CancelFlag = None):
    """基于通达信公式的深度扫描。

    - 若提供 quote_filter（实时行情），先按其过滤收窄范围，再按 |pct| 取前 max_stocks 只
    - 对候选逐只拉取日K（走缓存）、计算指标，判断公式在信号日K线是否成立
    - start_date/end_date（YYYY-MM-DD，v0.7.3 历史日期扫描）：
      不传=判断最新一根K线；传了=对 [start_date, end_date] 范围内每个交易日
      逐日判断（指标在全序列上计算，逐日截断判断，结果按 signal_date 记录）。
      某股票在信号日无数据（停牌/未上市）时自动跳过该日。
    - v0.8.1：strategy_id 写入 ScanResult 用于跨层级候选按父策略 ID 流转；
              direction 已废弃但保留参数兼容。
    """
    if not formula or not formula.strip():
        raise ValueError("公式不能为空")

    candidates = stock_df
    if quote_filter is not None and not quote_filter.empty:
        qf = quote_filter.set_index("code")
        # 只保留行情列，避免与候选表的 name/market 列冲突
        keep = [c for c in ("price", "pct", "vol", "amount") if c in qf.columns]
        qf = qf[keep]
        merged = candidates.merge(qf, left_on="code", right_index=True, how="inner")
        # 只取行情里活跃且涨幅在合理区间的标的作为候选
        merged = merged[(merged["pct"] >= -9.9) & (merged["pct"] <= 9.9)]
        merged = merged.sort_values("pct", ascending=False)
        candidates = merged.head(max_stocks).reset_index(drop=True)
    else:
        candidates = candidates.head(max_stocks)

    total = len(candidates)
    results = []
    start = datetime.datetime.now()
    for i, row in enumerate(candidates.itertuples()):
        if cancelled and cancelled():
            log.info("扫描被取消")
            break
        if progress:
            progress(i, total, row.name)
        try:
            if end_date and isinstance(feed, CachedFeed):
                # 历史日期扫描：按目标日期判定缓存可用性，避免全市场重拉
                bars = feed.get_daily_bars(row.code, market=row.market,
                                           count=bars_count, as_of_date=end_date)
            else:
                bars = feed.get_daily_bars(row.code, market=row.market,
                                           count=bars_count)
            if bars is None or len(bars) < 30:
                continue
            if end_date:
                bars = bars[bars["date"] <= end_date].reset_index(drop=True)
                if len(bars) < 30:
                    continue
            df = compute_indicators(bars)
            if start_date and end_date:
                # 历史范围扫描：逐日判断 [start, end] 内每个交易日
                days = [d for d in df["date"].astype(str).tolist()
                        if start_date <= d <= end_date]
                for d in days:
                    if cancelled and cancelled():
                        break
                    df_t = df[df["date"] <= d]
                    if df_t.empty or str(df_t["date"].iloc[-1]) != d:
                        continue  # 该日无数据（停牌等）
                    hit, labels = formula_signal(df_t, formula)
                    if hit:
                        results.append(_make_scan_result(row, df_t, labels, d,
                                                         direction, strategy_id))
            else:
                hit, labels = formula_signal(df, formula)
                if hit:
                    results.append(_make_scan_result(row, df, labels, None,
                                                     direction, strategy_id))
        except Exception as e:  # noqa: BLE001
            log.debug("扫描 %s 失败: %s", row.code, e)
    log.info("公式扫描完成：%d/%d 只命中，耗时 %.1fs",
             len(results), total, (datetime.datetime.now() - start).total_seconds())
    return results


def _make_scan_result(row, df, labels, signal_date, direction="", strategy_id=0):
    """由命中行构造 ScanResult（signal_date 为历史日期扫描的信号日期）。
    v0.8.1：新增 strategy_id 用于跨层级候选流转；direction 已废弃但保留兼容。
    """
    last = df.iloc[-1]
    reason = "；".join(f"{k}:{'是' if v > 0 else '否'}" for k, v in labels.items())
    return ScanResult(
        code=row.code, name=row.name, market=row.market,
        price=float(last["close"]),
        pct=float((last["close"] / df.iloc[-2]["close"] - 1) * 100),
        vol=float(last["vol"]), amount=float(last["amount"]),
        indicators={k: (None if pd.isna(v) else round(float(v), 4))
                    for k, v in last.items()
                    if k not in ("date", "open", "high", "low", "close", "vol", "amount")},
        match_reason=reason or "条件成立",
        strategy_id=int(strategy_id or 0),
        direction=direction or "",
        signal_date=signal_date or "")


# ---------------------------------------------------------------------------
# 日K本地缓存
# ---------------------------------------------------------------------------
class CachedFeed:
    """带 SQLite 日K缓存的行情源包装：命中缓存免网络，未命中拉取并落盘。"""

    def __init__(self, feed: DataFeed, db):
        self.feed = feed
        self.db = db
        self.last_source = "none"  # cache / network / cache_stale / none
        self.last_date = None
        self.last_adjust = None    # qfq / none（v0.7.0 起日K为前复权口径）

    def get_daily_bars(self, code, market=None, count=300, force_refresh=False,
                       as_of_date=None):
        """获取日K，优先走本地缓存；缓存过期或强制刷新时从行情服务器拉取。

        as_of_date（YYYY-MM-DD，v0.7.3 历史日期扫描）：本次所需数据的最新日期。
        历史日期扫描时，缓存只要覆盖到 as_of_date 即可复用——此前按"今天/最近
        交易日"判定，会把不含今天的历史缓存全部误判过期，导致全市场重拉（很慢）。

        返回的 DataFrame 与之前保持一致；调用方如需判断本次是否命中缓存，
        可使用本类的 last_source / last_date 属性；复权状态见 last_adjust。
        """
        cached = self.db.get_cached_daily_bars(code)
        today = datetime.date.today()
        today_str = today.isoformat()
        usable = False
        stale_days = None
        if not cached.empty:
            last_date = str(cached["date"].iloc[-1])
            try:
                ld = datetime.date.fromisoformat(last_date)
                if as_of_date:
                    # 历史日期扫描：缓存覆盖到目标日期即可复用（不需要今天的数据）
                    usable = last_date >= as_of_date
                    stale_days = (today - ld).days
                else:
                    days = (today - ld).days
                    stale_days = days
                    # 盘中：缓存必须含今天（当日实时数据）
                    # 盘后/盘前/周末：缓存最后日期 >= 最近交易日即可用。
                    # 最近交易日跳过周末（否则周一盘前/周末扫描会把周五的缓存全部误判
                    # 过期而全市场重拉，v0.7.2 修复）。
                    if is_trading_time():
                        usable = last_date >= today_str
                    else:
                        d = today
                        while d.weekday() >= 5:  # 跳过周六日
                            d -= datetime.timedelta(days=1)
                        usable = last_date >= d.isoformat()
            except ValueError:
                usable = False

        if usable and not force_refresh:
            self.last_source = "cache"
            self.last_date = str(cached["date"].iloc[-1])
            self.last_adjust = "qfq"   # v0.7.0 起缓存统一为前复权口径
            if len(cached) >= count:
                return cached.tail(count).reset_index(drop=True)
            return cached

        # 缓存不可用或强制刷新：尝试网络拉取
        bars = None
        try:
            bars = self.feed.get_daily_bars(code, market=market, count=count)
        except Exception as e:  # noqa: BLE001
            log.warning("拉取 %s 日K失败: %s", code, e)

        if bars is not None and len(bars):
            self.last_adjust = getattr(self.feed, "last_adjust", "none")
            if self.last_adjust != "qfq":
                # 本次未拿到前复权数据（除权事件拉取失败降级）：不写缓存，
                # 避免把不复权数据当作 qfq 口径缓存污染后续扫描
                log.warning("%s 未获得前复权数据(last_adjust=%s)，跳过缓存写入",
                            code, self.last_adjust)
            else:
                try:
                    self.db.cache_daily_bars(code, market, bars)
                except Exception as e:  # noqa: BLE001
                    log.debug("缓存 %s 失败: %s", code, e)
            net_last = str(bars["date"].iloc[-1])
            # 网络数据不应比本地缓存还旧（公开服务器常滞后于券商/本地），
            # 若更旧则保留缓存，避免覆盖掉更新的数据。
            cache_last = str(cached["date"].iloc[-1]) if not cached.empty else None
            if cache_last and net_last < cache_last:
                log.warning("%s 网络数据(%s) 比本地缓存(%s) 旧，保留本地缓存",
                            code, net_last, cache_last)
                self.last_source = "cache"
                self.last_date = cache_last
                if len(cached) >= count:
                    return cached.tail(count).reset_index(drop=True)
                return cached
            self.last_source = "network"
            self.last_date = net_last
            return bars

        # 网络失败但缓存不是极度陈旧（<=5天）时回退，避免盘中断网后完全无法诊断
        if not cached.empty and stale_days is not None and stale_days <= 5:
            log.warning("%s 网络取数失败，回退到 %s 的缓存（滞后 %d 天）",
                        code, cached["date"].iloc[-1], stale_days)
            self.last_source = "cache_stale"
            self.last_date = str(cached["date"].iloc[-1])
            if len(cached) >= count:
                return cached.tail(count).reset_index(drop=True)
            return cached

        self.last_source = "none"
        self.last_date = None
        return bars


# ---------------------------------------------------------------------------
# 全市场并行深度扫描
# ---------------------------------------------------------------------------
def multi_scan_formula(feed, stock_df, formulas,
                    quote_filter=None, max_stocks=0, bars_count=300,
                    start_date=None, end_date=None,
                    progress=None, cancelled=None):
    """一次全市场扫描，同时执行多个公式策略（v0.8.11）。

    formulas: [(strategy_id, strategy_name, formula), ...] —— 只含"公式类"策略
              （DNA/indicator 策略不在这里）。
    返回 {strategy_id: [ScanResult, ...]}（按策略分组，命中股票可能同时属于多个策略）。

    性能优化：每只股票只 拉一次K线 + 算一次指标（compute_indicators），
    然后对该票的指标数据逐个公式求值——相比每个策略各自扫全市场，
    K线拉取与指标计算从 N×全市场 降到 1×全市场。

    语义与 scan_formula 保持一致：
    - quote_filter 实时行情预过滤（近涨停/跌停剔除），max_stocks 控制候选数
    - start_date/end_date 历史日期扫描（逐日判断，signal_date 记录）
    - 股票在信号日无数据自动跳过
    """
    from indicators import compute_indicators, formula_signal

    if not formulas:
        return {}
    # 过滤掉空公式
    valid = [(sid, name, f) for sid, name, f in formulas if f and f.strip()]
    if not valid:
        return {}

    candidates = stock_df
    if quote_filter is not None and not quote_filter.empty:
        qf = quote_filter.set_index("code")
        keep = [c for c in ("price", "pct", "vol", "amount") if c in qf.columns]
        qf = qf[keep]
        merged = candidates.merge(qf, left_on="code", right_index=True, how="inner")
        merged = merged[(merged["pct"] >= -9.9) & (merged["pct"] <= 9.9)]
        merged = merged.sort_values("pct", ascending=False)
        candidates = merged.head(max_stocks if max_stocks > 0 else len(merged)).reset_index(drop=True)
    elif max_stocks and max_stocks > 0:
        candidates = candidates.head(max_stocks)

    total = len(candidates)
    # 每个策略独立的结果桶
    buckets: dict = {sid: [] for sid, _, _ in valid}
    start = datetime.datetime.now()
    for i, row in enumerate(candidates.itertuples()):
        if cancelled and cancelled():
            log.info("多策略扫描被取消")
            break
        if progress:
            progress(i, total, row.name)
        try:
            if end_date and isinstance(feed, CachedFeed):
                bars = feed.get_daily_bars(row.code, market=row.market,
                                           count=bars_count, as_of_date=end_date)
            else:
                bars = feed.get_daily_bars(row.code, market=row.market,
                                           count=bars_count)
            if bars is None or len(bars) < 30:
                continue
            if end_date:
                bars = bars[bars["date"] <= end_date].reset_index(drop=True)
                if len(bars) < 30:
                    continue
            df = compute_indicators(bars)
            if start_date and end_date:
                # 历史范围扫描：逐日判断
                days = [d for d in df["date"].astype(str).tolist()
                        if start_date <= d <= end_date]
                for sid, name, formula in valid:
                    for d in days:
                        if cancelled and cancelled():
                            break
                        df_t = df[df["date"] <= d]
                        if df_t.empty or str(df_t["date"].iloc[-1]) != d:
                            continue
                        try:
                            hit, labels = formula_signal(df_t, formula)
                        except Exception:
                            continue
                        if hit:
                            buckets[sid].append(_make_scan_result(
                                row, df_t, labels, d, "", sid))
            else:
                for sid, name, formula in valid:
                    try:
                        hit, labels = formula_signal(df, formula)
                    except Exception:
                        continue
                    if hit:
                        buckets[sid].append(_make_scan_result(
                            row, df, labels, None, "", sid))
        except Exception as e:  # noqa: BLE001
            log.debug("多策略扫描 %s 失败: %s", row.code, e)
    log.info("多策略扫描完成：%d 个策略，共 %d 只命中，耗时 %.1fs",
             len(valid), sum(len(v) for v in buckets.values()),
             (datetime.datetime.now() - start).total_seconds())
    return buckets


def parallel_multi_scan_formula(stock_df, formulas, db, workers=4, bars_count=300,
                                start_date=None, end_date=None,
                                progress=None, cancelled=None) -> dict:
    """多线程并行多策略扫描全市场（v0.8.11）。

    每线程独立 pytdx 连接 + 共享日K缓存；按股票分块，每块内逐票×多公式。
    返回 {strategy_id: [ScanResult, ...]}。
    """
    from concurrent.futures import ThreadPoolExecutor
    if stock_df is None or stock_df.empty:
        return {}
    if not formulas:
        return {}
    total = len(stock_df)
    workers = max(1, workers)
    chunks = [stock_df.iloc[i::workers] for i in range(workers)]
    lock = threading.Lock()
    done = {"n": 0}

    def _progress_cb(_i, _total, name):
        with lock:
            done["n"] += 1
        if progress and done["n"] % 5 == 0:
            progress(min(done["n"], total), total, name)

    def _scan_chunk(chunk):
        if cancelled and cancelled():
            return {}
        feed = CachedFeed(DataFeed(db=db), db)
        try:
            return multi_scan_formula(
                feed, chunk.reset_index(drop=True), formulas,
                max_stocks=len(chunk), bars_count=bars_count,
                start_date=start_date, end_date=end_date,
                progress=_progress_cb, cancelled=cancelled)
        finally:
            feed.feed.disconnect()

    merged: dict = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for part in ex.map(_scan_chunk, chunks):
            for sid, hits in part.items():
                merged.setdefault(sid, []).extend(hits)
    log.info("并行多策略扫描完成：%d 策略，耗时已完成", len(formulas))
    return merged


def parallel_scan_formula(stock_df: pd.DataFrame, formula: str, db, workers=4,
                          bars_count=300, start_date=None, end_date=None, direction="",
                          strategy_id=0,
                          progress: ProgressCb = None,
                          cancelled: CancelFlag = None):
    """多线程并行扫描全市场（每线程独立 pytdx 连接 + 共享日K缓存）。
    start_date/end_date: 历史日期扫描范围（v0.7.3），None=最新K线。
    v0.8.1：strategy_id 透传到 ScanResult；direction 已废弃但保留兼容。"""
    if stock_df is None or stock_df.empty:
        return []
    total = len(stock_df)
    chunks = [stock_df.iloc[i::workers] for i in range(workers)]
    results = []
    lock = threading.Lock()
    done = {"n": 0}

    def _progress_cb(_i, _total, name):
        with lock:
            done["n"] += 1
        if progress and done["n"] % 5 == 0:
            progress(min(done["n"], total), total, name)

    def _scan_chunk(chunk):
        if cancelled and cancelled():
            return []
        # v0.7.1：DataFeed 传入 db —— 除权事件走缓存，避免每票重复拉取 xdxr
        feed = CachedFeed(DataFeed(db=db), db)
        try:
            return scan_formula(feed, chunk.reset_index(drop=True), formula,
                                max_stocks=len(chunk), bars_count=bars_count,
                                start_date=start_date, end_date=end_date,
                                direction=direction, strategy_id=strategy_id,
                                progress=_progress_cb, cancelled=cancelled)
        finally:
            feed.feed.disconnect()

    start = datetime.datetime.now()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for part in ex.map(_scan_chunk, chunks):
            results.extend(part)
    log.info("并行扫描完成：%d/%d 只命中，耗时 %.1fs",
             len(results), total, (datetime.datetime.now() - start).total_seconds())
    return results


# ---------------------------------------------------------------------------
# 二层 DNA 精筛（v0.7.0）
# ---------------------------------------------------------------------------
def dna_screen(feed, candidates, template_names, threshold=0.0,
               window_mode="template_len", length_scan=True, fast=False,
               workers=1, as_of_date=None,
               progress: ProgressCb = None, cancelled: CancelFlag = None):
    """DNA 精筛：对候选列表逐票提取三维特征并与模具库比对，输出打分排行榜。

    candidates: 可迭代的 {code,name,market}（ScanResult 或 dict 均可）
    template_names: 参与比对的模具名列表（来自精筛策略配置）
    threshold: 分数阈值（>0 时低于阈值的候选被过滤）
    window_mode: "template_len" 按模具长度 / "fixed" 固定25天
    length_scan: 长度弹性扫描（默认开）
    fast: 快速模式（全市场直测：单档、不做长度弹性）
    workers: 并行线程数（v0.7.1；全市场直测建议 >=4，每线程独立行情连接+共享缓存）
    as_of_date: 历史日期精筛（v0.7.3）——候选特征窗口末端锚定该日（YYYY-MM-DD）；
                None=锚定最新交易日。signal_date 写入结果。
    返回按分数降序的 ScanResult 列表（dna_score/dna_template/dna_detail 已填充）。
    """
    from core.dna_engine import (DNAFeatureExtractor, DNAPatternMatcher,
                                 FEATURE_VERSION, MIN_WINDOW, TemplateManager)

    tm = TemplateManager()
    names = [n for n in template_names if n in tm.templates
             and tm.metadata.get(n, {}).get("_feature_version") == FEATURE_VERSION]
    if not names:
        skipped = [n for n in template_names if n in tm.templates]
        raise ValueError(
            "配置的模具均为旧版特征口径（feature_version≠%d），"
            "请在策略管理页「🧬 DNA模具」中删除后重新制作" % FEATURE_VERSION
            if skipped else
            "模具库为空或配置的模具不存在，请先在策略管理页制作模具")
    matcher = DNAPatternMatcher(tm, window_mode=window_mode,
                                length_scan=(length_scan and not fast))

    items = []
    for c in candidates:
        if isinstance(c, ScanResult):
            items.append((c.code, c.name, c.market))
        else:
            items.append((c["code"], c.get("name", ""), c.get("market")))
    total = len(items)
    if total == 0:
        return []

    # 小候选集（如粗筛后精筛）单线程即可；大批量（全市场直测）多线程并行
    if workers <= 1 or total < 100:
        results = _dna_screen_chunk(feed, items, names, matcher, tm,
                                    threshold, as_of_date, progress, cancelled)
    else:
        lock = threading.Lock()
        done = {"n": 0}

        def _progress(_i, _total, name):
            with lock:
                done["n"] += 1
            if progress and done["n"] % 5 == 0:
                progress(min(done["n"], total), total, name)

        chunks = [items[i::workers] for i in range(workers)]

        def _run_chunk(chunk):
            if cancelled and cancelled():
                return []
            # 每线程独立行情连接（DataFeed 带 db：除权事件共享缓存）
            tfeed = CachedFeed(DataFeed(db=getattr(feed, "db", None)),
                               getattr(feed, "db", None))
            try:
                return _dna_screen_chunk(tfeed, chunk, names, matcher, tm,
                                         threshold, as_of_date, _progress, cancelled)
            finally:
                tfeed.feed.disconnect()

        results = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for part in ex.map(_run_chunk, chunks):
                results.extend(part)

    results.sort(key=lambda r: (r.dna_score or 0), reverse=True)
    log.info("DNA 精筛完成：%d/%d 只通过（%d 线程）",
             len(results), total, max(1, workers if total >= 100 else 1))
    return results


def _dna_screen_chunk(feed, items, names, matcher, tm, threshold, as_of_date=None,
                      progress=None, cancelled=None):
    """dna_screen 的单线程分片主体（多线程模式下由各线程独立执行）。"""
    from core.dna_engine import DNAFeatureExtractor, MIN_WINDOW
    results = []
    total = len(items)
    for i, (code, name, market) in enumerate(items):
        if cancelled and cancelled():
            log.info("DNA 精筛被取消")
            break
        if progress:
            progress(i, total, name)
        try:
            if as_of_date and isinstance(feed, CachedFeed):
                bars = feed.get_daily_bars(code, market=market, count=300,
                                           as_of_date=as_of_date)
            else:
                bars = feed.get_daily_bars(code, market=market, count=300)
            if bars is None or len(bars) < MIN_WINDOW + 5:
                continue
            if as_of_date:
                # 历史日期精筛：窗口末端锚定指定日
                bars = bars[bars["date"] <= as_of_date].reset_index(drop=True)
                if len(bars) < MIN_WINDOW + 5:
                    continue
                if str(bars["date"].iloc[-1]) != as_of_date:
                    continue  # 该日无数据（停牌等）
            fdf = DNAFeatureExtractor.calculate_indicators(bars)
            feat = DNAFeatureExtractor.extract_matrix(fdf)
            dates = bars["date"].astype(str).tolist()
            last = bars.iloc[-1]
            best = None
            all_t = {}
            for t_name in names:
                r = matcher.match_candidate(feat, tm.templates[t_name], dates)
                all_t[t_name] = r["score"]
                if best is None or r["score"] > best["score"]:
                    best = {**r, "template": t_name}
            if best is None or best["score"] <= 0:
                continue
            if threshold and best["score"] < threshold:
                continue
            detail = {
                "score": best["score"], "template": best["template"],
                "len": best["best_len"], "start": best["best_start"],
                "end": best["best_end"], "all": all_t,
            }
            results.append(ScanResult(
                code=code, name=name, market=market,
                price=float(last["close"]),
                pct=float((last["close"] / bars.iloc[-2]["close"] - 1) * 100),
                vol=float(last["vol"]), amount=float(last["amount"]),
                match_reason=(f"DNA[{best['template']}] {best['score']}分 "
                              f"({best['best_len']}天 "
                              f"{best.get('best_start')}~{best.get('best_end')})"),
                dna_score=best["score"], dna_template=best["template"],
                dna_detail=json.dumps(detail, ensure_ascii=False),
                signal_date=as_of_date or ""))
        except Exception as e:  # noqa: BLE001
            log.debug("DNA 精筛 %s 失败: %s", code, e)
    return results


# ---------------------------------------------------------------------------
# 二层精筛（v0.7.4）：DTW骨架分 + 微观规则综合分
# ---------------------------------------------------------------------------
def layer2_screen(feed, candidates, template_names, threshold=0.0, top_n=None,
                  window_mode="template_len", length_scan=True, fast=False,
                  workers=1, as_of_date=None, template_manager=None,
                  progress: ProgressCb = None, cancelled: CancelFlag = None):
    """二层精筛完整链路：DNA 骨架匹配 + 微观规则评分，输出综合分排行榜。

    candidates: 可迭代的 {code,name,market}（ScanResult 或 dict 均可）
    template_names: 参与比对的模具名列表（来自精筛策略配置或模具库全量）
    threshold: 综合分过滤阈值（final_score >= threshold 才保留；0=不过滤）
    top_n: 兜底条数——阈值过滤后不足 top_n 条时，按综合分取前 top_n 补齐
           （None=不兜底）。自动串联场景用 threshold=75, top_n=15。
    window_mode / length_scan / fast / workers / as_of_date: 同 dna_screen
    template_manager: 可选模具库实例（默认加载 dna_storage/，测试可注入临时库）
    返回按 final_score 降序的 ScanResult 列表（dna_score=骨架分，
    final_score=综合分，buy_level/signals 已填充，dna_detail 含微观明细）。
    """
    from core.dna_engine import (DNAFeatureExtractor, DNAPatternMatcher,
                                 FEATURE_VERSION, MIN_WINDOW, TemplateManager)
    from core.micro_rules import MicroRuleEvaluator

    tm = template_manager or TemplateManager()
    names = [n for n in template_names if n in tm.templates
             and tm.metadata.get(n, {}).get("_feature_version") == FEATURE_VERSION]
    if not names:
        skipped = [n for n in template_names if n in tm.templates]
        raise ValueError(
            "配置的模具均为旧版特征口径（feature_version≠%d），"
            "请在策略管理页「🧬 DNA模具」中删除后重新制作" % FEATURE_VERSION
            if skipped else
            "模具库为空或配置的模具不存在，请先在策略管理页制作模具")
    matcher = DNAPatternMatcher(tm, window_mode=window_mode,
                                length_scan=(length_scan and not fast))
    mre = MicroRuleEvaluator()

    items = []
    for c in candidates:
        if isinstance(c, ScanResult):
            items.append((c.code, c.name, c.market))
        else:
            items.append((c["code"], c.get("name", ""), c.get("market")))
    total = len(items)
    if total == 0:
        return []

    # 与 dna_screen 相同的并行编排：小候选集单线程，大批量多线程
    if workers <= 1 or total < 100:
        scored = _layer2_chunk(feed, items, names, matcher, tm, mre,
                               as_of_date, progress, cancelled)
    else:
        lock = threading.Lock()
        done = {"n": 0}

        def _progress(_i, _total, name):
            with lock:
                done["n"] += 1
            if progress and done["n"] % 5 == 0:
                progress(min(done["n"], total), total, name)

        chunks = [items[i::workers] for i in range(workers)]

        def _run_chunk(chunk):
            if cancelled and cancelled():
                return []
            tfeed = CachedFeed(DataFeed(db=getattr(feed, "db", None)),
                               getattr(feed, "db", None))
            try:
                return _layer2_chunk(tfeed, chunk, names, matcher, tm, mre,
                                     as_of_date, _progress, cancelled)
            finally:
                tfeed.feed.disconnect()

        scored = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for part in ex.map(_run_chunk, chunks):
                scored.extend(part)

    # 综合分阈值过滤 + TopN 兜底
    scored.sort(key=lambda r: (r.final_score if r.final_score is not None else 0),
                reverse=True)
    if threshold and threshold > 0:
        kept = [r for r in scored if (r.final_score or 0) >= threshold]
        if top_n and len(kept) < top_n:
            have = {r.code for r in kept}
            for r in scored:
                if len(kept) >= top_n:
                    break
                if r.code not in have:
                    kept.append(r)
                    have.add(r.code)
    else:
        kept = scored

    log.info("二层精筛（DNA+微观）完成：%d/%d 只通过（综合分≥%s%s）",
             len(kept), total, threshold if threshold else 0,
             f" 或 Top{top_n}" if (threshold and top_n) else "")
    return kept


def _layer2_chunk(feed, items, names, matcher, tm, mre, as_of_date=None,
                  progress=None, cancelled=None):
    """layer2_screen 的单线程分片主体。"""
    import json as _json
    from core.dna_engine import DNAFeatureExtractor, MIN_WINDOW
    results = []
    total = len(items)
    for i, (code, name, market) in enumerate(items):
        if cancelled and cancelled():
            log.info("二层精筛被取消")
            break
        if progress:
            progress(i, total, name)
        try:
            if as_of_date and isinstance(feed, CachedFeed):
                bars = feed.get_daily_bars(code, market=market, count=300,
                                           as_of_date=as_of_date)
            else:
                bars = feed.get_daily_bars(code, market=market, count=300)
            if bars is None or len(bars) < MIN_WINDOW + 5:
                continue
            if as_of_date:
                bars = bars[bars["date"] <= as_of_date].reset_index(drop=True)
                if len(bars) < MIN_WINDOW + 5:
                    continue
                if str(bars["date"].iloc[-1]) != as_of_date:
                    continue  # 该日无数据（停牌等）
            fdf = DNAFeatureExtractor.calculate_indicators(bars)
            feat = DNAFeatureExtractor.extract_matrix(fdf)
            dates = bars["date"].astype(str).tolist()
            last = bars.iloc[-1]
            best = None
            all_t = {}
            for t_name in names:
                r = matcher.match_candidate(feat, tm.templates[t_name], dates)
                all_t[t_name] = r["score"]
                if best is None or r["score"] > best["score"]:
                    best = {**r, "template": t_name}
            if best is None or best["score"] <= 0:
                continue
            dtw_score = float(best["score"])
            # 微观规则：加减分 + 买点定级 + 信号
            mr = mre.evaluate(bars, fdf, best)
            modifier = mr["score_modifier"]
            final_score = float(np.clip(dtw_score + modifier, 0, 100))
            signals_str = "；".join(mr["signals"]) or ""
            buy_level = mr["buy_level"]
            detail = {
                "score": dtw_score, "template": best["template"],
                "len": best["best_len"], "start": best["best_start"],
                "end": best["best_end"], "all": all_t,
                "final_score": final_score, "modifier": modifier,
                "buy_level": buy_level, "signals": mr["signals"],
            }
            reason_bits = [f"DNA[{best['template']}] {dtw_score}分",
                           f"综合{final_score}分"]
            if buy_level:
                reason_bits.append(buy_level)
            if signals_str:
                reason_bits.append(signals_str)
            results.append(ScanResult(
                code=code, name=name, market=market,
                price=float(last["close"]),
                pct=float((last["close"] / bars.iloc[-2]["close"] - 1) * 100),
                vol=float(last["vol"]), amount=float(last["amount"]),
                match_reason="｜".join(reason_bits),
                dna_score=dtw_score, dna_template=best["template"],
                dna_detail=_json.dumps(detail, ensure_ascii=False),
                final_score=final_score,
                buy_level=buy_level,
                signals=signals_str,
                signal_date=as_of_date or ""))
        except Exception as e:  # noqa: BLE001
            log.debug("二层精筛 %s 失败: %s", code, e)
    return results


# ---------------------------------------------------------------------------
# 5日均量预热（量比计算）
# v0.6.6 起移除：实时行情页签已替换为行情日历，量比/vol5 不再使用。
# 原 warm_vol5 / get_vol5 代码已删除（记录见 PROJECT_LOG.md v0.6.6）。
# ---------------------------------------------------------------------------


