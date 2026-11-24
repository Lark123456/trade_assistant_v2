# -*- coding: utf-8 -*-
"""AI 上下文装配（v0.11.1）。

把「AI 需要理解的事实」从数据库里现取，拼成一段紧凑文本，替代过去手写在
提示词里的策略说明。三块内容：

    1. 策略卡   —— 直接取 strategies.description / formula / 版本号，改名改公式自动同步
    2. 命中清单 —— 分层列出，一层额外给「行业分布 Top5」
    3. 个股档案 —— 行业 + 概念 Top5（按板块成分数升序，过滤泛标签）

设计原则：
    - 纯函数 + 只读查询，不做任何网络请求；
    - 不传 db 时退化为旧格式（便于单测和离线调用）；
    - 总长度受 max_chars 约束，超长截断并标注。
"""
from __future__ import annotations

import datetime
import logging

log = logging.getLogger("ai_context")

MAX_DESC_CHARS = 500       # 单个策略说明书上限
MAX_FORMULA_CHARS = 500    # 单个策略公式上限
GENERIC_BOARD_MIN = 1000   # 成分数超过该值的概念视为泛标签（融资融券/深股通等）
MAX_CONCEPT = 5            # 每只票保留的概念数
MAX_DIST_CODES = 3000      # 行业分布最多统计的股票数（防止一次扫 2 万只时爆查询）

SCAN_MODE_LABELS = {
    "batch": "盘前盘后",
    "intraday": "盘中重点",
    "manual": "手动",
}


def _num(v, nd=2):
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "-"


def _pct(v):
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "-"


def _chunks(seq, size=800):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ---------------------------------------------------------------------------
#  个股档案
# ---------------------------------------------------------------------------
def load_profiles(db, codes, max_concept=MAX_CONCEPT,
                  generic_min=GENERIC_BOARD_MIN):
    """批量取个股板块档案。

    返回 {code: {"industry": str, "region": str, "concepts": [str, ...]}}
    """
    codes = [c for c in dict.fromkeys(codes or []) if c]
    if not codes:
        return {}

    rows = []
    with db._cursor() as cur:  # noqa: SLF001  复用连接与锁
        for part in _chunks(codes):
            marks = ",".join("?" * len(part))
            rows.extend(cur.execute(
                "SELECT sb.code, b.board_type, b.name, b.stock_count "
                "FROM stock_board sb JOIN board b ON b.id=sb.board_id "
                f"WHERE b.is_active=1 AND sb.code IN ({marks})",
                part).fetchall())

    buckets = {}
    for r in rows:
        d = buckets.setdefault(r["code"], {"industry": "", "region": "",
                                           "concepts": []})
        if r["board_type"] == "industry" and not d["industry"]:
            d["industry"] = r["name"]
        elif r["board_type"] == "region" and not d["region"]:
            d["region"] = r["name"]
        elif r["board_type"] == "concept":
            d["concepts"].append((int(r["stock_count"] or 0), r["name"]))

    out = {}
    for code, d in buckets.items():
        concepts = [n for c, n in sorted(d["concepts"]) if c <= generic_min]
        if not concepts:   # 全是泛标签时兜底，别让档案空着
            concepts = [n for _c, n in sorted(d["concepts"])[:3]]
        out[code] = {"industry": d["industry"], "region": d["region"],
                     "concepts": concepts[:max_concept]}
    return out


def format_profile(profile):
    """把档案压成一行：电池｜麒麟电池、宁组合、锂矿概念。"""
    if not profile:
        return ""
    parts = []
    if profile.get("industry"):
        parts.append(profile["industry"])
    if profile.get("concepts"):
        parts.append("、".join(profile["concepts"]))
    return "｜".join(parts)


def industry_distribution(db, codes, top=5):
    """一层命中的行业分布 Top N，返回 [(行业, 只数), ...]。"""
    codes = [c for c in dict.fromkeys(codes or []) if c][:MAX_DIST_CODES]
    if not codes:
        return []
    from collections import Counter
    cnt = Counter()
    with db._cursor() as cur:  # noqa: SLF001
        for part in _chunks(codes):
            marks = ",".join("?" * len(part))
            for r in cur.execute(
                    "SELECT b.name AS nm, COUNT(DISTINCT sb.code) AS n "
                    "FROM stock_board sb JOIN board b ON b.id=sb.board_id "
                    "WHERE b.is_active=1 AND b.board_type='industry' "
                    f"AND sb.code IN ({marks}) GROUP BY b.name", part):
                cnt[r["nm"]] += int(r["n"])
    return cnt.most_common(top)


# ---------------------------------------------------------------------------
#  策略卡
# ---------------------------------------------------------------------------
def build_strategy_cards(db, sections):
    """按本次出现的策略生成策略卡，直接引用策略库原文。"""
    if db is None or not sections:
        return ""
    try:
        strategies = {s.get("name"): s for s in db.list_strategies()}
    except Exception as e:  # noqa: BLE001
        log.debug("读取策略库失败，跳过策略卡: %s", e)
        return ""

    seen, cards = set(), []
    for sec in sections:
        name = sec.get("strategy") or ""
        if not name or name in seen:
            continue
        seen.add(name)
        s = strategies.get(name)
        layer = int(sec.get("layer") or 1)
        if not s:
            cards.append(f"[{name}]｜{layer} 层｜（策略库中未找到，可能已删除或改名）")
            continue

        version = 1
        version_day = ""
        try:
            version = db.get_strategy_latest_version(s["id"]) or 1
            if version > 1:
                v = db.get_strategy_version(s["id"], version) or {}
                version_day = str(v.get("created_at") or "")[:10]
        except Exception as e:  # noqa: BLE001
            log.debug("读取策略版本失败 %s: %s", name, e)

        head = [f"[{s.get('code') or '-'}] {name}",
                {1: "一层粗筛", 2: "二层精筛"}.get(layer, f"{layer} 层"),
                SCAN_MODE_LABELS.get(s.get("scan_mode"), s.get("scan_mode") or "-"),
                f"v{version}" + (f"（{version_day} 修订）" if version_day else "")]
        card = ["｜".join(head)]

        desc = (s.get("description") or "").strip()
        if desc:
            card.append("说明书：" + desc[:MAX_DESC_CHARS])
        formula = (s.get("formula") or "").strip()
        if formula:
            card.append("公式：" + formula[:MAX_FORMULA_CHARS])
        cards.append("\n".join(card))

    if not cards:
        return ""
    return "【策略卡】（来自本地策略库原文，按此理解策略）\n" + "\n\n".join(cards)


