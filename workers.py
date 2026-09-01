# -*- coding: utf-8 -*-
"""后台工作线程：所有 pytdx 网络请求与扫描/提醒检查都在这里执行，避免阻塞界面。"""
import logging
import queue
import threading
import uuid

import pandas as pd
from PyQt5.QtCore import QThread, pyqtSignal

from alert_engine import check_bar_alerts, check_quote_alerts
from database import Database
from data_feed import DataFeed, infer_market
from indicators import compute_indicators
from push import send_push
from scanner import (CachedFeed, ScanResult, layer2_screen, parallel_scan_formula,
                     parallel_multi_scan_formula, scan_formula, strategy_formula)

log = logging.getLogger("worker")


class Worker(QThread):
    status = pyqtSignal(str)
    quotes_ready = pyqtSignal(object)              # 观察池/提醒标的行情 df
    market_quotes_ready = pyqtSignal(object)       # 全市场行情 df
    bars_ready = pyqtSignal(str, str, object, str)   # code, name, df, source
    list_ready = pyqtSignal(object)                # 全市场股票列表 df
    alerts_fired = pyqtSignal(list)                # 触发提醒事件
    scan_progress = pyqtSignal(int, int, str)
    scan_done = pyqtSignal(object)                 # {"results","new_hits","session_id",...}
    scan_error = pyqtSignal(str)

    def __init__(self, feed: DataFeed, db: Database, parent=None):
        super().__init__(parent)
        self.feed = feed
        self.db = db
        self._q = queue.Queue()
        self._stop = threading.Event()
        self._scan_cancel = threading.Event()
        self._scan_lock = threading.Lock()
        self._scanning = False       # v0.7.3：是否有扫描在运行（防重复点击堆积任务）
        self._stock_df = None

    # ------------------------------------------------------------------ API
    def is_scan_running(self):
        """是否有扫描任务正在执行（strategy_scan / auto_scan / dna_scan）。"""
        with self._scan_lock:
            return self._scanning

    def _set_scanning(self, running: bool):
        with self._scan_lock:
            self._scanning = running

    def stop(self):
        self._stop.set()
        self._q.put(("__exit__", None))

    def cancel_scan(self):
        self._scan_cancel.set()

    def request_stock_list(self):
        self._q.put(("stock_list", None))

    def request_quotes(self, codes):
        self._q.put(("quotes", codes))

    def request_market_quotes(self, codes, save=True):
        self._q.put(("market_quotes", {"codes": codes, "save": save}))

    def request_bars(self, code, name, market, count=300, source=""):
        self._q.put(("bars", {"code": code, "name": name, "market": market,
                              "count": count, "source": source}))

    def request_strategy_scan(self, strategy, mode, stock_df, quote_filter=None,
                              max_stocks=100, parallel=4, bars_count=300,
                              start_date=None, end_date=None):
        """按策略扫描。mode: batch(全市场并行) / intraday(盘中重点范围) / manual(单策略)
        start_date/end_date: 历史日期扫描范围（v0.7.3），None=最新K线。
        返回 False 表示已有扫描在运行（拒绝重复提交）。"""
        if self.is_scan_running():
            return False
        self._scan_cancel.clear()
        self._q.put(("strategy_scan", {"strategy": strategy, "mode": mode,
                                       "stock_df": stock_df, "quote_filter": quote_filter,
                                       "max_stocks": max_stocks, "parallel": parallel,
                                       "bars_count": bars_count,
                                       "start_date": start_date, "end_date": end_date}))
        return True

    def request_auto_scan(self, strategies, mode, stock_df, quote_filter=None,
                          max_stocks=100, parallel=4, bars_count=300,
                          start_date=None, end_date=None):
        """批量自动扫描（盘前盘后/盘中）：依次执行多个启用策略。
        返回 False 表示已有扫描在运行（拒绝重复提交）。"""
        if self.is_scan_running():
            return False
        self._scan_cancel.clear()
        self._q.put(("auto_scan", {"strategies": strategies, "mode": mode,
                                   "stock_df": stock_df, "quote_filter": quote_filter,
                                   "max_stocks": max_stocks, "parallel": parallel,
                                   "bars_count": bars_count,
                                   "start_date": start_date, "end_date": end_date}))
        return True

    def request_dna_scan(self, strategy, candidates, mode="dna_manual", parallel=1,
                         as_of_date=None):
        """二层DNA精筛：对指定候选列表（如最近一次粗筛结果）直接精筛，不重跑粗筛。
        as_of_date: 历史日期精筛（v0.7.3），None=最新交易日。
        返回 False 表示已有扫描在运行（拒绝重复提交）。"""
        if self.is_scan_running():
            return False
        self._scan_cancel.clear()
        self._q.put(("dna_scan", {"strategy": strategy, "candidates": candidates,
                                  "mode": mode, "parallel": parallel,
                                  "as_of_date": as_of_date}))
        return True

    def get_stock_df(self):
        return self._stock_df

    # ------------------------------------------------------------------ 主循环
    def run(self):
        while not self._stop.is_set():
            try:
                task, payload = self._q.get(timeout=0.3)
            except queue.Empty:
                continue
            if task == "__exit__":
                break
            try:
                self._handle(task, payload)
            except Exception as e:  # noqa: BLE001
                log.exception("任务 %s 失败", task)
                if task in ("strategy_scan", "auto_scan", "dna_scan"):
                    self.scan_error.emit(str(e))
                else:
                    self.status.emit(f"错误: {e}")
        self.feed.disconnect()

    def _handle(self, task, payload):
        if task == "stock_list":
            try:
                df = self.feed.get_stock_list(refresh=True)
            except Exception as e:  # noqa: BLE001
                log.warning("在线获取股票列表失败，使用本地缓存: %s", e)
                cached = self.db.load_stock_list()
                if not cached:
                    raise
                df = pd.DataFrame(cached)
            self._stock_df = df
            self.db.save_stock_list(df)
            self.list_ready.emit(df)
            self.status.emit(f"股票列表加载完成：{len(df)} 只")

        elif task == "quotes":
            df = self.feed.get_quotes(payload)
            self.quotes_ready.emit(df)
            self._check_quotes_alerts(df)

        elif task == "market_quotes":
            codes = payload["codes"]
            df = self.feed.get_quotes(codes)
            if payload.get("save"):
                self.db.save_market_snapshot(df)
            self.market_quotes_ready.emit(df)
            self.status.emit(f"全市场行情刷新 {len(df)} 条 @ {self.feed.connected_ip}")

        elif task == "bars":
            df = self.feed.get_daily_bars(payload["code"], market=payload["market"],
                                          count=payload["count"])
            self.bars_ready.emit(payload["code"], payload["name"], df, payload.get("source", ""))

        elif task == "strategy_scan":
            self._set_scanning(True)
            try:
                results = self._run_strategy_scan(payload) or []
                # v0.7.4：单策略批量扫描完成后自动串联二层精筛
                if results and payload.get("mode") == "batch":
                    self._chain_layer2(results, payload)
            finally:
                self._set_scanning(False)

        elif task == "dna_scan":
            # 选股模块下区：对指定候选列表精筛（不重跑粗筛）
            self._set_scanning(True)
            try:
                p = payload
                self._run_dna_strategy(
                    p["strategy"],
                    {**p, "stock_df": None, "bars_count": 300},
                    uuid.uuid4().hex[:12], p.get("mode", "dna_manual"))
            finally:
                self._set_scanning(False)

        elif task == "auto_scan":
            # v0.8.11 重做：公式类策略合并成一次全市场扫描
            # 关键: 一次全市场遍历, 每只票只拉一次K线/算一次指标,
            # 对该票的指标数据逐公式求值, 返回 {strategy_id: [ScanResult]}
            self._set_scanning(True)
            try:
                all_hits = []
                mode = payload["mode"]
                formula_strategies = [s for s in payload["strategies"]
                                      if s.get("kind") != "dna"]
                dna_strategies = [s for s in payload["strategies"]
                                  if s.get("kind") == "dna"]
                if formula_strategies and not self._scan_cancel.is_set():
                    formulas = []
                    for s in formula_strategies:
                        f = s.get("formula") or ""
                        if f and f.strip():
                            formulas.append((s.get("id"), s.get("name"), f))
                    if formulas:
                        self.status.emit(
                            f"⚡ 全市场扫描一次, 同时执行 {len(formulas)} 个策略…")
                        if mode == "batch":
                            buckets = parallel_multi_scan_formula(
                                payload["stock_df"], formulas, self.db,
                                workers=payload["parallel"],
                                bars_count=payload.get("bars_count", 300),
                                start_date=payload.get("start_date"),
                                end_date=payload.get("end_date"),
                                progress=self._scan_progress,
                                cancelled=self._scan_cancel.is_set)
                        else:  # intraday / manual: 重点范围(行情预过滤)
                            from scanner import multi_scan_formula
                            buckets = multi_scan_formula(
                                self.feed, payload["stock_df"], formulas,
                                max_stocks=payload["max_stocks"],
                                quote_filter=payload["quote_filter"],
                                bars_count=payload.get("bars_count", 300),
                                start_date=payload.get("start_date"),
                                end_date=payload.get("end_date"),
                                progress=self._scan_progress,
                                cancelled=self._scan_cancel.is_set)
                        for st in formula_strategies:
                            if self._scan_cancel.is_set():
                                break
                            results = buckets.get(st.get("id"), [])
                            if not results:
                                continue
                            session_id = uuid.uuid4().hex[:12]
                            hits = self._finalize_strategy_hits(
                                st, results, session_id, mode, payload)
                            all_hits.extend(hits)
                # DNA 策略保留原路径(每个独立执行, 全市场或重点范围)
                for st in dna_strategies:
                    if self._scan_cancel.is_set():
                        break
                    self.status.emit(f"正在执行策略: {st['name']}")
                    hits = self._run_strategy_scan({
                        "strategy": st, "mode": mode,
                        "stock_df": payload["stock_df"],
                        "quote_filter": payload["quote_filter"],
                        "max_stocks": payload["max_stocks"],
                        "parallel": payload["parallel"],
                        "bars_count": payload.get("bars_count", 300),
                        "start_date": payload.get("start_date"),
                        "end_date": payload.get("end_date"),
                    }) or []
                    all_hits.extend(hits)
                self._chain_layer2(all_hits, payload)
                self.status.emit("自动扫描全部完成")
            finally:
                self._set_scanning(False)

    # ------------------------------------------------------------ 策略扫描
    def _run_strategy_scan(self, p):
        """执行单策略扫描（一层粗筛）；返回本次命中的 ScanResult 列表
        （DNA 精筛策略返回空列表，其结果是 layer=2 不走粗筛返回）。
        v0.8.1：废弃 direction 字段；候选流转由 parent_strategy_ids 链式控制。
        """
        strategy = p["strategy"]
        session_id = uuid.uuid4().hex[:12]
        mode = p["mode"]
        level = int(strategy.get("level", 1) or 1)
        # v0.8.1：解析 parent_strategy_ids（list[str|int]）
        psids = self._parse_parent_strategy_ids(strategy)
        if strategy.get("kind") == "dna":
            self._run_dna_strategy(strategy, p, session_id, mode)
            return []
        formula = strategy_formula(strategy)
        if not formula:
            raise ValueError(f"策略「{strategy.get('name', '')}」没有可执行的公式")
        if mode == "batch":
            results = parallel_scan_formula(
                p["stock_df"], formula, self.db, workers=p["parallel"],
                bars_count=p["bars_count"],
                start_date=p.get("start_date"), end_date=p.get("end_date"),
                progress=self._scan_progress, cancelled=self._scan_cancel.is_set)
        else:  # intraday / manual：重点范围（行情预过滤）
            results = scan_formula(
                self.feed, p["stock_df"], formula, max_stocks=p["max_stocks"],
                quote_filter=p["quote_filter"], bars_count=p["bars_count"],
                start_date=p.get("start_date"), end_date=p.get("end_date"),
                progress=self._scan_progress, cancelled=self._scan_cancel.is_set)

        if results:
            self.db.add_scan_hits(session_id, strategy.get("id"),
                                   strategy.get("name"), results,
                                   layer=level, parent_strategy_ids=psids)

        # 新命中 = 本次结果中，当日该策略此前未命中过的股票
        prev = {x["code"] for x in self.db.hits_today(session_exclude=session_id)
                if x.get("strategy_id") == strategy.get("id")}
        new_hits = [h for h in results if h.code not in prev]
        # v0.7.3：历史日期对照扫描不自动进池（信号已错过，进池无意义）
        is_hist = self._is_historical_scan(p.get("end_date"))
        if not is_hist:
            # v0.8.12：add_date = 用户选的 end_date (缺省今天) -> 全部日期对齐
            import datetime as _dt
            _signal_date = p.get("end_date") or _dt.date.today().isoformat()
            self._auto_add_to_watch(new_hits, strategy, level=level,
                                     add_date=_signal_date)

        self.scan_done.emit({
            "results": results, "new_hits": new_hits, "session_id": session_id,
            "strategy_name": strategy.get("name"), "mode": mode,
            "historical": is_hist, "level": level,
            "parent_strategy_ids": psids,
        })
        self.status.emit(
            f"策略「{strategy.get('name')}」扫描完成：命中 {len(results)} 只，"
            f"其中新信号 {len(new_hits)} 只"
            + ("（历史日期对照，未自动进池）" if is_hist else ""))
        return results

    def _parse_parent_strategy_ids(self, strategy):
        """从 strategy 字典提取 parent_strategy_ids（兼容 JSON 字符串 + parent_strategy_id 单值）。
        返回 int 列表。
        """
        import json
        psids_raw = strategy.get("parent_strategy_ids")
        if psids_raw:
            if isinstance(psids_raw, str):
                try:
                    psids = json.loads(psids_raw)
                except (ValueError, TypeError):
                    psids = []
            else:
                psids = list(psids_raw)
            return [int(x) for x in psids if str(x).isdigit()]
        # 兼容旧字段 parent_strategy_id（单值）
        legacy = strategy.get("parent_strategy_id")
        if legacy:
            return [int(legacy)]
        return []

    def _chain_layerN(self, level, layer_hits, payload):
        """通用串联：level=N 的候选 = 父策略的命中合并（v0.8.1 多选父策略链式）。
        v0.8.0：原 _chain_layer2 升级为通用 _chain_layerN（保留 _chain_layer2 作为别名）。

        每个 level=N 策略的 parent_strategy_ids 指定其父策略列表，
        候选 = 这些父策略（level=N-1）所有命中记录的去重合并。
        """
        if level <= 1:
            return layer_hits  # 一层无需串联
        if not layer_hits or self._scan_cancel.is_set():
            return

        # v0.8.1：按 strategy_id 索引命中（一层命中来自多个策略，候选接收多个父策略的并集）
        by_strategy_id = {}
        for h in layer_hits:
            sid = getattr(h, "strategy_id", None)
            if sid is None:
                # 历史数据可能没有 strategy_id 字段，跳过
                continue
            by_strategy_id.setdefault(sid, []).append(h)

        # 找 level=N 的所有启用策略
        strategies = self.db.list_strategies(enabled_only=True, level=level)
        if not strategies:
            return

        # 对每个 level=N 策略：候选 = 其父策略列表对应的命中合并
        for st in strategies:
            if self._scan_cancel.is_set():
                break
            psids = self._parse_parent_strategy_ids(st)
            # 没有父策略：候选 = level=N-1 全量命中（兼容旧用法/全市场直测）
            if not psids:
                cands_pool = layer_hits
            else:
                cands_pool = []
                for pid in psids:
                    cands_pool.extend(by_strategy_id.get(pid, []))
            # 跨父策略去重（同票只算一次）
            uniq = {}
            for h in cands_pool:
                if isinstance(h, ScanResult):
                    uniq.setdefault(h.code, h)
            cands = list(uniq.values())
            if not cands:
                self.status.emit(
                    f"⚙️ level={level} {st['name']}：父策略无命中，跳过")
                continue

            self.status.emit(
                f"⚙️ level={level} {st['name']}：{len(cands)} 只父策略命中 → 精筛…")
            try:
                st_payload = {
                    "strategy": st, "stock_df": None, "candidates": cands,
                    "max_stocks": len(cands), "parallel": 1, "bars_count": 300,
                    "start_date": payload.get("start_date"),
                    "end_date": payload.get("end_date"),
                    "as_of_date": payload.get("end_date"),
                    "quote_filter": None,
                }
                self._run_dna_strategy(st, st_payload,
                                       uuid.uuid4().hex[:12], "batch")
            except Exception as e:  # noqa: BLE001
                log.warning("level=%d 策略「%s」执行失败: %s",
                            level, st.get("name"), e)

    def _chain_layer2(self, hits, payload):
        """v0.8.0：保留为 _chain_layerN(level=2) 的别名（向后兼容）。"""
        self._chain_layerN(2, hits, payload)
        return
        # 保留原代码作为参考（已通过 _run_dna_strategy 串联）
        if not hits or self._scan_cancel.is_set():
            return
        from core.dna_engine import FEATURE_VERSION, TemplateManager
        tm = TemplateManager()
        names = [n for n in tm.templates
                 if tm.metadata.get(n, {}).get("_feature_version") == FEATURE_VERSION]
        if not names:
            self.status.emit("二层精筛跳过：模具库为空或均为旧版特征口径，"
                             "请先在策略管理页制作模具")
            return
        # 跨策略去重（同一票可能命中多个粗筛策略，精筛只算一次）
        uniq = {}
        for h in hits:
            if isinstance(h, ScanResult):
                uniq.setdefault(h.code, h)
        cands = list(uniq.values())
        if not cands:
            return
        self.status.emit(
            f"⚙️ 自动串联二层精筛：{len(cands)} 只粗筛命中 → DNA形态匹配+微观规则…")
        as_of_date = payload.get("end_date")
        feed = CachedFeed(self.feed, self.db)
        try:
            hits2 = layer2_screen(
                feed, cands, names, threshold=75.0, top_n=15,
                window_mode="template_len", length_scan=True, fast=False,
                workers=1, as_of_date=as_of_date,
                progress=self._scan_progress,
                cancelled=self._scan_cancel.is_set)
        except Exception as e:  # noqa: BLE001
            log.warning("自动二层精筛失败: %s", e)
            self.status.emit(f"二层精筛失败：{e}")
            return
        session_id = uuid.uuid4().hex[:12]
        if hits2:
            self.db.add_scan_hits(session_id, None, "自动二层精筛", hits2, layer=2)
        # 历史日期对照不自动进池（信号已错过）
        is_hist = self._is_historical_scan(as_of_date)
        if not is_hist:
            self._dna_add_to_watch(hits2, {"name": "自动二层精筛"})
            self.db.mark_session_in_watchlist(session_id)
        self.scan_done.emit({
            "results": hits2, "new_hits": hits2, "session_id": session_id,
            "strategy_name": "自动二层精筛", "mode": payload.get("mode", "batch"),
            "layer": 2, "historical": is_hist,
        })
        self.status.emit(
            f"二层精筛完成：{len(cands)} 只候选 → {len(hits2)} 只通过"
            + ("（历史日期对照，未自动进池）" if is_hist else ""))

    def _is_historical_scan(self, date_str):
        """判断 signal_date 是否指向历史日期（非最新交易日）。
        历史日期对照扫描的结果不自动进观察池（v0.7.3）。"""
        if not date_str:
            return False
        _dmin, dmax = self.db.get_bars_date_range()
        return bool(dmax) and date_str != dmax

    def _auto_add_to_watch(self, hits, strategy, layer=1, level=None,
                            add_date=None):
        """新命中自动进入观察池（待审核），已在池中（有效状态）则跳过。
        v0.8.0：layer=入池层级（来自 strategy.level）——同票同 add_date
        update_watch_dna 会升级到最高 level（精筛覆盖粗筛）。
        v0.8.1：参数名统一为 level（与 _dna_add_to_watch 一致）；
               保留 layer 兼容旧调用，优先使用 level。
        v0.8.12：add_date 由调用方传入（=用户选的 end_date），保证 watch_pool /
                  TDX 板块名 / 日历归档与用户所选日期对齐。
        """
        if level is not None:
            layer = level  # 兼容层
        if not hits:
            return
        active = {str(w["code"]) for w in self.db.list_watch()
                  if w["status"] in ("待审核", "观察中", "已开仓")}
        layer_note = "粗筛" if layer == 1 else ("精筛" if layer == 2 else f"level{layer}精筛")
        added = 0
        for h in hits:
            if h.code in active:
                continue
            self.db.add_watch(code=h.code, name=h.name, market=h.market,
                              trigger_indicator=strategy.get("name", ""),
                              entry_price=h.price or None,
                              reason=f"策略「{strategy.get('name', '')}」自动命中（{layer_note}层）",
                              status="待审核",
                              add_date=add_date,   # v0.8.12：跟随用户选的日期
                              dna_score=getattr(h, "dna_score", None),
                              dna_template=getattr(h, "dna_template", None),
                              level=layer)
            active.add(h.code)
            added += 1
        if added:
            self.status.emit(f"🆕 新命中 {added} 只已自动加入观察池（待审核，{layer_note}层）")

    # ------------------------------------------------------------ DNA 精筛
    def _run_dna_strategy(self, strategy, p, session_id, mode):
        """DNA 精筛策略执行（v0.8.1：parent_strategy_ids 多选链式）：

        - 候选已传入（来自 _chain_layerN 按父策略 ID 过滤后）→ 直接精筛
        - 有 parent_strategy_ids（多选，v0.8.1）：先跑所有父策略（layer=parent.level 入库+进池），
          再对其命中结果精筛（候选合并去重）
        - 有 legacy parent_strategy_id（单值，向后兼容）：按单父策略处理
        - 无 parent + 无 candidates → 全市场直测（仅 dna_fast / 策略管理页"立即扫描所选"）

        入库 layer = strategy["level"]（默认 2），parent_strategy_ids 写入 scan_history。
        """
        import json as _json
        cfg = {}
        try:
            cfg = _json.loads(strategy.get("formula") or "{}")
        except ValueError:
            cfg = {}
        threshold = float(cfg.get("threshold", 0) or 0)
        window_mode = cfg.get("window_mode", "template_len")
        length_scan = bool(cfg.get("length_scan", True))
        level = int(strategy.get("level", 2) or 2)
        # v0.8.1：候选池在 _chain_layerN 已按父策略过滤好，这里直接用 p["candidates"] 即可。
        # 若 candidates 为空但有 parent_strategy_ids（独立精筛场景），需要现场跑父策略。

        candidates = p.get("candidates")

        # v0.8.1：从 strategy 提取多选父策略 IDs
        psids = self._parse_parent_strategy_ids(strategy)
        legacy_parent_id = cfg.get("parent") or strategy.get("parent_strategy_id")
        if not psids and legacy_parent_id:
            psids = [int(legacy_parent_id)]

        if candidates is None:
            if psids:
                # 独立精筛场景：先跑所有父策略，候选 = 父命中合并去重
                all_parent_hits = []
                for pid in psids:
                    parent = self.db.get_strategy(int(pid))
                    if not parent or (parent.get("kind") != "dna" and not strategy_formula(parent)):
                        log.warning("精筛策略「%s」的父策略 %s 不可用", strategy.get("name"), pid)
                        continue
                    self.status.emit(f"正在执行从属粗筛：{parent.get('name')}")
                    if mode == "batch":
                        results = parallel_scan_formula(
                            p["stock_df"], strategy_formula(parent), self.db,
                            workers=p["parallel"], bars_count=p["bars_count"],
                            strategy_id=parent["id"],
                            progress=self._scan_progress,
                            cancelled=self._scan_cancel.is_set)
                    else:
                        results = scan_formula(
                            self.feed, p["stock_df"], strategy_formula(parent),
                            max_stocks=p["max_stocks"], quote_filter=p["quote_filter"],
                            bars_count=p["bars_count"],
                            strategy_id=parent["id"],
                            progress=self._scan_progress,
                            cancelled=self._scan_cancel.is_set)
                    if results:
                        parent_session = uuid.uuid4().hex[:12]
                        self.db.add_scan_hits(
                            parent_session, parent["id"], parent["name"],
                            results, layer=int(parent.get("level", 1) or 1),
                            parent_strategy_ids=[strategy.get("id")])
                        self._auto_add_to_watch(results, parent,
                                                level=int(parent.get("level", 1) or 1))
                    all_parent_hits.extend(results or [])
                # 去重
                uniq = {}
                for h in all_parent_hits:
                    if isinstance(h, ScanResult):
                        uniq.setdefault(h.code, h)
                candidates = list(uniq.values())
            else:
                # 一级独立精筛：候选 = 传入范围（全市场直测）
                stock_df = p.get("stock_df")
                candidates = [{"code": r.code, "name": r.name, "market": r.market}
                              for r in stock_df.itertuples()] \
                    if stock_df is not None else []

        if not candidates:
            self.scan_done.emit({
                "results": [], "new_hits": [], "session_id": session_id,
                "strategy_name": strategy.get("name"), "mode": mode, "level": level,
                "parent_strategy_ids": psids})
            self.status.emit(
                f"精筛策略「{strategy.get('name')}」：无候选（父策略 0 命中或范围为空）")
            return

        # 全市场直测（dna_fast）与全市场自动扫描自动使用快速模式（单档，不做长度弹性）
        fast = mode == "dna_fast" or (
            mode == "batch" and p.get("stock_df") is not None
            and len(p["stock_df"]) > 500)
        # 历史日期精筛：窗口末端锚定指定日（v0.7.3）
        as_of_date = p.get("as_of_date") or p.get("end_date")
        feed = CachedFeed(self.feed, self.db)
        # v0.7.4：DNA 精筛统一走 layer2_screen（DTW骨架分 + 微观规则综合分）
        hits = layer2_screen(feed, candidates, cfg.get("templates", []),
                             threshold=threshold, top_n=None,
                             window_mode=window_mode,
                             length_scan=length_scan, fast=fast,
                             workers=p.get("parallel", 1),
                             as_of_date=as_of_date,
                             progress=self._scan_progress,
                             cancelled=self._scan_cancel.is_set)
        # v0.8.1：每个 ScanResult 写入 strategy_id（用于跨层级候选按父策略索引）
        strategy_id = int(strategy.get("id") or 0)
        for h in hits:
            h.strategy_id = strategy_id
        if hits:
            self.db.add_scan_hits(session_id, strategy.get("id"),
                                  strategy.get("name"), hits,
                                  layer=level,
                                  parent_strategy_ids=psids)

        prev = {x["code"] for x in self.db.hits_today(session_exclude=session_id)
                if x.get("strategy_id") == strategy.get("id")}
        new_hits = [h for h in hits if h.code not in prev]
        # v0.7.3：历史日期对照精筛不自动进池（信号已错过，进池无意义）
        is_hist = self._is_historical_scan(as_of_date)
        if not is_hist:
            self._dna_add_to_watch(hits, strategy, level=level)
            self.db.mark_session_in_watchlist(session_id)

        self.scan_done.emit({
            "results": hits, "new_hits": new_hits, "session_id": session_id,
            "strategy_name": strategy.get("name"), "mode": mode,
            "level": level, "parent_strategy_ids": psids,
            "historical": is_hist,
        })
        self.status.emit(
            f"精筛策略「{strategy.get('name')}」完成：候选 {len(candidates)} 只，"
            f"DNA通过 {len(hits)} 只" + ("" if not new_hits
                                        else f"，新信号 {len(new_hits)} 只")
            + ("（历史日期对照，未自动进池）" if is_hist else ""))

    def _dna_add_to_watch(self, hits, strategy, level=2):
        """DNA 精筛命中自动进池：已在池则更新 DNA 标注 + 升级 level，否则新增并标注来源。
        v0.7.4：原因中附加综合分与买点定级。
        v0.8.0：level=精筛层级（入参为 strategy.level，默认 2）。
        """
        if not hits:
            return
        active = {str(w["code"]) for w in self.db.list_watch()
                  if w["status"] in ("待审核", "观察中", "已开仓")}
        added = 0
        for h in hits:
            note = self._dna_watch_note(h)
            if h.code in active:
                self.db.update_watch_dna(
                    h.code, h.dna_score, h.dna_template, note=note, level=level)
            else:
                self.db.add_watch(code=h.code, name=h.name, market=h.market,
                                  trigger_indicator=strategy.get("name", ""),
                                  entry_price=h.price or None,
                                  reason=f"策略「{strategy.get('name', '')}」DNA精筛命中"
                                         f"{note}",
                                  status="待审核",
                                  dna_score=h.dna_score,
                                  dna_template=h.dna_template,
                                  level=level)
                added += 1
        if added:
            self.status.emit(f"🆕 DNA精筛新命中 {added} 只已加入观察池（待审核）")

    @staticmethod
    def _dna_watch_note(h):
        """构造观察池备注片段（以 精筛[模具] 开头，配合 update_watch_dna 去重）：
        精筛[模具]骨架分；综合X分｜买点定级｜微观信号"""
        bits = [f"精筛[{h.dna_template}] {h.dna_score}分"]
        if getattr(h, "final_score", None) is not None:
            bits.append(f"综合{round(float(h.final_score), 1)}分")
        if getattr(h, "buy_level", ""):
            bits.append(h.buy_level)
        if getattr(h, "signals", ""):
            bits.append(h.signals)
        return "；".join(bits)

    def _scan_progress(self, i, total, name):
        self.scan_progress.emit(i, total, name)

    # ------------------------------------------------------------ 提醒检查
    def _check_quotes_alerts(self, quotes_df):
        alerts = self.db.list_alerts(enabled_only=True)
        if not alerts:
            return
        events = []
        for row in quotes_df.itertuples():
            events.extend(check_quote_alerts(alerts, row._asdict()))

        for a in alerts:
            if a["alert_type"] in ("macd_cross", "kdj", "rsi", "strategy_signal"):
                code = str(a["code"])
                market = a["market"] if a["market"] is not None else infer_market(code)
                try:
                    bars = self.feed.get_daily_bars(code, market=market, count=120)
                    if bars is not None and len(bars) >= 30:
                        dfi = compute_indicators(bars)
                        events.extend(check_bar_alerts([a], code, dfi))
                except Exception as e:  # noqa: BLE001
                    log.debug("提醒检查 %s 失败: %s", code, e)

        if events:
            seen = set()
            for e in events:
                if e["alert_id"] in seen:
                    continue
                seen.add(e["alert_id"])
                self.db.mark_alert_triggered(e["alert_id"], e["message"])
            self.alerts_fired.emit(events)
            self._push_events(events)

    def _push_events(self, events):
        """提醒触发时按配置推送微信/Bark。"""
        try:
            channel = self.db.get_setting("push_channel", "none")
            key = self.db.get_setting("push_key", "")
            if channel and channel != "none" and key:
                for e in events[:3]:
                    ok, msg = send_push(channel, key, "交易提醒", e["message"])
                    if not ok:
                        log.warning("推送失败: %s", msg)
        except Exception as e:  # noqa: BLE001
            log.warning("推送处理异常: %s", e)
