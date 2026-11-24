# -*- coding: utf-8 -*-
"""SQLite 数据持久化：观察池 / 交易日志 / 因子快照 / 提醒 / 行情缓存。"""
import json
import logging
import sqlite3
import threading
from contextlib import contextmanager

import strategy_archive

log = logging.getLogger("database")

SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    name TEXT,
    market INTEGER,
    add_date TEXT,
    trigger_indicator TEXT,
    indicator_params TEXT,        -- JSON
    entry_price REAL,
    reason TEXT,
    status TEXT DEFAULT '观察中',  -- 观察中/已开仓/已清仓
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS tdx_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    name TEXT,
    market INTEGER,
    add_date TEXT,                -- 所属日历日期（行情日历二级观察池）
    block_name TEXT,              -- 来源通达信板块
    reason TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS trade_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id INTEGER,
    code TEXT,
    name TEXT,
    trade_type TEXT,              -- 开仓/止盈/止损/减仓/加仓
    trigger_condition TEXT,
    trade_price REAL,
    trade_volume INTEGER,
    trade_time TEXT,
    snapshot_id INTEGER,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS factor_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER,
    stock_code TEXT,
    market INTEGER,
    snapshot_time TEXT,
    indicators TEXT,              -- JSON: {"MACD_DIF":..,"KDJ_K":..}
    market_index REAL,
    volume REAL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    name TEXT,
    market INTEGER,
    alert_type TEXT,              -- price/percent/volume/macd_cross/kdj/rs
    operator TEXT,                -- >= / <= / > / < / cross_up / cross_down
    threshold REAL,
    params TEXT,                  -- JSON
    enabled INTEGER DEFAULT 1,
    last_triggered_at TEXT,
    triggered_count INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS alert_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER,
    code TEXT,
    name TEXT,
    message TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS market_snapshot (
    code TEXT PRIMARY KEY,
    name TEXT,
    market INTEGER,
    price REAL, last_close REAL, open REAL, high REAL, low REAL,
    vol REAL, amount REAL, pct REAL,
    update_time TEXT
);
CREATE TABLE IF NOT EXISTS stock_list (
    code TEXT PRIMARY KEY,
    name TEXT,
    market INTEGER
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT,                        -- 策略编号 S0001（v0.6.5 起，稳定不变，改名不影响）
    name TEXT NOT NULL,
    kind TEXT DEFAULT 'formula',      -- formula / indicator(指标库存档)；visual 已于 v0.6.3 移除并迁移
    formula TEXT,                     -- 通达信公式
    conditions TEXT,                  -- 历史遗留列（visual 策略迁移后为空，不再使用）
    description TEXT,                 -- 中文逻辑说明书（保险机制）
    enabled INTEGER DEFAULT 1,
    scan_mode TEXT DEFAULT 'batch',   -- intraday 盘中重点 / batch 盘前盘后全市场 / manual 手动
    created_at TEXT,
    updated_at TEXT,
    tdx_alias TEXT                    -- v0.8.15: TDX cfg name 字段别名(≤8字节 GBK),
                                       -- 用于通达信板块管理的"目录名/分组名"; 不填 fallback 到 name 截断
);
-- v0.8.15: 已有 db 升级: ALTER TABLE 加 tdx_alias 列(若不存在)
-- SQLite 不支持 IF NOT EXISTS for ADD COLUMN, 用 try/except 包裹
CREATE TABLE IF NOT EXISTS strategy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER,
    code TEXT,                        -- 策略编号（归集依据）
    version INTEGER,                  -- 版本号 v1、v2...
    name TEXT,
    kind TEXT,
    formula TEXT,
    description TEXT,
    scan_mode TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS scan_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    strategy_id INTEGER,
    strategy_name TEXT,
    scan_time TEXT,
    code TEXT,
    name TEXT,
    market INTEGER,
    price REAL,
    pct REAL,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS daily_bars_cache (
    code TEXT,
    market INTEGER,
    date TEXT,
    open REAL, high REAL, low REAL, close REAL,
    vol REAL, amount REAL,
    PRIMARY KEY (code, date)
);
CREATE TABLE IF NOT EXISTS vol5_cache (
    code TEXT PRIMARY KEY,
    vol5 REAL,
    date TEXT
);
CREATE TABLE IF NOT EXISTS xdxr_events (
    code TEXT,
    market INTEGER,
    date TEXT,
    category INTEGER,             -- 1=除权除息（价格相关），5=股本变化，9=转配股上市
    fenhong REAL,                 -- 每股现金红利（元）
    peigujia REAL,                -- 配股价（元）
    songzhuangu REAL,             -- 每股送转股数
    peigu REAL,                   -- 每股配股数
    updated_at TEXT,
    PRIMARY KEY (code, date, category)
);
"""


class Database:
    """线程安全的 SQLite 封装。"""

    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        # WAL 模式 + busy_timeout：改善多线程扫描时的读写并发（v0.7.1 性能优化）
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        except Exception as e:  # noqa: BLE001
            log.debug("PRAGMA 设置失败: %s", e)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()
        log.info("数据库初始化完成: %s", self.path)

    def _migrate(self):
        """旧库增量升级：给 strategies 表补 description 列等。"""
        import datetime
        now = datetime.datetime.now().isoformat(timespec="seconds")
        cur = self._conn.cursor()
        cols = {r[1] for r in cur.execute("PRAGMA table_info(strategies)").fetchall()}
        # v0.8.15：TDX cfg name 别名(≤8字节 GBK, 用户可手动填)
        if "tdx_alias" not in cols:
            cur.execute("ALTER TABLE strategies ADD COLUMN tdx_alias TEXT")
            log.info("数据库升级：strategies 表新增 tdx_alias 列")
        if "description" not in cols:
            cur.execute("ALTER TABLE strategies ADD COLUMN description TEXT")
            log.info("数据库升级：strategies 表新增 description 列")
        # v0.6.3：可视化条件编辑器已移除，存量 visual 策略迁移为 formula
        # （formula 列保存的即条件树导出的等价公式）
        cur.execute("UPDATE strategies SET kind='formula', conditions=NULL "
                    "WHERE kind='visual'")
        if cur.rowcount:
            log.info("数据库升级：%d 条 visual 策略迁移为 formula", cur.rowcount)
        # v0.6.5：策略编号（S0001...）——已有策略按 id 顺序补号，删除不回收
        if "code" not in cols:
            cur.execute("ALTER TABLE strategies ADD COLUMN code TEXT")
            log.info("数据库升级：strategies 表新增 code 列")
        rows = cur.execute("SELECT id FROM strategies WHERE code IS NULL OR code='' "
                           "ORDER BY id").fetchall()
        if rows:
            nums = [int(r["code"][1:]) for r in
                    cur.execute("SELECT code FROM strategies WHERE code LIKE 'S%'").fetchall()
                    if r["code"] and r["code"][1:].isdigit()]
            nxt = (max(nums) + 1) if nums else 1
            for r in rows:
                cur.execute("UPDATE strategies SET code=? WHERE id=?",
                            (f"S{nxt:04d}", r["id"]))
                nxt += 1
            log.info("数据库升级：为 %d 条策略生成编号", len(rows))
        # v0.6.5：为无版本记录的策略初始化 v1（迭代版本归集起点），并写归档文件
        for r in cur.execute("SELECT * FROM strategies").fetchall():
            cnt = cur.execute("SELECT COUNT(*) AS c FROM strategy_versions "
                              "WHERE strategy_id=?", (r["id"],)).fetchone()["c"]
            if cnt == 0:
                cur.execute(
                    "INSERT INTO strategy_versions(strategy_id,code,version,name,kind,"
                    "formula,description,scan_mode,created_at) VALUES(?,?,1,?,?,?,?,?,?)",
                    (r["id"], r["code"], r["name"], r["kind"], r["formula"] or "",
                     r["description"] or "", r["scan_mode"], r["created_at"] or now))
                strategy_archive.archive_strategy_version(
                    {"code": r["code"], "name": r["name"], "kind": r["kind"],
                     "formula": r["formula"] or "", "description": r["description"] or "",
                     "scan_mode": r["scan_mode"]}, 1)

        # v0.7.0：DNA 精筛 —— 分层/分数列 + 策略从属关系 + 观察池 DNA 标注
        hcols = {r[1] for r in cur.execute("PRAGMA table_info(scan_history)").fetchall()}
        for col, ddl in (("layer", "ALTER TABLE scan_history ADD COLUMN layer INTEGER DEFAULT 1"),
                         ("dna_score", "ALTER TABLE scan_history ADD COLUMN dna_score REAL"),
                         ("dna_template", "ALTER TABLE scan_history ADD COLUMN dna_template TEXT"),
                         ("dna_detail", "ALTER TABLE scan_history ADD COLUMN dna_detail TEXT"),
                         ("signal_date", "ALTER TABLE scan_history ADD COLUMN signal_date TEXT")):
            if col not in hcols:
                cur.execute(ddl)
                log.info("数据库升级：scan_history 表新增 %s 列", col)
        # v0.7.4：双层筛选贯通 —— 二层综合分/微观字段 + 观察池标记 + 交易日期
        #   dtw_score=骨架相似度分  final_score=综合分(骨架+微观修正)
        #   matched_template=最佳匹配模具  buy_level=买点定级  signals=微观信号
        #   in_watchlist=是否已入观察池  trade_date=信号交易日(YYYY-MM-DD)
        hcols = {r[1] for r in cur.execute("PRAGMA table_info(scan_history)").fetchall()}
        for col, ddl in (("dtw_score", "ALTER TABLE scan_history ADD COLUMN dtw_score REAL"),
                         ("final_score", "ALTER TABLE scan_history ADD COLUMN final_score REAL"),
                         ("matched_template", "ALTER TABLE scan_history ADD COLUMN matched_template TEXT"),
                         ("buy_level", "ALTER TABLE scan_history ADD COLUMN buy_level TEXT"),
                         ("signals", "ALTER TABLE scan_history ADD COLUMN signals TEXT"),
                         ("in_watchlist", "ALTER TABLE scan_history ADD COLUMN in_watchlist INTEGER DEFAULT 0"),
                         ("trade_date", "ALTER TABLE scan_history ADD COLUMN trade_date TEXT")):
            if col not in hcols:
                cur.execute(ddl)
                log.info("数据库升级：scan_history 表新增 %s 列", col)
        scols = {r[1] for r in cur.execute("PRAGMA table_info(strategies)").fetchall()}
        if "parent_strategy_id" not in scols:
            cur.execute("ALTER TABLE strategies ADD COLUMN parent_strategy_id INTEGER")
            log.info("数据库升级：strategies 表新增 parent_strategy_id 列（DNA精筛从属关系）")
        # v0.8.0：业务层级与选股方向重构（与 kind 技术类型解耦）
        #   level=1=方向层（一层粗筛）/2=精细层（二层精筛）/3+=预留
        #   direction=选股方向（v0.8.0 简化为：level=1 时 direction=分类标签；
        #                          level≥2 时通过 parent_strategy_ids 链式关联，
        #                          不再使用 direction 字段——v0.8.1 起彻底废弃）
        if "level" not in scols:
            cur.execute("ALTER TABLE strategies ADD COLUMN level INTEGER NOT NULL DEFAULT 1")
            log.info("数据库升级：strategies 表新增 level 列（业务层级）")
        if "direction" not in scols:
            cur.execute("ALTER TABLE strategies ADD COLUMN direction TEXT NOT NULL DEFAULT ''")
            log.info("数据库升级：strategies 表新增 direction 列（已废弃，仅兼容旧数据）")
        # v0.8.1 重构：direction 字段废弃，引入 tags（分类标签）+ parent_strategy_ids（多选父策略）
        if "tags" not in scols:
            cur.execute("ALTER TABLE strategies ADD COLUMN tags TEXT NOT NULL DEFAULT ''")
            log.info("数据库升级：strategies 表新增 tags 列（level=1 的分类标签，自由文本）")
        if "parent_strategy_ids" not in scols:
            cur.execute("ALTER TABLE strategies ADD COLUMN parent_strategy_ids TEXT NOT NULL DEFAULT '[]'")
            log.info("数据库升级：strategies 表新增 parent_strategy_ids 列（level≥2 多选父策略 JSON）")
        # 旧 direction 数据迁移到 tags（direction='B1' → tags='B1'）
        cur.execute("UPDATE strategies SET tags=direction WHERE (tags='' OR tags IS NULL) AND direction<>''")
        n_tags = cur.rowcount
        if n_tags:
            log.info("数据库升级：%d 条策略 direction 迁移到 tags", n_tags)
        # 旧 kind=dna 策略默认 level=2（之前都是二层精筛）
        n_dna = cur.execute(
            "UPDATE strategies SET level=2 WHERE kind='dna' AND level=1").rowcount
        if n_dna:
            log.info("数据库升级：%d 条 kind=dna 策略默认 level=2", n_dna)
        # 旧 parent_strategy_id（数字单选）→ parent_strategy_ids（JSON 数组）
        # 兼容：扫描时 parent_strategy_ids 包含原 parent_strategy_id 即可
        cur.execute(
            "UPDATE strategies SET parent_strategy_ids='[' || parent_strategy_id || ']' "
            "WHERE parent_strategy_id IS NOT NULL AND parent_strategy_ids='[]'")
        n_legacy = cur.rowcount
        if n_legacy:
            log.info("数据库升级：%d 条策略 parent_strategy_id 迁移到 parent_strategy_ids", n_legacy)
        # v0.8.1 重构：保留 scan_history.direction 列（已废弃，不再使用）
        hcols = {r[1] for r in cur.execute("PRAGMA table_info(scan_history)").fetchall()}
        if "direction" not in hcols:
            cur.execute("ALTER TABLE scan_history ADD COLUMN direction TEXT NOT NULL DEFAULT ''")
            log.info("数据库升级：scan_history 表新增 direction 列")
        # v0.8.1：scan_history 表新增 parent_strategy_ids 列（候选来源追溯，仅观察用）
        if "parent_strategy_ids" not in hcols:
            cur.execute("ALTER TABLE scan_history ADD COLUMN parent_strategy_ids TEXT NOT NULL DEFAULT '[]'")
            log.info("数据库升级：scan_history 表新增 parent_strategy_ids 列")
        # v0.8.0：watch_pool 表新增 level 列（入池时记录生效层级）
        wcols = {r[1] for r in cur.execute("PRAGMA table_info(watch_pool)").fetchall()}
        for col, ddl in (("dna_score", "ALTER TABLE watch_pool ADD COLUMN dna_score REAL"),
                         ("dna_template", "ALTER TABLE watch_pool ADD COLUMN dna_template TEXT")):
            if col not in wcols:
                cur.execute(ddl)
                log.info("数据库升级：watch_pool 表新增 %s 列", col)
        if "level" not in wcols:
            cur.execute("ALTER TABLE watch_pool ADD COLUMN level INTEGER NOT NULL DEFAULT 1")
            log.info("数据库升级：watch_pool 表新增 level 列（生效层级）")
        # v0.9.0：策略回测统计列（最新一次回测的胜率/盈亏比/前瞻收益，用于策略管理列表标签展示）
        scols2 = {r[1] for r in cur.execute("PRAGMA table_info(strategies)").fetchall()}
        for col, ddl in (
                ("backtest_win_rate", "ALTER TABLE strategies ADD COLUMN backtest_win_rate REAL"),
                ("backtest_profit_loss_ratio", "ALTER TABLE strategies ADD COLUMN backtest_profit_loss_ratio REAL"),
                ("backtest_total_signals", "ALTER TABLE strategies ADD COLUMN backtest_total_signals INTEGER"),
                ("backtest_avg_ret", "ALTER TABLE strategies ADD COLUMN backtest_avg_ret REAL"),
                ("backtest_avg_holding_days", "ALTER TABLE strategies ADD COLUMN backtest_avg_holding_days REAL"),
                ("backtest_forward_returns", "ALTER TABLE strategies ADD COLUMN backtest_forward_returns TEXT"),
                ("backtest_config", "ALTER TABLE strategies ADD COLUMN backtest_config TEXT"),
                ("backtest_updated_at", "ALTER TABLE strategies ADD COLUMN backtest_updated_at TEXT"),
        ):
            if col not in scols2:
                cur.execute(ddl)
                log.info("数据库升级：strategies 表新增 %s 列（v0.9.0 策略回测统计）", col)

        # v0.7.0：前复权全量切换 —— 首次切换时清空旧日K缓存（旧缓存为不复权口径）
        row = cur.execute("SELECT value FROM settings WHERE key='bars_adjust'").fetchone()
        if not row or row["value"] != "qfq":
            cur.execute("DELETE FROM daily_bars_cache")
            cur.execute("INSERT INTO settings(key,value) VALUES('bars_adjust','qfq') "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            log.info("数据库升级 v0.7.0：日K缓存已清空，切换到前复权口径（bars_adjust=qfq）")

        # v0.10.0：按「信号日」查询的表达式索引。
        # list_scan_history(day=...)/list_scan_dates 都用 COALESCE(trade_date,
        # date(scan_time)) 过滤，没有索引时 8 万行要全表扫 + 排序（实测 7 秒，
        # 选股页每次刷新和日报都受影响）。
        cur.execute("CREATE INDEX IF NOT EXISTS idx_scan_history_day "
                    "ON scan_history(COALESCE(trade_date, date(scan_time)))")

    @contextmanager
    def _cursor(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ------------------------------------------------------------------ 通用
    def get_setting(self, key, default=None):
        with self._cursor() as cur:
            row = cur.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key, value):
        with self._cursor() as cur:
            cur.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)))

    # ---------------------------------------------------------------- 观察池
    def add_watch(self, code, name, market, trigger_indicator="", indicator_params=None,
                  entry_price=None, reason="", status="观察中", add_date=None,
                  dna_score=None, dna_template=None, level=1):
        """加入观察池。v0.8.0：level=入池层级（1=粗筛/2=精筛）——同票同 add_date
        保留最高 level 记录（精筛>粗筛）。
        """
        import datetime
        add_date = add_date or datetime.date.today().isoformat()
        now = datetime.datetime.now().isoformat(timespec="seconds")
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO watch_pool(code,name,market,add_date,trigger_indicator,"
                "indicator_params,entry_price,reason,status,created_at,"
                "dna_score,dna_template,level) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (code, name, market, add_date, trigger_indicator,
                 json.dumps(indicator_params or {}, ensure_ascii=False), entry_price,
                 reason, status, now, dna_score, dna_template, int(level)))
            return cur.lastrowid

    def update_watch(self, watch_id, **fields):
        allowed = {"trigger_indicator", "indicator_params", "entry_price", "reason", "status",
                   "dna_score", "dna_template", "level"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append(f"{k}=?")
            vals.append(json.dumps(v, ensure_ascii=False) if k == "indicator_params" and v is not None else v)
        if not sets:
            return
        vals.append(watch_id)
        with self._cursor() as cur:
            cur.execute(f"UPDATE watch_pool SET {', '.join(sets)} WHERE id=?", vals)

    def list_watch(self, status=None):
        sql = "SELECT * FROM watch_pool"
        args = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY id DESC"
        with self._cursor() as cur:
            rows = cur.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def get_watch(self, watch_id):
        with self._cursor() as cur:
            row = cur.execute("SELECT * FROM watch_pool WHERE id=?", (watch_id,)).fetchone()
        return dict(row) if row else None

    def delete_watch(self, watch_id):
        with self._cursor() as cur:
            cur.execute("DELETE FROM watch_pool WHERE id=?", (watch_id,))
            cur.execute("DELETE FROM trade_log WHERE watch_id=?", (watch_id,))

    # -------------------------------------------- 通达信日历导入（v0.8.5）
    # 与一级观察池 watch_pool 分离：从「行情日历」发起的通达信导入进入本表，
    # 按 add_date 归属到某日的二级「观察池」子页；一级「观察池」大模块不受影响。
    def add_tdx_import(self, code, name="", market=None, add_date=None,
                       block_name="", reason=""):
        """通达信导入 → 行情日历某日期的二级观察池（独立于 watch_pool）。"""
        import datetime
        add_date = add_date or datetime.date.today().isoformat()
        now = datetime.datetime.now().isoformat(timespec="seconds")
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO tdx_imports(code,name,market,add_date,block_name,reason,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (code, name, market, add_date, block_name, reason, now))
            return cur.lastrowid

    def list_tdx_imports(self, add_date=None):
        """列出通达信日历导入；add_date 给定则只取该日期的。"""
        sql = "SELECT * FROM tdx_imports"
        args = []
        if add_date:
            sql += " WHERE add_date=?"
            args.append(add_date)
        sql += " ORDER BY id DESC"
        with self._cursor() as cur:
            rows = cur.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def delete_tdx_import(self, code, add_date=None):
        """删除通达信日历导入（可只删某日期的记录）。"""
        with self._cursor() as cur:
            if add_date:
                cur.execute("DELETE FROM tdx_imports WHERE code=? AND add_date=?",
                            (code, add_date))
            else:
                cur.execute("DELETE FROM tdx_imports WHERE code=?", (code,))

    def count_calendar_day(self, day):
        """清空前的预统计：返回某日期在各表里的条目数.

        day: 'YYYY-MM-DD'. 用于清空前的确认对话框预览.
        """
        with self._cursor() as cur:
            n_scan = cur.execute(
                "SELECT COUNT(*) c FROM scan_history "
                "WHERE date(scan_time)=? OR trade_date=?",
                (day, day)).fetchone()["c"]
            n_watch = cur.execute(
                "SELECT COUNT(*) c FROM watch_pool WHERE add_date=?",
                (day,)).fetchone()["c"]
            n_tdx = cur.execute(
                "SELECT COUNT(*) c FROM tdx_imports WHERE add_date=?",
                (day,)).fetchone()["c"]
        return {"scan_history": n_scan, "watch_pool": n_watch, "tdx_imports": n_tdx}

    def clear_calendar_day(self, day):
        """一键清空某日期的全部数据（v0.8.14：行情日历 → 当日清单 → 🗑️ 清空当日）.

        清空:
          - scan_history: 该日所有命中（layer=1 一层粗筛 + layer=2 二层精筛），
            匹配条件为 date(scan_time)=day OR trade_date=day（兼容历史日期对照扫描
            的 trade_date 字段）
          - watch_pool: 该日 add_date 的入池记录 + 关联的 trade_log
          - tdx_imports: 该日 add_date 的日历二级观察池条目

        返回 dict {scan_history, watch_pool, tdx_imports: int} 各表删除行数.
        """
        import datetime as _dt
        # 先取出该日的 watch_pool id 列表（用于级联清理 trade_log）
        with self._cursor() as cur:
            watch_ids = [r["id"] for r in cur.execute(
                "SELECT id FROM watch_pool WHERE add_date=?", (day,)).fetchall()]
            # 1) 清 scan_history（一层 + 二层全部）
            cur.execute(
                "DELETE FROM scan_history "
                "WHERE date(scan_time)=? OR trade_date=?",
                (day, day))
            n_scan = cur.rowcount
            # 2) 清 tdx_imports（先于 watch_pool 互不影响，但顺序无所谓）
            cur.execute("DELETE FROM tdx_imports WHERE add_date=?", (day,))
            n_tdx = cur.rowcount
            # 3) 清 trade_log（按 watch_id 列表）
            if watch_ids:
                placeholders = ",".join("?" * len(watch_ids))
                cur.execute(f"DELETE FROM trade_log WHERE watch_id IN ({placeholders})",
                            watch_ids)
            # 4) 清 watch_pool
            cur.execute("DELETE FROM watch_pool WHERE add_date=?", (day,))
            n_watch = cur.rowcount
        return {"scan_history": n_scan, "watch_pool": n_watch, "tdx_imports": n_tdx}

    # ---------------------------------------------------------------- 交易日志
    def add_trade(self, watch_id, code, name, trade_type, trigger_condition="",
                  trade_price=None, trade_volume=None, notes="", trade_time=None):
        import datetime
        trade_time = trade_time or datetime.datetime.now().isoformat(timespec="seconds")
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO trade_log(watch_id,code,name,trade_type,trigger_condition,"
                "trade_price,trade_volume,trade_time,notes) VALUES(?,?,?,?,?,?,?,?,?)",
                (watch_id, code, name, trade_type, trigger_condition,
                 trade_price, trade_volume, trade_time, notes))
            return cur.lastrowid

    def list_trades(self, code=None, trade_type=None, limit=500):
        sql = "SELECT * FROM trade_log WHERE 1=1"
        args = []
        if code:
            sql += " AND code=?"
            args.append(code)
        if trade_type:
            sql += " AND trade_type=?"
            args.append(trade_type)
        sql += " ORDER BY trade_time DESC LIMIT ?"
        args.append(limit)
        with self._cursor() as cur:
            rows = cur.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def delete_trade(self, trade_id):
        with self._cursor() as cur:
            cur.execute("DELETE FROM trade_log WHERE id=?", (trade_id,))
            cur.execute("DELETE FROM factor_snapshot WHERE trade_id=?", (trade_id,))

    # ---------------------------------------------------------------- 因子快照
    def add_snapshot(self, trade_id, stock_code, market, indicators, market_index=None, volume=None):
        import datetime
        now = datetime.datetime.now().isoformat(timespec="seconds")
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO factor_snapshot(trade_id,stock_code,market,snapshot_time,"
                "indicators,market_index,volume) VALUES(?,?,?,?,?,?,?)",
                (trade_id, stock_code, market, now,
                 json.dumps(indicators or {}, ensure_ascii=False), market_index, volume))
            sid = cur.lastrowid
            cur.execute("UPDATE trade_log SET snapshot_id=? WHERE id=?", (sid, trade_id))
            return sid

    def get_snapshot(self, snapshot_id):
        with self._cursor() as cur:
            row = cur.execute("SELECT * FROM factor_snapshot WHERE id=?", (snapshot_id,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ 提醒
    def add_alert(self, code, name, market, alert_type, operator, threshold, params=None):
        import datetime
        now = datetime.datetime.now().isoformat(timespec="seconds")
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO alerts(code,name,market,alert_type,operator,threshold,"
                "params,enabled,created_at) VALUES(?,?,?,?,?,?,?,1,?)",
                (code, name, market, alert_type, operator, threshold,
                 json.dumps(params or {}, ensure_ascii=False), now))
            return cur.lastrowid

    def list_alerts(self, enabled_only=False):
        sql = "SELECT * FROM alerts"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id DESC"
        with self._cursor() as cur:
            rows = cur.execute(sql).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # B003 修复：params 落库为 JSON 字符串，返回时统一解析为 dict
            try:
                d["params"] = json.loads(d.get("params") or "{}")
            except (json.JSONDecodeError, TypeError):
                d["params"] = {}
            out.append(d)
        return out

    def update_alert(self, alert_id, **fields):
        allowed = {"alert_type", "operator", "threshold", "params", "enabled"}
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k}=?")
                vals.append(json.dumps(v, ensure_ascii=False) if k == "params" and v is not None else v)
        if not sets:
            return
        vals.append(alert_id)
        with self._cursor() as cur:
            cur.execute(f"UPDATE alerts SET {', '.join(sets)} WHERE id=?", vals)

    def delete_alert(self, alert_id):
        with self._cursor() as cur:
            cur.execute("DELETE FROM alerts WHERE id=?", (alert_id,))

    def mark_alert_triggered(self, alert_id, message):
        import datetime
        now = datetime.datetime.now().isoformat(timespec="seconds")
        with self._cursor() as cur:
            row = cur.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
            cur.execute("UPDATE alerts SET last_triggered_at=?, triggered_count=triggered_count+1 "
                        "WHERE id=?", (now, alert_id))
            if row:
                cur.execute("INSERT INTO alert_log(alert_id,code,name,message,created_at) "
                            "VALUES(?,?,?,?,?)",
                            (alert_id, row["code"], row["name"], message, now))

    def list_alert_logs(self, limit=300):
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT * FROM alert_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ 策略
    def add_strategy(self, name, kind="formula", formula="", conditions=None,
                     scan_mode="batch", enabled=1, description="",
                     parent_strategy_id=None, level=1, tags="",
                     parent_strategy_ids=None, tdx_alias=""):
        """新增策略：自动分配编号（S0001...，删除不回收），并写入 v1 版本 + 归档文件。
        v0.8.1 重构：
        - level=业务层级（1=方向/2=精细/3+=预留）
        - tags=分类标签（level=1 自由文本，B1/B2/超短等；不影响候选流转）
        - parent_strategy_ids=多选父策略（level≥2 时使用，JSON 数组）
        - parent_strategy_id=单选父策略（兼容旧策略，新策略不用）
        - direction 字段已废弃（保留列不再写入）
        v0.8.15：tdx_alias=TOML 通达信 cfg name 字段别名（≤8字节 GBK，留空则 fallback 用 name 截断）
        """
        if parent_strategy_ids is None:
            parent_strategy_ids = []
        if not isinstance(parent_strategy_ids, list):
            parent_strategy_ids = [int(parent_strategy_ids)]
        parent_strategy_ids_json = json.dumps(parent_strategy_ids, ensure_ascii=False)
        import datetime
        now = datetime.datetime.now().isoformat(timespec="seconds")
        with self._cursor() as cur:
            maxrow = cur.execute(
                "SELECT code FROM strategies WHERE code LIKE 'S%' "
                "ORDER BY code DESC LIMIT 1").fetchone()
            nxt = 1
            if maxrow and maxrow["code"] and maxrow["code"][1:].isdigit():
                nxt = int(maxrow["code"][1:]) + 1
            code = f"S{nxt:04d}"
            cur.execute(
                "INSERT INTO strategies(code,name,kind,formula,conditions,description,"
                "enabled,scan_mode,created_at,updated_at,parent_strategy_id,"
                "level,tags,parent_strategy_ids,tdx_alias) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (code, name, kind, formula,
                 json.dumps(conditions or {}, ensure_ascii=False), description or "",
                 enabled, scan_mode, now, now, parent_strategy_id,
                 int(level), tags or "", parent_strategy_ids_json,
                 tdx_alias or ""))
            sid = cur.lastrowid
            cur.execute(
                "INSERT INTO strategy_versions(strategy_id,code,version,name,kind,"
                "formula,description,scan_mode,created_at) VALUES(?,?,1,?,?,?,?,?,?)",
                (sid, code, name, kind, formula, description or "", scan_mode, now))
        strategy_archive.archive_strategy_version(
            {"code": code, "name": name, "kind": kind, "formula": formula,
             "description": description or "", "scan_mode": scan_mode}, 1)
        return sid

    def list_strategies(self, enabled_only=False, kind=None, level=None, tags=None):
        """列出策略。可按 kind/level/tags 过滤。
        v0.8.1：direction 字段已废弃，改为 tags（分类标签）。
        """
        sql = "SELECT * FROM strategies WHERE 1=1"
        args = []
        if enabled_only:
            sql += " AND enabled=1"
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if level is not None:
            sql += " AND level=?"
            args.append(int(level))
        if tags:
            sql += " AND tags LIKE ?"
            args.append(f"%{tags}%")
        sql += " ORDER BY level ASC, id DESC"
        with self._cursor() as cur:
            rows = cur.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def get_strategy(self, strategy_id):
        with self._cursor() as cur:
            row = cur.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
        return dict(row) if row else None

    def update_strategy(self, strategy_id, **fields):
        """更新策略。公式/说明书/类型/层级/标签/父策略变化时自动生成新版本并归档。
        v0.8.1：direction 字段已废弃，改为 tags（level=1 分类标签）
        + parent_strategy_ids（level≥2 多选父策略 JSON）。"""
        allowed = {"name", "kind", "formula", "conditions", "description",
                   "enabled", "scan_mode", "parent_strategy_id",
                   "level", "tags", "parent_strategy_ids", "tdx_alias"}
        import datetime
        now = datetime.datetime.now().isoformat(timespec="seconds")
        archived = None
        with self._cursor() as cur:
            row = cur.execute("SELECT * FROM strategies WHERE id=?",
                              (strategy_id,)).fetchone()
            if row is None:
                return
            old_f = row["formula"] or ""
            old_d = row["description"] or ""
            old_tags = row["tags"] or ""
            try:
                old_psids = json.loads(row["parent_strategy_ids"] or "[]")
            except (ValueError, TypeError):
                old_psids = []
            new_f = fields.get("formula", old_f) or ""
            new_d = fields.get("description", old_d) or ""
            new_name = fields.get("name", row["name"])
            new_kind = fields.get("kind", row["kind"])
            new_mode = fields.get("scan_mode", row["scan_mode"])
            new_level = int(fields.get("level", row["level"] or 1))
            new_tags = fields.get("tags", old_tags)
            new_psids_raw = fields.get("parent_strategy_ids", old_psids)
            if isinstance(new_psids_raw, str):
                try:
                    new_psids = json.loads(new_psids_raw)
                except (ValueError, TypeError):
                    new_psids = []
            else:
                new_psids = list(new_psids_raw) if new_psids_raw else []
            new_psids_json = json.dumps(new_psids, ensure_ascii=False)
            # 版本触发条件：公式 / 说明书 / 类型 / 层级 / 标签 / 父策略 变化
            version_changed = (new_f != old_f or new_d != old_d
                               or new_kind != row["kind"]
                               or new_level != (row["level"] or 1)
                               or new_tags != old_tags
                               or sorted(new_psids) != sorted(old_psids))
            if version_changed:
                vrow = cur.execute(
                    "SELECT MAX(version) AS v FROM strategy_versions "
                    "WHERE strategy_id=?", (strategy_id,)).fetchone()
                ver = (vrow["v"] or 0) + 1
                cur.execute(
                    "INSERT INTO strategy_versions(strategy_id,code,version,name,kind,"
                    "formula,description,scan_mode,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (strategy_id, row["code"], ver, new_name, new_kind,
                     new_f, new_d, new_mode, now))
                archived = ({"code": row["code"], "name": new_name, "kind": new_kind,
                             "formula": new_f, "description": new_d,
                             "scan_mode": new_mode}, ver)
            sets, vals = ["updated_at=?"], [now]
            for k, v in fields.items():
                if k not in allowed or v is None:
                    continue
                if k == "conditions":
                    sets.append(f"{k}=?")
                    vals.append(json.dumps(v, ensure_ascii=False))
                elif k == "parent_strategy_ids":
                    sets.append(f"{k}=?")
                    vals.append(new_psids_json)
                else:
                    sets.append(f"{k}=?")
                    vals.append(v)
            vals.append(strategy_id)
            cur.execute(f"UPDATE strategies SET {', '.join(sets)} WHERE id=?", vals)
        if archived:
            strategy_archive.archive_strategy_version(*archived)

    def delete_strategy(self, strategy_id):
        """删除策略（版本归档文件保留，不随删除消失）。"""
        with self._cursor() as cur:
            cur.execute("DELETE FROM strategies WHERE id=?", (strategy_id,))

    # ------------------------------------------------------------ 版本历史
    def list_strategy_versions(self, strategy_id, limit=200):
        """策略迭代版本列表（新版本在前）。"""
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT * FROM strategy_versions WHERE strategy_id=? "
                "ORDER BY version DESC LIMIT ?", (strategy_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_strategy_version(self, strategy_id, version):
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT * FROM strategy_versions WHERE strategy_id=? AND version=?",
                (strategy_id, version)).fetchone()
        return dict(row) if row else None

    def get_strategy_latest_version(self, strategy_id):
        """当前最新版本号（无版本记录返回 1）。"""
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT MAX(version) AS v FROM strategy_versions "
                "WHERE strategy_id=?", (strategy_id,)).fetchone()
        return row["v"] or 1

    # ------------------------------------------------------ 策略导入/导出（v0.10.5）
    # 导出字段：只带"策略本身"，不含回测统计（那是本地历史推出来的，换机器没意义）
    STRATEGY_EXPORT_FIELDS = (
        "id", "code", "name", "kind", "formula", "conditions", "description",
        "enabled", "scan_mode", "level", "tags", "tdx_alias",
        "parent_strategy_id", "parent_strategy_ids", "created_at", "updated_at",
    )

    def export_strategies(self):
        """导出全部策略（用于策略迁移）。"""
        cols = ", ".join(self.STRATEGY_EXPORT_FIELDS)
        with self._cursor() as cur:
            rows = cur.execute(f"SELECT {cols} FROM strategies ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def export_strategy_versions(self):
        """导出全部策略的历史版本。"""
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT strategy_id, code, version, name, kind, formula, description, "
                "scan_mode, created_at FROM strategy_versions ORDER BY strategy_id, version"
            ).fetchall()
        return [dict(r) for r in rows]

    def import_strategy_bundle(self, bundle, mode="merge"):
        """导入策略包（v0.10.5）。

        mode:
            merge   —— 按 code（优先）或 name 匹配：已存在则更新，不存在则新增；
            replace —— 先清空本地全部策略与版本，再按包内容重建。

        父策略引用按 id 映射重写（跨机器 id 必然不同，所以先落库再回填）。
        返回统计 dict：{"added","updated","skipped","versions_added","id_map","errors"}
        """
        rows = list(bundle.get("strategies") or [])
        versions = list(bundle.get("strategy_versions") or [])
        mode = (mode or "merge").lower()

        def _as_list(v):
            if v is None:
                return []
            if isinstance(v, (list, tuple)):
                return list(v)
            try:
                parsed = json.loads(v)
                return list(parsed) if isinstance(parsed, list) else []
            except (ValueError, TypeError):
                return []

        import datetime as _dt
        now = _dt.datetime.now().isoformat(timespec="seconds")

        stats = {"added": 0, "updated": 0, "skipped": 0,
                 "versions_added": 0, "versions_skipped": 0,
                 "id_map": {}, "errors": []}

        with self._cursor() as cur:
            if mode == "replace":
                cur.execute("DELETE FROM strategy_versions")
                cur.execute("DELETE FROM strategies")

            by_code = {r["code"]: r["id"] for r in
                       cur.execute("SELECT id, code FROM strategies "
                                   "WHERE code IS NOT NULL AND code<>''").fetchall()}
            by_name = {r["name"]: r["id"] for r in
                       cur.execute("SELECT id, name FROM strategies").fetchall()}

            for rec in rows:
                name = (rec.get("name") or "").strip()
                code = (rec.get("code") or "").strip()
                if not name:
                    stats["skipped"] += 1
                    stats["errors"].append("跳过一条没有名称的策略")
                    continue

                old_id = rec.get("id")
                target = None
                if code and code in by_code:
                    target = by_code[code]
                elif name in by_name:
                    target = by_name[name]

                fields = {
                    "name": name,
                    "kind": rec.get("kind") or "formula",
                    "formula": rec.get("formula") or "",
                    "conditions": rec.get("conditions") or "{}",
                    "description": rec.get("description") or "",
                    "enabled": 1 if rec.get("enabled") else 0,
                    "scan_mode": rec.get("scan_mode") or "batch",
                    "level": int(rec.get("level") or 1),
                    "tags": rec.get("tags") or "",
                    "tdx_alias": rec.get("tdx_alias") or "",
                    "parent_strategy_id": rec.get("parent_strategy_id"),
                    "parent_strategy_ids": rec.get("parent_strategy_ids") or "[]",
                }
                try:
                    if target:
                        sets = ", ".join(f"{k}=?" for k in fields)
                        cur.execute(
                            f"UPDATE strategies SET {sets}, updated_at=? WHERE id=?",
                            (*fields.values(), now, target))
                        stats["updated"] += 1
                        new_id = target
                    else:
                        cols = ", ".join(fields)
                        marks = ", ".join("?" * len(fields))
                        cur.execute(
                            f"INSERT INTO strategies({cols}, code, created_at, updated_at) "
                            f"VALUES({marks}, ?, ?, ?)",
                            (*fields.values(), code or None, now, now))
                        new_id = cur.lastrowid
                        stats["added"] += 1
                        if code:
                            by_code[code] = new_id
                        by_name[name] = new_id
                    if old_id is not None:
                        stats["id_map"][int(old_id)] = new_id
                except Exception as e:  # noqa: BLE001
                    stats["skipped"] += 1
                    stats["errors"].append(f"{name}: {e}")

            # 第二遍：按 id 映射回填父策略引用（跨机器 id 不同）
            for rec in rows:
                old_id = rec.get("id")
                if old_id is None:
                    continue
                new_id = stats["id_map"].get(int(old_id))
                if not new_id:
                    continue
                mapped = [stats["id_map"][int(p)]
                          for p in _as_list(rec.get("parent_strategy_ids"))
                          if int(p) in stats["id_map"]]
                old_parent = rec.get("parent_strategy_id")
                new_parent = stats["id_map"].get(int(old_parent)) if old_parent else None
                # 老字段（单父策略）在旧库里可能为空：只有一个父策略时补上，
                # 保证新库里的 legacy 分支也能正确串联
                if new_parent is None and len(mapped) == 1:
                    new_parent = mapped[0]
                cur.execute(
                    "UPDATE strategies SET parent_strategy_ids=?, parent_strategy_id=? "
                    "WHERE id=?",
                    (json.dumps(mapped, ensure_ascii=False), new_parent, new_id))

            # 版本历史（同策略同版本号已存在则跳过）
            for v in versions:
                old_sid = v.get("strategy_id")
                if old_sid is None or int(old_sid) not in stats["id_map"]:
                    stats["versions_skipped"] += 1     # 策略已删除的孤儿版本
                    continue
                sid = stats["id_map"][int(old_sid)]
                ver = int(v.get("version") or 1)
                exists = cur.execute(
                    "SELECT 1 FROM strategy_versions WHERE strategy_id=? AND version=?",
                    (sid, ver)).fetchone()
                if exists:
                    continue
                cur.execute(
                    "INSERT INTO strategy_versions(strategy_id,code,version,name,kind,"
                    "formula,description,scan_mode,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (sid, v.get("code") or "", ver, v.get("name") or "",
                     v.get("kind") or "formula", v.get("formula") or "",
                     v.get("description") or "", v.get("scan_mode") or "batch",
                     v.get("created_at") or now))
                stats["versions_added"] += 1

        log.info("策略导入完成（%s）：新增 %d / 更新 %d / 跳过 %d / 版本 %d",
                 mode, stats["added"], stats["updated"], stats["skipped"],
                 stats["versions_added"])
        return stats

    # -------------------------------------------------------------- 扫描历史
    def _active_watch_codes(self):
        """当前观察池中"有效"状态（待审核/观察中/已开仓）的股票代码集合。"""
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT DISTINCT code FROM watch_pool "
                "WHERE status IN ('待审核','观察中','已开仓')").fetchall()
        return {str(r["code"]) for r in rows}

    def add_scan_hits(self, session_id, strategy_id, strategy_name, hits, layer=1,
                    direction="", parent_strategy_ids=None):
        """批量写入一次扫描的命中记录。hits: [ScanResult, ...]
        layer: 1=一层粗筛 2=二层DNA精筛；DNA 字段从 ScanResult 属性读取。
        v0.8.1：direction 字段已废弃，parent_strategy_ids (JSON) 取代之。
        v0.7.4：二层记录额外持久化 dtw_score/final_score/matched_template/
        buy_level/signals/trade_date/in_watchlist（in_watchlist 按当前观察池有效状态计算）。
        """
        if parent_strategy_ids is None:
            parent_strategy_ids = []
        if not isinstance(parent_strategy_ids, list):
            parent_strategy_ids = [int(parent_strategy_ids)]
        psids_json = json.dumps(parent_strategy_ids, ensure_ascii=False)
        import datetime
        now = datetime.datetime.now().isoformat(timespec="seconds")
        active = self._active_watch_codes()
        with self._cursor() as cur:
            cur.executemany(
                "INSERT INTO scan_history(session_id,strategy_id,strategy_name,scan_time,"
                "code,name,market,price,pct,reason,layer,direction,"
                "parent_strategy_ids,"
                "dna_score,dna_template,"
                "dna_detail,signal_date,dtw_score,matched_template,final_score,"
                "buy_level,signals,trade_date,in_watchlist) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(session_id, strategy_id, strategy_name, now,
                  h.code, h.name, h.market, h.price, h.pct,
                  (h.match_reason or "")[:300], layer,
                  direction or getattr(h, "direction", "") or "",
                  psids_json,
                  getattr(h, "dna_score", None),
                  getattr(h, "dna_template", None),
                  getattr(h, "dna_detail", None),
                  getattr(h, "signal_date", None),
                  getattr(h, "dtw_score", getattr(h, "dna_score", None)),
                  getattr(h, "matched_template", getattr(h, "dna_template", None)),
                  getattr(h, "final_score", None),
                  getattr(h, "buy_level", None),
                  getattr(h, "signals", None),
                  getattr(h, "signal_date", None) or None,
                  1 if str(h.code) in active else 0) for h in hits])

    def update_scan_in_watchlist(self, code, day=None, flag=1):
        """将某票（某交易日的）扫描命中标记为已入观察池（in_watchlist=1）。
        day: 交易日（YYYY-MM-DD）；None=标记该票全部命中。
        返回更新行数。"""
        with self._cursor() as cur:
            if day:
                cur.execute(
                    "UPDATE scan_history SET in_watchlist=? WHERE code=? "
                    "AND COALESCE(trade_date, date(scan_time))=?",
                    (int(flag), str(code), day))
            else:
                cur.execute("UPDATE scan_history SET in_watchlist=? WHERE code=?",
                            (int(flag), str(code)))
            return cur.rowcount

    def mark_session_in_watchlist(self, session_id, flag=1):
        """把某扫描会话的所有命中标记为已入观察池（自动进池后调用）。"""
        with self._cursor() as cur:
            cur.execute("UPDATE scan_history SET in_watchlist=? WHERE session_id=?",
                        (int(flag), str(session_id)))

    def delete_scan_hit(self, code, day=None, layer=None, strategy_id=None):
        """删除 scan_history 中的命中记录（v0.8.0：行情日历手动删除）。

        code: 必填股票代码
        day: 交易日（YYYY-MM-DD）；None=删除该票所有命中
        layer: 1=粗筛 / 2=精筛；None=不限
        strategy_id: 仅删除某策略的命中；None=不限
        返回删除行数。
        """
        sql = "DELETE FROM scan_history WHERE code=?"
        args = [str(code)]
        if day:
            sql += " AND COALESCE(trade_date, date(scan_time))=?"
            args.append(day)
        if layer is not None:
            sql += " AND layer=?"
            args.append(int(layer))
        if strategy_id is not None:
            sql += " AND strategy_id=?"
            args.append(int(strategy_id))
        with self._cursor() as cur:
            cur.execute(sql, args)
            return cur.rowcount

    def get_bars_date_range(self):
        """日K缓存覆盖的日期范围 (min_date, max_date)；无缓存返回 (None, None)。

        用于历史日期扫描的日期选择器范围限制（范围外日期置灰）。
        """
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT MIN(date) AS d1, MAX(date) AS d2 FROM daily_bars_cache"
            ).fetchone()
        if not row or not row["d1"]:
            return None, None
        return row["d1"], row["d2"]

    def update_watch_dna(self, code, dna_score, dna_template, note="", level=2):
        """精筛命中时更新观察池中该票最新有效记录的 DNA 标注 + 层级升级。
        v0.8.0：level=精筛命中层级（默认2），同票同 add_date 精筛命中自动覆盖 level。
        """
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT id, reason, level FROM watch_pool "
                "WHERE code=? AND status IN ('待审核','观察中','已开仓') "
                "ORDER BY id DESC LIMIT 1", (str(code),)).fetchone()
            if not row:
                return False
            reason = row["reason"] or ""
            if note and f"精筛[{dna_template}]" not in reason:
                reason = (reason + note)[:500]
            # level 只升不降（精筛覆盖粗筛）
            new_level = max(int(row["level"] or 1), int(level))
            cur.execute(
                "UPDATE watch_pool SET dna_score=?, dna_template=?, reason=?, level=? "
                "WHERE id=?",
                (dna_score, dna_template, reason, new_level, row["id"]))
            return True

    def list_scan_history(self, strategy_id=None, code=None, day=None, limit=500):
        sql = "SELECT * FROM scan_history WHERE 1=1"
        args = []
        if strategy_id:
            sql += " AND strategy_id=?"
            args.append(strategy_id)
        if code:
            sql += " AND code=?"
            args.append(code)
        if day:
            # v0.7.4：优先按信号交易日（trade_date），旧数据回退到扫描日
            sql += " AND COALESCE(trade_date, date(scan_time))=?"
            args.append(day)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._cursor() as cur:
            rows = cur.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def list_scan_dates(self, year, month):
        """指定月份的命中日期列表（用于行情日历淡红标记）。"""
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT DISTINCT COALESCE(trade_date, date(scan_time)) AS d FROM scan_history "
                "WHERE strftime('%Y-%m', COALESCE(trade_date, scan_time))=?",
                (f"{year:04d}-{month:02d}",)).fetchall()
        return [r["d"] for r in rows if r["d"]]

    def count_cached_bars_on(self, day):
        """某日有多少只股票在本地缓存里已有日K（v0.10.3，判断该日数据是否落地）。"""
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT COUNT(DISTINCT code) AS c FROM daily_bars_cache WHERE date=?",
                (str(day)[:10],)).fetchone()
        return int(row["c"] or 0) if row else 0

    def get_market_breadth(self, day):
        """某交易日的市场宽度（v0.10.0，供选股日报使用）。

        以 daily_bars_cache 的「该日收盘 vs 上一交易日收盘」统计，
        只取目标日前后 30 天窗口，避免全表窗口排序。

        返回 {total, up, down, flat, avg_pct}；该日无缓存数据时返回 None。
        """
        sql = (
            "WITH d AS ("
            "  SELECT code, date, close,"
            "         LAG(close) OVER (PARTITION BY code ORDER BY date) AS prev"
            "  FROM daily_bars_cache"
            "  WHERE date <= ? AND date >= date(?, '-30 day')"
            "), t AS ("
            "  SELECT close, prev FROM d"
            "  WHERE date = ? AND prev IS NOT NULL AND prev > 0"
            ") "
            "SELECT COUNT(*) AS total,"
            "       SUM(CASE WHEN close > prev THEN 1 ELSE 0 END) AS up,"
            "       SUM(CASE WHEN close < prev THEN 1 ELSE 0 END) AS down,"
            "       SUM(CASE WHEN close = prev THEN 1 ELSE 0 END) AS flat,"
            "       AVG((close / prev - 1) * 100) AS avg_pct "
            "FROM t"
        )
        with self._cursor() as cur:
            row = cur.execute(sql, (day, day, day)).fetchone()
        if not row or not row["total"]:
            return None
        return {
            "total": int(row["total"]),
            "up": int(row["up"] or 0),
            "down": int(row["down"] or 0),
            "flat": int(row["flat"] or 0),
            "avg_pct": round(float(row["avg_pct"] or 0), 3),
        }

    def list_latest_layer_hits(self, layer=1, limit=500, parent_strategy_id=None,
                              strategy_ids=None):
        """最近一次指定层（1=粗筛 2=DNA精筛）扫描会话的命中列表。
        v0.8.1：direction 字段废弃；可按 parent_strategy_id 或 strategy_ids 过滤。
        无则返回空。"""
        # 候选 strategy_ids：None = 不过滤；list = 仅这些策略的命中
        with self._cursor() as cur:
            sql = "SELECT session_id FROM scan_history WHERE layer=?"
            args = [layer]
            if strategy_ids:
                placeholders = ",".join("?" * len(strategy_ids))
                sql += f" AND strategy_id IN ({placeholders})"
                args.extend(strategy_ids)
            sql += " ORDER BY id DESC LIMIT 1"
            row = cur.execute(sql, args).fetchone()
            if not row:
                return []
            sql = "SELECT * FROM scan_history WHERE session_id=? AND layer=?"
            args2 = [row["session_id"], layer]
            if strategy_ids:
                placeholders = ",".join("?" * len(strategy_ids))
                sql += f" AND strategy_id IN ({placeholders})"
                args2.extend(strategy_ids)
            sql += " ORDER BY id DESC LIMIT ?"
            args2.append(limit)
            rows = cur.execute(sql, args2).fetchall()
        return [dict(r) for r in rows]

    def list_scan_sessions(self, limit=50):
        """最近扫描会话（去重）。"""
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT session_id, strategy_id, strategy_name, MAX(scan_time) AS scan_time, "
                "COUNT(*) AS hits FROM scan_history GROUP BY session_id "
                "ORDER BY scan_time DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def hits_today(self, session_exclude=None):
        """当日命中（按 code+strategy 去重），用于新命中提醒。"""
        import datetime
        today = datetime.date.today().isoformat()
        sql = "SELECT code, name, strategy_id, strategy_name, MAX(scan_time) AS scan_time " \
              "FROM scan_history WHERE date(scan_time)=? "
        args = [today]
        if session_exclude:
            sql += " AND session_id != ?"
            args.append(session_exclude)
        sql += " GROUP BY code, strategy_id"
        with self._cursor() as cur:
            rows = cur.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------- 策略回测统计（v0.9.0）
    def save_backtest_stats(self, strategy_id, win_rate=None, profit_loss_ratio=None,
                            total_signals=None, avg_ret=None, avg_holding_days=None,
                            forward_returns=None, config=None, raw=None):
        """写入/更新某条策略的回测统计（策略管理列表中以标签形式展示）。

        - win_rate / profit_loss_ratio / avg_ret 等数值字段独立可空；
        - forward_returns: dict（如 {'1':{'pct_avg':..,'win_rate':..},...}），存为 JSON
        - config: dict，存为 JSON
        - raw: dict，原始统计快照（含 stats/exit_reason_dist 等）便于后续溯源

        每次调用都会写一条新的 backtest_stats 历史记录（保留时间序列）；
        最新数值同步到 strategies 表的回测展示列。
        """
        import datetime
        now = datetime.datetime.now().isoformat(timespec="seconds")
        fwd_json = json.dumps(forward_returns or {}, ensure_ascii=False)
        cfg_json = json.dumps(config or {}, ensure_ascii=False)
        raw_json = json.dumps(raw or {}, ensure_ascii=False)
        with self._cursor() as cur:
            # 1) 历史表：每次保存都留痕
            cur.execute(
                "CREATE TABLE IF NOT EXISTS strategy_backtest ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "strategy_id INTEGER,"
                "win_rate REAL, profit_loss_ratio REAL,"
                "total_signals INTEGER, avg_ret REAL, avg_holding_days REAL,"
                "forward_returns TEXT, config TEXT, raw TEXT,"
                "created_at TEXT)"
            )
            cur.execute(
                "INSERT INTO strategy_backtest(strategy_id,win_rate,profit_loss_ratio,"
                "total_signals,avg_ret,avg_holding_days,forward_returns,config,raw,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (strategy_id, win_rate, profit_loss_ratio, total_signals, avg_ret,
                 avg_holding_days, fwd_json, cfg_json, raw_json, now))
            # 2) strategies 表同步最新展示列（仅展示用，不参与扫描）
            cur.execute(
                "UPDATE strategies SET backtest_win_rate=?, backtest_profit_loss_ratio=?,"
                "backtest_total_signals=?, backtest_avg_ret=?, backtest_avg_holding_days=?,"
                "backtest_forward_returns=?, backtest_config=?, backtest_updated_at=? "
                "WHERE id=?",
                (win_rate, profit_loss_ratio, total_signals, avg_ret, avg_holding_days,
                 fwd_json, cfg_json, now, strategy_id))

    def get_latest_backtest_stats(self, strategy_id):
        """读取某条策略最近的回测统计快照（dict）；无则返回 None。"""
        import json as _json
        with self._cursor() as cur:
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='strategy_backtest'")
            if cur.fetchone() is None:
                return None
            row = cur.execute(
                "SELECT * FROM strategy_backtest WHERE strategy_id=? "
                "ORDER BY id DESC LIMIT 1", (strategy_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("forward_returns", "config", "raw"):
            if d.get(k):
                try:
                    d[k] = _json.loads(d[k])
                except (ValueError, TypeError):
                    d[k] = {}
            else:
                d[k] = {}
        return d

    def list_backtest_history(self, strategy_id, limit=20):
        """列出某策略的历史回测记录（新→旧）。"""
        with self._cursor() as cur:
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='strategy_backtest'")
            if cur.fetchone() is None:
                return []
            rows = cur.execute(
                "SELECT id, strategy_id, win_rate, profit_loss_ratio, total_signals,"
                " avg_ret, avg_holding_days, created_at FROM strategy_backtest "
                "WHERE strategy_id=? ORDER BY id DESC LIMIT ?",
                (strategy_id, limit)).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- K线缓存
    def cache_daily_bars(self, code, market, df):
        """把日K DataFrame 写入缓存（覆盖式）。"""
        if df is None or df.empty:
            return
        with self._cursor() as cur:
            cur.execute("DELETE FROM daily_bars_cache WHERE code=?", (str(code),))
            cur.executemany(
                "INSERT INTO daily_bars_cache(code,market,date,open,high,low,close,vol,amount) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                [(str(code), market, r.date, r.open, r.high, r.low, r.close, r.vol, r.amount)
                 for r in df.itertuples()])

    def get_cached_daily_bars(self, code):
        """读缓存日K（时间正序 DataFrame），无缓存返回空 DataFrame。"""
        import pandas as pd
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT date,open,high,low,close,vol,amount FROM daily_bars_cache "
                "WHERE code=? ORDER BY date", (str(code),)).fetchall()
        return pd.DataFrame([dict(r) for r in rows])

    def cache_latest_date(self, code):
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT MAX(date) AS d FROM daily_bars_cache WHERE code=?", (str(code),)).fetchone()
        return row["d"] if row and row["d"] else None

    def get_close_on_date(self, code, date):
        """指定交易日（YYYY-MM-DD）的收盘价；该日无K线返回 None。
        用于行情日历观察池「较加入日收盘价涨跌幅」计算（v0.7.5）。"""
        if not date:
            return None
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT close FROM daily_bars_cache WHERE code=? AND date=?",
                (str(code), str(date))).fetchone()
        return float(row["close"]) if row else None

    def get_latest_close(self, code):
        """最近一根日K收盘价（缓存）；无缓存返回 None。"""
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT close FROM daily_bars_cache WHERE code=? "
                "ORDER BY date DESC LIMIT 1", (str(code),)).fetchone()
        return float(row["close"]) if row else None

    def get_latest_bar_snapshot(self, code):
        """最近两根日K的收盘/最高/最低（缓存），用于非交易时段展示当日涨跌与振幅。
        返回 {"close","prev_close","high","low"}；数据不足返回 None。"""
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT close, high, low FROM daily_bars_cache WHERE code=? "
                "ORDER BY date DESC LIMIT 2", (str(code),)).fetchall()
        if len(rows) < 2:
            return None
        return {"close": float(rows[0]["close"]), "high": float(rows[0]["high"]),
                "low": float(rows[0]["low"]), "prev_close": float(rows[1]["close"])}

    # ---------------------------------------------------------- 除权除息事件缓存
    def get_xdxr_events(self, code):
        """读某票的除权除息事件（时间正序）。无缓存返回空列表。"""
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT date,category,fenhong,peigujia,songzhuangu,peigu "
                "FROM xdxr_events WHERE code=? ORDER BY date", (str(code),)).fetchall()
        return [dict(r) for r in rows]

    def save_xdxr_events(self, code, market, events):
        """覆盖式写入某票的除权除息事件缓存。"""
        if not events:
            return
        import datetime
        now = datetime.datetime.now().isoformat(timespec="seconds")
        with self._cursor() as cur:
            cur.execute("DELETE FROM xdxr_events WHERE code=?", (str(code),))
            cur.executemany(
                "INSERT INTO xdxr_events(code,market,date,category,fenhong,peigujia,"
                "songzhuangu,peigu,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                [(str(code), market, e["date"], int(e.get("category", 0)),
                  float(e.get("fenhong") or 0), float(e.get("peigujia") or 0),
                  float(e.get("songzhuangu") or 0), float(e.get("peigu") or 0), now)
                 for e in events])

    # ---------------------------------------------------------------- 行情缓存
    def save_market_snapshot(self, df):
        if df is None or df.empty:
            return
        import datetime
        now = datetime.datetime.now().isoformat(timespec="seconds")
        with self._cursor() as cur:
            cur.executemany(
                "INSERT INTO market_snapshot(code,name,market,price,last_close,open,high,low,"
                "vol,amount,pct,update_time) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name,market=excluded.market,"
                "price=excluded.price,last_close=excluded.last_close,open=excluded.open,"
                "high=excluded.high,low=excluded.low,vol=excluded.vol,amount=excluded.amount,"
                "pct=excluded.pct,update_time=excluded.update_time",
                [(r.code, r.name, r.market, r.price, r.last_close, r.open, r.high, r.low,
                  r.vol, r.amount, r.pct, now) for r in df.itertuples()])

    def load_market_snapshot(self, codes=None):
        if codes:
            ph = ",".join("?" * len(codes))
            with self._cursor() as cur:
                rows = cur.execute(
                    f"SELECT * FROM market_snapshot WHERE code IN ({ph})", codes).fetchall()
        else:
            with self._cursor() as cur:
                rows = cur.execute("SELECT * FROM market_snapshot").fetchall()
        return [dict(r) for r in rows]

    def save_stock_list(self, df):
        if df is None or df.empty:
            return
        with self._cursor() as cur:
            cur.executemany(
                "INSERT OR REPLACE INTO stock_list(code,name,market) VALUES(?,?,?)",
                [(r.code, r.name, r.market) for r in df.itertuples()])

    def load_stock_list(self):
        with self._cursor() as cur:
            rows = cur.execute("SELECT * FROM stock_list").fetchall()
        return [dict(r) for r in rows]

    def close(self):
        with self._lock:
            self._conn.close()
