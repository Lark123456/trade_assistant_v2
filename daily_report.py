# -*- coding: utf-8 -*-
"""选股日报：把某一天的全部扫描结果汇总成文档 + 飞书消息（v0.10.0）。

分三块：
    1. ``DailyReportConfig`` —— 定时扫描 / 飞书推送的配置（存 settings 表）；
    2. ``build_daily_report`` —— 从 scan_history 汇总出 Markdown 文档与飞书行；
    3. ``should_fire`` / ``save_report`` —— 定时判定与本地落盘。

设计要点：
    - 同一策略当天可能被扫多次（手动重跑、二层父策略重跑），日报只取
      **最近一次会话**的结果，避免重复计数；
    - 文档按「一层粗筛 / 二层精筛 / 观察池 / 提醒」四段组织，每段按命中数排序；
    - 飞书消息是精简版（每策略最多 N 只），完整版落本地 markdown。
"""
from __future__ import annotations

import datetime
import logging
import os
from dataclasses import dataclass, field

from config import BASE_DIR

log = logging.getLogger("daily_report")

REPORT_DIR = BASE_DIR / "reports"

# settings 键名
K_ENABLED = "daily_report_enabled"
K_TIME = "daily_report_time"
K_WEEKDAY_ONLY = "daily_report_weekday_only"
# 推送方式：app = 自建应用机器人(app_id+app_secret+receive_id) / webhook = 自定义机器人
K_MODE = "feishu_mode"
K_WEBHOOK = "feishu_webhook"
K_SECRET = "feishu_secret"
K_APP_ID = "feishu_app_id"
K_APP_SECRET = "feishu_app_secret"
K_RECEIVE_ID = "feishu_receive_id"
K_RECEIVE_ID_TYPE = "feishu_receive_id_type"
K_MAX_PER = "daily_report_max_per_strategy"
K_INCLUDE_WATCH = "daily_report_include_watch"
K_INCLUDE_ALERT = "daily_report_include_alert"
K_SAVE_LOCAL = "daily_report_save_local"
K_CATCHUP = "daily_report_catchup"
K_LAST_RUN = "daily_report_last_run"


def _as_bool(v, default=False):
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on", "是")


# ---------------------------------------------------------------------------
#  配置
# ---------------------------------------------------------------------------
@dataclass
class DailyReportConfig:
    """定时扫描 + 飞书推送配置。

    推送方式 mode：
        - ``app``     ：自建应用机器人（app_id + app_secret + receive_id），v0.10.2 默认
        - ``webhook`` ：自定义机器人（一条 Webhook 地址）
    """

    enabled: bool = False
    hhmm: str = "15:30"
    weekday_only: bool = True
    mode: str = "app"
    webhook: str = ""
    secret: str = ""
    app_id: str = ""
    app_secret: str = ""
    receive_id: str = ""
    receive_id_type: str = "chat_id"
    max_per_strategy: int = 10
    include_watch: bool = True
    include_alert: bool = True
    save_local: bool = True
    catchup: bool = False
    last_run: str = ""

    # ---------------- 读写 ----------------
    @classmethod
    def load(cls, db):
        def g(key, default=""):
            return db.get_setting(key, default)

        def gi(key, default):
            try:
                return int(g(key, default) or default)
            except (TypeError, ValueError):
                return default

        webhook = (g(K_WEBHOOK, "") or "").strip()
        mode = (g(K_MODE, "") or "").strip().lower()
        if not mode:
            # 老配置没有 feishu_mode：填了 webhook 就走自定义机器人
            mode = "webhook" if webhook else "app"

        return cls(
            enabled=_as_bool(g(K_ENABLED), False),
            hhmm=(g(K_TIME, "15:30") or "15:30").strip(),
            weekday_only=_as_bool(g(K_WEEKDAY_ONLY), True),
            mode=mode if mode in ("app", "webhook") else "app",
            webhook=webhook,
            secret=(g(K_SECRET, "") or "").strip(),
            app_id=(g(K_APP_ID, "") or "").strip(),
            app_secret=(g(K_APP_SECRET, "") or "").strip(),
            receive_id=(g(K_RECEIVE_ID, "") or "").strip(),
            receive_id_type=(g(K_RECEIVE_ID_TYPE, "chat_id") or "chat_id").strip(),
            max_per_strategy=max(1, gi(K_MAX_PER, 10)),
            include_watch=_as_bool(g(K_INCLUDE_WATCH), True),
            include_alert=_as_bool(g(K_INCLUDE_ALERT), True),
            save_local=_as_bool(g(K_SAVE_LOCAL), True),
            catchup=_as_bool(g(K_CATCHUP), False),
            last_run=(g(K_LAST_RUN, "") or "").strip(),
        )

    def credentials(self):
        """给 feishu.send_message 用的凭证字典。"""
        return {
            "mode": self.mode,
            "webhook": self.webhook,
            "secret": self.secret,
            "app_id": self.app_id,
            "app_secret": self.app_secret,
            "receive_id": self.receive_id,
            "receive_id_type": self.receive_id_type or "chat_id",
        }

    def save(self, db):
        db.set_setting(K_ENABLED, "1" if self.enabled else "0")
        db.set_setting(K_TIME, self.hhmm)
        db.set_setting(K_WEEKDAY_ONLY, "1" if self.weekday_only else "0")
        db.set_setting(K_MODE, self.mode)
        db.set_setting(K_WEBHOOK, self.webhook.strip())
        db.set_setting(K_SECRET, self.secret.strip())
        db.set_setting(K_APP_ID, self.app_id.strip())
        db.set_setting(K_APP_SECRET, self.app_secret.strip())
        db.set_setting(K_RECEIVE_ID, self.receive_id.strip())
        db.set_setting(K_RECEIVE_ID_TYPE, self.receive_id_type or "chat_id")
        db.set_setting(K_MAX_PER, str(self.max_per_strategy))
        db.set_setting(K_INCLUDE_WATCH, "1" if self.include_watch else "0")
        db.set_setting(K_INCLUDE_ALERT, "1" if self.include_alert else "0")
        db.set_setting(K_SAVE_LOCAL, "1" if self.save_local else "0")
        db.set_setting(K_CATCHUP, "1" if self.catchup else "0")
        db.set_setting(K_LAST_RUN, self.last_run)

    def mark_ran(self, db, day=None):
        self.last_run = day or datetime.date.today().isoformat()
        db.set_setting(K_LAST_RUN, self.last_run)


