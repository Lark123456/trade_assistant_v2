# -*- coding: utf-8 -*-
"""pytdx 行情数据层。

- 服务器池自动切换（连接失败/取数失败时换下一台）
- 线程安全（内部加锁，pytdx 连接不允许多线程并发）
- 提供：实时行情、日K线、指数行情、全市场 A 股列表
"""
import datetime
import logging
import threading

import pandas as pd
from pytdx.hq import TdxHq_API

from config import (CONNECT_TIMEOUT, INDEX_CODES, MAX_BARS_PER_CALL,
                    QUOTE_BATCH, STOCK_PREFIXES, TDX_SERVERS)

log = logging.getLogger("data_feed")


def is_trading_time(now=None):
    """判断当前是否处于 A 股交易时段（含集合竞价与收盘缓冲）。"""
    now = now or datetime.datetime.now()
    if now.weekday() >= 5:  # 周末
        return False
    hm = now.hour * 100 + now.minute
    return any(s <= hm <= e for s, e in
               [((9 * 100 + 15), (11 * 100 + 35)), ((12 * 100 + 55), (15 * 100 + 10))])


def trading_minutes_elapsed(now=None):
    """当日已开盘累计分钟数（9:15 起算，收盘封顶 240 分钟），非交易日/开盘前返回 0。"""
    now = now or datetime.datetime.now()
    if now.weekday() >= 5:
        return 0
    start = datetime.datetime(now.year, now.month, now.day, 9, 15)
    end = datetime.datetime(now.year, now.month, now.day, 15, 0)
    if now < start:
        return 0
    if now > end:
        return 240
    lunch_start = datetime.datetime(now.year, now.month, now.day, 11, 30)
    lunch_end = datetime.datetime(now.year, now.month, now.day, 13, 0)
    minutes = (now - start).total_seconds() / 60
    if now > lunch_start:
        overlap = min((now - lunch_start).total_seconds() / 60,
                      (lunch_end - lunch_start).total_seconds() / 60)
        minutes -= overlap
    return max(0.0, min(minutes, 240.0))


def infer_market(code):
    """根据股票代码推断市场：6/9 开头 -> 上海(1)，其余 -> 深圳(0)。"""
    return 1 if str(code).startswith(("6", "9")) else 0


def price_limit_pct(code):
    """按板块返回涨跌停幅度：主板 10% / 创业板(30)科创板(68) 20% / 北交所(4/8/92) 30%。

    用于复权自检容差：除权日市场高开/低开可能达到涨跌停价（一字板），
    该幅度内属正常市场行为，不作为复权计算异常告警。
    """
    code = str(code).zfill(6)
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("4", "8", "92")):
        return 0.30
    return 0.10