# ---------------------------------------------------------------------------
#  命中清单
# ---------------------------------------------------------------------------
def _hit_line(row, layer, profile_text=""):
    code = row.get("code") or ""
    name = row.get("name") or ""
    if layer == 2 and row.get("final_score") is not None:
        line = (f"  {code} {name} 综合{_num(row.get('final_score'), 1)} "
                f"骨架{_num(row.get('dtw_score'), 1)} "
                f"{row.get('buy_level') or '-'} "
                f"模具={row.get('matched_template') or '-'}")
    else:
        line = (f"  {code} {name} 收{_num(row.get('price'), 2)} "
                f"{_pct(row.get('pct'))}")
    if profile_text:
        line += f"｜{profile_text}"
    return line


def _overlaps(sections, limit=10):
    """跨策略共振：同一只票被 ≥2 个策略命中（含一/二层）。"""
    seen = {}
    for s in sections:
        strat = s.get("strategy") or "?"
        for row in (s.get("rows") or []):
            code = row.get("code")
            if not code:
                continue
            d = seen.setdefault(code, {"name": row.get("name") or "",
                                       "strats": []})
            if strat not in d["strats"]:
                d["strats"].append(strat)
    items = [(c, v) for c, v in seen.items() if len(v["strats"]) >= 2]
    items.sort(key=lambda kv: -len(kv[1]["strats"]))
    return items[:limit]


# ---------------------------------------------------------------------------
#  总装
# ---------------------------------------------------------------------------
def build_context(db, rep, max_per_strategy=8, breadth=None, max_chars=5000):
    """把日报结果装配成喂给模型的上下文文本。"""
    rep = rep or {}
    st = rep.get("stats") or {}
    sections = rep.get("sections") or []
    day = rep.get("day") or datetime.date.today().isoformat()

    # ---- 预取：策略卡 / 个股档案 / 行业分布 ----
    cards = ""
    profiles, dist = {}, []
    if db is not None:
        try:
            cards = build_strategy_cards(db, sections)
        except Exception as e:  # noqa: BLE001
            log.debug("生成策略卡失败: %s", e)
        try:
            shown = [r.get("code") for s in sections
                     for r in (s.get("rows") or [])[:max_per_strategy]
                     if r.get("code")]
            profiles = load_profiles(db, shown)
            layer1_codes = [r.get("code") for s in sections if s.get("layer") == 1
                            for r in (s.get("rows") or []) if r.get("code")]
            dist = industry_distribution(db, layer1_codes)
        except Exception as e:  # noqa: BLE001
            log.debug("生成个股档案失败: %s", e)

    # ---- 头部 ----
    lines = [f"日期：{day}"]
    if breadth:
        lines.append(
            f"市场宽度：上涨 {breadth.get('up', 0)} 家 / 下跌 {breadth.get('down', 0)} 家"
            f" / 平均 {_pct(breadth.get('avg_pct'))}（覆盖 {breadth.get('total', 0)} 只）")
    lines.append(
        f"命中合计：{st.get('total', 0)} 只（一层 {st.get('layer1', 0)} / "
        f"二层 {st.get('layer2', 0)}），策略数 {st.get('strategies', 0)}")
    if st.get("watch_new"):
        lines.append(f"当日自动进观察池：{st['watch_new']} 只")

    if cards:
        lines.append("")
        lines.append(cards)

    # ---- 分层清单 ----
    for layer, label in ((2, "二层精筛"), (1, "一层粗筛")):
        part = [s for s in sections if s.get("layer") == layer]
        if not part:
            continue
        lines.append("")
        lines.append(f"【{label}】")
        for s in part:
            count = s.get("count", 0)
            lines.append(f"{s.get('strategy')}：{count} 只"
                         + (f"（列前 {min(max_per_strategy, count)} 只）"
                            if count > max_per_strategy else ""))
            if layer == 1 and dist:
                top = "、".join(f"{n} {c}" for n, c in dist)
                lines.append(f"  行业分布：{top}（按 {count} 只统计）")
            for row in (s.get("rows") or [])[:max_per_strategy]:
                ptxt = format_profile(profiles.get(row.get("code")))
                lines.append(_hit_line(row, layer, ptxt))

    # ---- 跨策略共振 ----
    ov = _overlaps(sections)
    if ov:
        lines.append("")
        lines.append("【跨策略共振】（同一只票被多个策略同时命中，优先级更高）")
        for code, v in ov:
            lines.append(f"  {code} {v['name']}：{'、'.join(v['strats'])}")

    text = "\n".join(lines)
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + "\n……（数据过长已截断）"
    return text