# ---------------------------------------------------------------------------
#  定时判定
# ---------------------------------------------------------------------------
def parse_hhmm(text, default=(15, 30)):
    """'15:30' -> (15, 30)；非法值回退默认。"""
    try:
        hh, mm = str(text).strip().split(":")[:2]
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, AttributeError):
        pass
    return default


# 探测「今天有没有日K」用的代表性标的（流动性最好、几乎不会停牌）
# (代码, market)  market: 0=深市 1=沪市
PROBE_CODES = (("600519", 1), ("000001", 0), ("601318", 1))


def resolve_scan_date(db, probe=None, today=None):
    """确定定时扫描的信号日（v0.10.3）。

    优先今天；今天日K还没落地（法定节假日 / 行情源滞后 / 收盘数据未出）时，
    回退到**最近一个有数据的交易日**，避免推出一份空日报。

    probe: ``probe(code, market) -> 'YYYY-MM-DD' | None``，查某标的最后一根K线日期；
           None 时只用本地缓存判断。
    返回 ``(信号日, 说明)``。
    """
    today = today or datetime.date.today().isoformat()

    cached_max = None
    try:
        _mn, mx = db.get_bars_date_range()
        cached_max = str(mx)[:10] if mx else None
    except Exception as e:  # noqa: BLE001
        log.debug("读取缓存日期范围失败: %s", e)

    if cached_max == today:
        return today, f"本地缓存里已有 {today} 的日K"

    probe_max = None
    if probe is not None:
        for code, market in PROBE_CODES:
            try:
                d = probe(code, market)
            except Exception as e:  # noqa: BLE001
                log.debug("探测 %s 日K失败: %s", code, e)
                d = None
            if d:
                d = str(d)[:10]
                if probe_max is None or d > probe_max:
                    probe_max = d
            if probe_max == today:
                break

    if probe_max == today:
        return today, f"探测到 {today} 的日K已可获取"
    if probe_max and probe_max < today:
        return probe_max, (f"今天（{today}）还没有日K（非交易日或行情未落地），"
                           f"自动回退到最近交易日 {probe_max}")
    if cached_max and cached_max < today:
        return cached_max, f"探测不可用，按缓存里最近的交易日 {cached_max} 执行"
    return today, "按今天执行"


def should_fire(cfg: DailyReportConfig, now=None, grace_minutes=120):
    """判断此刻是否该触发定时任务，返回 (bool, 原因)。

    ``grace_minutes``：到点后允许的迟到窗口。程序在设定时间之后才启动时，
    窗口内仍会自动执行；超过窗口需勾选「启动时补跑」才会当天补执行。
    """
    now = now or datetime.datetime.now()
    if not cfg.enabled:
        return False, "未启用定时扫描"
    if cfg.weekday_only and now.weekday() >= 5:
        return False, "周末不执行"
    if cfg.last_run == now.date().isoformat():
        return False, "今天已经执行过"
    hh, mm = parse_hhmm(cfg.hhmm)
    due = datetime.datetime.combine(now.date(), datetime.time(hh, mm))
    if now < due:
        return False, f"未到设定时间 {cfg.hhmm}"
    late_min = (now - due).total_seconds() / 60.0
    if not cfg.catchup and late_min > grace_minutes:
        return False, (f"已过设定时间 {late_min:.0f} 分钟（超过 {grace_minutes} 分钟宽限），"
                       f"如需当天补执行请勾选「启动时补跑」")
    return True, "到点执行" if late_min < 1 else f"补执行（迟到 {late_min:.0f} 分钟）"