class DataFeed:
    """pytdx 行情客户端封装。"""

    def __init__(self, servers=None, timeout=CONNECT_TIMEOUT, db=None):
        self.servers = list(servers or TDX_SERVERS)
        self.timeout = timeout
        self.db = db                    # 可选：除权事件缓存（Database 实例）
        self._api = TdxHq_API()
        self._lock = threading.RLock()
        self._server_index = -1
        self._connected_ip = None
        self._name_map = {}          # (market, code) -> 股票名称
        self._stock_list_cache = None  # DataFrame: code,name,market
        self.last_adjust = None      # qfq / none（最近一次日K的复权状态）

    # ------------------------------------------------------------------ 连接
    def _connect_next(self):
        """尝试连接服务器池中的下一台，成功返回 True。"""
        n = len(self.servers)
        for _ in range(n):
            self._server_index = (self._server_index + 1) % n
            ip, port = self.servers[self._server_index]
            try:
                self._api.disconnect()
            except Exception:
                pass
            try:
                if self._api.connect(ip, port, time_out=self.timeout):
                    self._connected_ip = ip
                    log.info("已连接行情服务器 %s:%s", ip, port)
                    return True
            except Exception as e:  # noqa: BLE001
                log.warning("连接 %s:%s 失败: %s", ip, port, e)
        self._connected_ip = None
        return False

    def _call(self, fn, *args, **kwargs):
        """在锁内调用 pytdx 方法，失败自动切换服务器重试一次。"""
        with self._lock:
            for attempt in range(2):
                if self._connected_ip is None and not self._connect_next():
                    raise ConnectionError("所有行情服务器均不可用")
                try:
                    # 关键：给 socket 设置接收超时。pytdx 的 connect() 只有连接超时，
                    # 请求发出后的 recv 无超时——若服务器对某些股票不响应（如部分
                    # 服务器对科创板历史请求无应答），会永久阻塞线程。设超时后由
                    # except 捕获 socket.timeout 并切换服务器重试。
                    self._set_receive_timeout()
                    return fn(*args, **kwargs)
                except Exception as e:  # noqa: BLE001
                    log.warning("请求失败(尝试%d): %s，切换服务器", attempt + 1, e)
                    self._connected_ip = None
            raise ConnectionError("行情服务器请求失败")

    def _set_receive_timeout(self, seconds=10):
        """给 pytdx 底层 socket 设置接收超时（秒）。

        服务器不响应时 recv 抛 socket.timeout，由 _call 捕获后切换服务器，
        避免单个请求永久挂起导致后台线程卡死、UI 表现为"抓取无反应/超时"。
        """
        try:
            client = getattr(self._api, "client", None)
            if client is not None:
                client.settimeout(seconds)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 实时行情
    def get_quotes(self, codes, with_name=True):
        """获取实时行情。

        codes: [(market, code), ...] 或 [code, ...]（自动推断市场）
        返回 DataFrame: code,name,price,last_close,open,high,low,vol,amount,pct,time
        """
        pairs = []
        for c in codes:
            if isinstance(c, (tuple, list)) and len(c) == 2:
                pairs.append((int(c[0]), str(c[1]).zfill(6)))
            else:
                pairs.append((infer_market(c), str(c).zfill(6)))

        rows = []
        for i in range(0, len(pairs), QUOTE_BATCH):
            batch = pairs[i:i + QUOTE_BATCH]
            raw = self._call(self._api.get_security_quotes, batch) or []
            for q in raw:
                try:
                    last_close = float(q.get("last_close") or 0)
                    price = float(q.get("price") or 0)
                    pct = (price - last_close) / last_close * 100 if last_close else 0.0
                except (TypeError, ValueError):
                    pct = 0.0
                code = str(q.get("code", "")).zfill(6)
                market = int(q.get("market", infer_market(code)))
                name = self._get_name(market, code) if with_name else q.get("name", "")
                rows.append({
                    "market": market,
                    "code": code,
                    "name": name,
                    "price": float(q.get("price") or 0),
                    "last_close": last_close,
                    "open": float(q.get("open") or 0),
                    "high": float(q.get("high") or 0),
                    "low": float(q.get("low") or 0),
                    "vol": float(q.get("vol") or 0),        # 手
                    "amount": float(q.get("amount") or 0),  # 元
                    "pct": round(pct, 2),
                    "zs": float(q.get("reversed_bytes9") or 0),  # 涨速(通达信原生)
                    "time": str(q.get("servertime", "")),
                })
        return pd.DataFrame(rows)

    def get_index_quotes(self):
        """指数行情（上证指数/深证成指/创业板指）。"""
        return self.get_quotes([(m, c) for m, c, _ in INDEX_CODES], with_name=False)

    # ------------------------------------------------------------ K 线数据
    def get_daily_bars(self, code, market=None, count=800, adjust="qfq"):
        """获取日K线，返回 DataFrame(date,open,high,low,close,vol,amount)。

        adjust: "qfq" 前复权（默认，v0.7.0 全量切换）/ "none" 原始不复权。
        前复权由除权除息事件自算（core.adjust），事件优先走本地缓存；
        服务器不支持时降级为不复权并置 last_adjust="none"。
        pytdx 单次最多 800 根且为倒序（最新在前），需要时翻页拼接。
        """
        market = int(market if market is not None else infer_market(code))
        code = str(code).zfill(6)
        bars = []
        start = 0
        while start < count:
            batch = self._call(self._api.get_security_bars, 4, market, code,
                               start, min(MAX_BARS_PER_CALL, count - start))
            if not batch:
                break
            bars.extend(batch)
            if len(batch) < MAX_BARS_PER_CALL:
                break
            start += len(batch)
        bars = bars[:count]

        rows = []
        for b in bars:
            dt = f"{b['year']:04d}-{b['month']:02d}-{b['day']:02d}"
            rows.append({
                "date": dt,
                "open": float(b.get("open", 0)),
                "high": float(b.get("high", 0)),
                "low": float(b.get("low", 0)),
                "close": float(b.get("close", 0)),
                "vol": float(b.get("vol", 0)),
                "amount": float(b.get("amount", 0)),
            })
        df = pd.DataFrame(rows)
        if not df.empty:
            # pytdx 不同服务器/版本返回顺序可能为最新在前或最旧在前，
            # 统一按日期升序排列，确保调用方拿到时间正序数据。
            df = df.sort_values("date").reset_index(drop=True)
        if not df.empty and adjust == "qfq":
            events = self._get_qfq_events(code, market)
            if events is None:
                self.last_adjust = "none"   # 事件拉取失败：降级不复权
                log.warning("%s 除权事件不可用，本次返回不复权数据", code)
            else:
                from core.adjust import compute_qfq_prices
                df, issues = compute_qfq_prices(df, events,
                                                limit_pct=price_limit_pct(code))
                self.last_adjust = "qfq"
                if issues:
                    log.warning("%s 复权告警: %s", code, "; ".join(issues))
        else:
            self.last_adjust = "none"
        return df

    def _get_qfq_events(self, code, market):
        """取除权除息事件：优先本地缓存，无则拉服务器并存库。失败返回 None。"""
        if self.db is not None:
            try:
                cached = self.db.get_xdxr_events(code)
                if cached:
                    return cached
            except Exception as e:  # noqa: BLE001
                log.debug("读除权事件缓存失败: %s", e)
        events = self.get_xdxr_events(market, code)
        if events is None:
            return None
        if self.db is not None and events:
            try:
                self.db.save_xdxr_events(code, market, events)
            except Exception as e:  # noqa: BLE001
                log.debug("缓存除权事件失败: %s", e)
        return events

    def get_xdxr_events(self, market, code):
        """从行情服务器拉取除权除息事件。失败返回 None（降级不复权）。"""
        code = str(code).zfill(6)
        try:
            raw = self._call(self._api.get_xdxr_info, int(market), code) or []
        except Exception as e:  # noqa: BLE001
            log.warning("%s 除权除息事件拉取失败: %s", code, e)
            return None
        events = []
        for r in raw:
            events.append({
                "date": f"{r['year']:04d}-{r['month']:02d}-{r['day']:02d}",
                "category": r.get("category", 0),
                "fenhong": r.get("fenhong") or 0,
                "peigujia": r.get("peigujia") or 0,
                "songzhuangu": r.get("songzhuangu") or 0,
                "peigu": r.get("peigu") or 0,
            })
        return events

    def get_kline_bars(self, code, market=None, category=0, count=800):
        """获取分钟K线（v0.7.3 看K线图用）。

        category: 0=5分钟 1=15分钟 2=30分钟 3=60分钟 7=1分钟
        返回 DataFrame(datetime, open, high, low, close, vol, amount)，时间正序。
        分钟线为不复权原始数据（前复权仅对日K有效，UI 会标注）。
        """
        market = int(market if market is not None else infer_market(code))
        code = str(code).zfill(6)
        bars = []
        start = 0
        while start < count:
            batch = self._call(self._api.get_security_bars, category, market, code,
                               start, min(MAX_BARS_PER_CALL, count - start))
            if not batch:
                break
            bars.extend(batch)
            if len(batch) < MAX_BARS_PER_CALL:
                break
            start += len(batch)
        bars = bars[:count]
        rows = []
        for b in bars:
            rows.append({
                "datetime": (f"{b['year']:04d}-{b['month']:02d}-{b['day']:02d} "
                             f"{b.get('hour', 0):02d}:{b.get('minute', 0):02d}"),
                "open": float(b.get("open", 0)), "high": float(b.get("high", 0)),
                "low": float(b.get("low", 0)), "close": float(b.get("close", 0)),
                "vol": float(b.get("vol", 0)), "amount": float(b.get("amount", 0)),
            })
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values("datetime").reset_index(drop=True)
        return df

    def get_index_daily_bars(self, market, code, count=800):
        bars = []
        start = 0
        while start < count:
            batch = self._call(self._api.get_index_bars, 4, market, code,
                               start, min(MAX_BARS_PER_CALL, count - start))
            if not batch:
                break
            bars.extend(batch)
            if len(batch) < MAX_BARS_PER_CALL:
                break
            start += len(batch)
        bars = bars[:count]
        rows = [{
            "date": f"{b['year']:04d}-{b['month']:02d}-{b['day']:02d}",
            "open": float(b.get("open", 0)), "high": float(b.get("high", 0)),
            "low": float(b.get("low", 0)), "close": float(b.get("close", 0)),
            "vol": float(b.get("vol", 0)), "amount": float(b.get("amount", 0)),
        } for b in bars]
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values("date").reset_index(drop=True)
        return df

    # ------------------------------------------------------------ 股票列表
    def _get_name(self, market, code):
        key = (market, str(code).zfill(6))
        return self._name_map.get(key, "")

    def get_stock_list(self, refresh=False):
        """全市场 A 股列表 DataFrame(code,name,market)，带缓存。

        优先使用 akshare（一次请求获取全市场 5500+ 只，代码+名称）；
        失败时退回 pytdx 分页（多数公开服务器仅返回深市列表，作为兜底）。
        """
        if self._stock_list_cache is not None and not refresh:
            return self._stock_list_cache

        df = None
        try:
            import akshare as ak
            import time as _time
            raw = None
            for attempt in range(3):  # akshare 偶发 SSL 抖动，重试
                try:
                    raw = ak.stock_info_a_code_name()
                    break
                except Exception as e:  # noqa: BLE001
                    log.warning("akshare 获取股票列表第 %d 次失败: %s", attempt + 1, e)
                    _time.sleep(1.5)
            if raw is not None:
                df = pd.DataFrame(raw)
                df["code"] = df["code"].astype(str).str.zfill(6)
                df["market"] = df["code"].apply(infer_market)
                df["name"] = df["name"].astype(str).str.replace(" ", "")
        except Exception as e:  # noqa: BLE001
            log.warning("akshare 获取股票列表失败，退回 pytdx: %s", e)

        if df is None or df.empty:
            df = self._stock_list_from_pytdx()

        df = df.drop_duplicates(subset=["code"]).reset_index(drop=True)
        self._stock_list_cache = df
        for _, r in df.iterrows():
            self._name_map[(int(r["market"]), str(r["code"]))] = r["name"]
        log.info("全市场 A 股列表加载完成，共 %d 只", len(df))
        return df

    def _stock_list_from_pytdx(self):
        """pytdx 分页拉取（兜底；多数服务器仅能取到深市列表）。"""
        rows = []
        for market in (0, 1):
            count = self._call(self._api.get_security_count, market) or 0
            start = 0
            while start < count:
                page = self._call(self._api.get_security_list, market, start)
                if not page:
                    break
                for item in page:
                    code = str(item.get("code", "")).zfill(6)
                    if code.startswith(STOCK_PREFIXES[market]):
                        rows.append({
                            "market": market,
                            "code": code,
                            "name": item.get("name", ""),
                        })
                if len(page) < 1000:
                    break
                start += len(page)
        return pd.DataFrame(rows)

    # ------------------------------------------------------------ 状态
    @property
    def connected_ip(self):
        return self._connected_ip

    def disconnect(self):
        with self._lock:
            try:
                self._api.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._connected_ip = None