# ---------------------------------------------------------------------------
#  文档构造
# ---------------------------------------------------------------------------
def _fmt_pct(v):
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "-"


def _fmt_num(v, nd=2):
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "-"


def _latest_sessions(rows):
    """同策略同层只保留最近一次会话的记录（rows 需按 id DESC 排序）。"""
    latest = {}
    for r in rows:
        key = (r.get("strategy_id"), int(r.get("layer") or 1))
        if key not in latest:
            latest[key] = r.get("session_id")
    keep, seen = [], set()
    for r in rows:
        key = (r.get("strategy_id"), int(r.get("layer") or 1))
        if latest.get(key) != r.get("session_id"):
            continue
        code = (r.get("code") or "")
        if (key, code) in seen:
            continue
        seen.add((key, code))
        keep.append(r)
    return keep


def build_daily_report(db, day=None, max_per_strategy=10,
                       include_watch=True, include_alert=True):
    """汇总某一天的全部扫描结果。

    返回 dict：
        day / title / markdown / feishu_lines / stats / sections
    """
    day = day or datetime.date.today().isoformat()
    max_per_strategy = max(1, int(max_per_strategy or 10))

    rows = db.list_scan_history(day=day, limit=20000)
    rows = _latest_sessions(rows)

    # ---- 分组：一层 / 二层 ----
    groups = {}
    for r in rows:
        layer = int(r.get("layer") or 1)
        name = r.get("strategy_name") or "未命名策略"
        groups.setdefault((layer, name), []).append(r)

    sections = []
    for (layer, name), items in groups.items():
        items.sort(key=lambda x: (x.get("pct") is None, -(x.get("pct") or 0)))
        sections.append({"layer": layer, "strategy": name,
                         "count": len(items), "rows": items})
    sections.sort(key=lambda s: (s["layer"], -s["count"]))

    n1 = sum(s["count"] for s in sections if s["layer"] == 1)
    n2 = sum(s["count"] for s in sections if s["layer"] == 2)
    total = n1 + n2
    n_strategies = len(sections)

    # ---- 市场宽度（可选，失败不影响主流程）----
    breadth = None
    try:
        breadth = db.get_market_breadth(day)
    except Exception as e:  # noqa: BLE001
        log.debug("市场宽度统计失败: %s", e)

    # ---- 观察池 / 提醒 ----
    watch_new = []
    if include_watch:
        try:
            watch_new = [w for w in db.list_watch()
                         if str(w.get("add_date") or "")[:10] == day]
        except Exception as e:  # noqa: BLE001
            log.debug("观察池统计失败: %s", e)

    alerts = []
    if include_alert:
        try:
            alerts = [a for a in db.list_alert_logs(limit=2000)
                      if str(a.get("created_at") or "")[:10] == day]
        except Exception as e:  # noqa: BLE001
            log.debug("提醒统计失败: %s", e)

    # ================= Markdown 文档 =================
    md = [f"# 选股日报 · {day}", ""]
    md.append(f"- 生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
    md.append(f"- 统计策略：{n_strategies} 个（一层 {sum(1 for s in sections if s['layer'] == 1)}"
              f" / 二层 {sum(1 for s in sections if s['layer'] == 2)}）")
    md.append(f"- 命中合计：**{total}** 只（一层 {n1} / 二层 {n2}）")
    if breadth:
        md.append(f"- 市场宽度：上涨 {breadth.get('up', 0)} 家 / 下跌 {breadth.get('down', 0)} 家"
                  f" / 平均 {_fmt_pct(breadth.get('avg_pct'))}"
                  f"（覆盖 {breadth.get('total', 0)} 只）")
    md.append("")

    if not sections:
        md.append("> 当日没有任何扫描命中记录。")
        md.append("")

    for layer, label in ((1, "一、一层粗筛"), (2, "二、二层精筛")):
        part = [s for s in sections if s["layer"] == layer]
        if not part:
            continue
        md.append(f"## {label}")
        md.append("")
        for s in part:
            md.append(f"### {s['strategy']}（{s['count']} 只）")
            md.append("")
            if layer == 2:
                md.append("| 代码 | 名称 | 综合分 | 骨架分 | 买点定级 | 最佳模具 | 收盘 |")
                md.append("|---|---|---|---|---|---|---|")
                for r in s["rows"][:max_per_strategy]:
                    md.append(
                        f"| {r.get('code')} | {r.get('name')} | "
                        f"{_fmt_num(r.get('final_score'), 1)} | "
                        f"{_fmt_num(r.get('dtw_score'), 1)} | "
                        f"{r.get('buy_level') or '-'} | "
                        f"{r.get('matched_template') or '-'} | "
                        f"{_fmt_num(r.get('price'))} |")
            else:
                md.append("| 代码 | 名称 | 收盘 | 涨幅 | 命中说明 |")
                md.append("|---|---|---|---|---|")
                for r in s["rows"][:max_per_strategy]:
                    md.append(
                        f"| {r.get('code')} | {r.get('name')} | "
                        f"{_fmt_num(r.get('price'))} | {_fmt_pct(r.get('pct'))} | "
                        f"{(r.get('reason') or '').replace('|', '/')[:40]} |")
            if s["count"] > max_per_strategy:
                md.append("")
                md.append(f"> 其余 {s['count'] - max_per_strategy} 只未逐条列出。")
            md.append("")

    if include_watch:
        md.append("## 三、观察池")
        md.append("")
        md.append(f"- 当日新增：**{len(watch_new)}** 只")
        for w in watch_new[:max_per_strategy]:
            md.append(f"  - {w.get('name')}({w.get('code')}) "
                      f"[{w.get('status') or '-'}] 来源：{w.get('trigger_indicator') or '-'}")
        if len(watch_new) > max_per_strategy:
            md.append(f"  - …… 其余 {len(watch_new) - max_per_strategy} 只")
        md.append("")

    if include_alert:
        md.append("## 四、提醒")
        md.append("")
        md.append(f"- 当日触发：**{len(alerts)}** 条")
        for a in alerts[:max_per_strategy]:
            md.append(f"  - {str(a.get('created_at') or '')[11:19]} {a.get('message')}")
        if len(alerts) > max_per_strategy:
            md.append(f"  - …… 其余 {len(alerts) - max_per_strategy} 条")
        md.append("")

    markdown = "\n".join(md)

    # ================= 飞书精简消息 =================
    lines = [f"命中合计 {total} 只（一层 {n1} / 二层 {n2}），策略 {n_strategies} 个"]
    if breadth:
        lines.append(f"市场：涨 {breadth.get('up', 0)} / 跌 {breadth.get('down', 0)}"
                     f" / 平均 {_fmt_pct(breadth.get('avg_pct'))}")
    for s in sections:
        tag = "一层" if s["layer"] == 1 else "二层"
        lines.append("")
        lines.append(f"【{tag}·{s['strategy']}】{s['count']} 只")
        for r in s["rows"][:max_per_strategy]:
            if s["layer"] == 2:
                lines.append(f"  {r.get('code')} {r.get('name')} "
                             f"综合{_fmt_num(r.get('final_score'), 1)} "
                             f"骨架{_fmt_num(r.get('dtw_score'), 1)} "
                             f"{r.get('buy_level') or ''}")
            else:
                lines.append(f"  {r.get('code')} {r.get('name')} "
                             f"{_fmt_num(r.get('price'))} {_fmt_pct(r.get('pct'))}")
        if s["count"] > max_per_strategy:
            lines.append(f"  …… 其余 {s['count'] - max_per_strategy} 只")
    if include_watch and watch_new:
        lines.append("")
        lines.append(f"观察池新增 {len(watch_new)} 只")
    if include_alert and alerts:
        lines.append(f"提醒触发 {len(alerts)} 条")
    lines.append("")
    lines.append(f"完整文档：reports/日报-{day}.md")

    return {
        "day": day,
        "title": f"📊 选股日报 {day}",
        "markdown": markdown,
        "feishu_lines": lines,
        "sections": sections,
        "stats": {
            "strategies": n_strategies,
            "total": total,
            "layer1": n1,
            "layer2": n2,
            "watch_new": len(watch_new),
            "alerts": len(alerts),
        },
    }


# ---------------------------------------------------------------------------
#  落盘
# ---------------------------------------------------------------------------
def report_path(day):
    return REPORT_DIR / f"日报-{day}.md"


def save_report(day, markdown, directory=None):
    """把 Markdown 写本地文件，返回路径字符串。"""
    d = directory or REPORT_DIR
    os.makedirs(d, exist_ok=True)
    path = os.path.join(str(d), f"日报-{day}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(markdown)
    log.info("选股日报已保存: %s", path)
    return path
