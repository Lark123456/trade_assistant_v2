# -*- coding: utf-8 -*-
"""AI 上下文装配（v0.11.1）。

【这个文件负责什么】
    把「AI 需要理解的事实」从本地数据库里现取出来，拼成一段纯文本，作为
    chat/completions 请求里的 user 消息。它不调用网络、不写库，只做只读查询
    和字符串拼接。

【它在整个流程里的位置】
    选股模块/自动日报
        └─ daily_report.build_daily_report()      汇总 scan_history → rep
        └─ ai_summary.summarize(rep, cfg, db=db)  组装消息并请求模型
              └─ ai_summary.build_brief(...)      （薄封装）
                    └─ 本文件 build_context()      ← 真正拼上下文的地方
    数据中心「AI 上下文预览」
        └─ 直接调用 build_context()，让用户看到即将发给模型的文本

【上下文的三块内容】
    1. 策略卡   —— 取 strategies.description / formula / 版本号，
                  用户在策略管理里改名或改公式后，下次生成自动同步；
    2. 个股档案 —— 取 board + stock_board，给每只票补「行业｜概念Top5」；
    3. 命中清单 —— 分层列出，一层额外给「行业分布 Top5」。

【输出格式（供参考，改格式前请先确认下游没有依赖）】
    日期：2026-11-24
    命中合计：10 只（一层 7 / 二层 3），策略数 4

    【策略卡】（来自本地策略库原文，按此理解策略）
    [S0001] B1海选｜一层粗筛｜盘前盘后｜v2（2026-09-02 修订）
    说明书：……
    公式：……

    【二层精筛】
    B1十张图：2 只
      300750 宁德时代 综合86.4 骨架0.9 SB1核心买点 模具=…｜电池｜麒麟电池、…

    【一层粗筛】
    B1海选：5 只
      行业分布：银行Ⅱ 2、乘用车 1（按 5 只统计）
      300750 宁德时代 收189.32 +3.21%｜电池｜麒麟电池、…

    【跨策略共振】（同一只票被多个策略同时命中，优先级更高）
      300750 宁德时代：B1十张图、B1海选

【设计原则】
    - 纯函数 + 只读查询，不做任何网络请求，方便单测和离线调试；
    - 不传 db 时退化为纯命中清单（旧格式），保证老调用方和测试不受影响；
    - 总长度受 max_chars 约束，超长时在**尾部**截断并标注。
"""
from __future__ import annotations

import datetime
import logging

log = logging.getLogger("ai_context")

# ---------------------------------------------------------------------------
#  可调参数（改这里会影响 AI 看到的信息量，进而影响 token 消耗）
# ---------------------------------------------------------------------------

# 单个策略说明书截取多少字。写太长会挤占命中清单的空间（截断在尾部）。
MAX_DESC_CHARS = 500

# 单个策略公式截取多少字。公式只是给模型"读"的语义参考，不会被本地执行。
MAX_FORMULA_CHARS = 500

# 概念标签的"泛化"阈值：板块成分数超过该值就视为泛标签并剔除。
# 例：融资融券 3895 只、深股通 1897 只，对区分个股没有信息量。
GENERIC_BOARD_MIN = 1000

# 每只股票最多保留几个概念。按成分数升序取，越靠前越"小众"、越有区分度。
MAX_CONCEPT = 5

# 行业分布最多统计多少只股票。防止某天一层命中 2 万只时，SQL 的 IN 列表过长。
MAX_DIST_CODES = 3000

# 策略扫描模式 → 中文标签，仅用于策略卡标题行，方便模型理解策略的使用场景。
SCAN_MODE_LABELS = {
    "batch": "盘前盘后",
    "intraday": "盘中重点",
    "manual": "手动",
}


# ---------------------------------------------------------------------------
#  小工具
# ---------------------------------------------------------------------------
def _num(v, nd=2):
    """数字格式化；None / 非数字一律返回 "-"，避免拼出 "None" 干扰模型。"""
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "-"


def _pct(v):
    """涨跌幅格式化：始终带正负号（+1.23% / -0.45%），方便模型判断红绿方向。"""
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "-"


def _chunks(seq, size=800):
    """把列表切成固定大小的小块。

    用途：SQLite 的 `IN (?,?,...)` 有占位符数量上限（不同版本 999~32766），
    一次性传入几千个股票代码会报 "too many SQL variables"，所以分批查询。
    """
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ---------------------------------------------------------------------------
#  个股档案：把一只票的板块标签查出来，拼成「行业｜概念1、概念2…」
# ---------------------------------------------------------------------------
def load_profiles(db, codes, max_concept=MAX_CONCEPT,
                  generic_min=GENERIC_BOARD_MIN):
    """批量查询个股板块档案（一次查多只，避免逐只查库）。

    参数：
        db            Database 实例（只读）
        codes         股票代码列表，重复项会被自动去重
        max_concept   每只票保留的概念数
        generic_min   成分数超过该值的概念视为泛标签，剔除

    返回：
        {code: {"industry": str, "region": str, "concepts": [str, ...]}}

    实现要点：
        - 按 _chunks 分批执行 IN 查询，规避 SQLite 占位符上限；
        - 一只票在东财体系里恰好有 1 个二级行业，所以 industry 取第一个；
        - 概念按 stock_count 升序排序后截断，即"越小众越靠前"；
        - 若概念全是泛标签（极少见），退化为成分数最小的前 3 个，避免档案为空。
    """
    # dict.fromkeys 去重且保持顺序；顺手过滤空代码
    codes = [c for c in dict.fromkeys(codes or []) if c]
    if not codes:
        return {}

    rows = []
    with db._cursor() as cur:  # noqa: SLF001  复用 Database 的连接与锁
        for part in _chunks(codes):
            marks = ",".join("?" * len(part))
            rows.extend(cur.execute(
                "SELECT sb.code, b.board_type, b.name, b.stock_count "
                "FROM stock_board sb JOIN board b ON b.id=sb.board_id "
                f"WHERE b.is_active=1 AND sb.code IN ({marks})",
                part).fetchall())

    # 把扁平的 (code, board_type, name, stock_count) 行按股票归拢
    buckets = {}
    for r in rows:
        d = buckets.setdefault(r["code"], {"industry": "", "region": "",
                                           "concepts": []})
        if r["board_type"] == "industry" and not d["industry"]:
            d["industry"] = r["name"]          # 只取第一个行业（理论上唯一）
        elif r["board_type"] == "region" and not d["region"]:
            d["region"] = r["name"]            # 地域标签，目前不参与拼装
        elif r["board_type"] == "concept":
            d["concepts"].append((int(r["stock_count"] or 0), r["name"]))

    # 概念的排序 / 过滤 / 截断
    out = {}
    for code, d in buckets.items():
        concepts = [n for c, n in sorted(d["concepts"]) if c <= generic_min]
        if not concepts:   # 全是泛标签时兜底，别让档案空着
            concepts = [n for _c, n in sorted(d["concepts"])[:3]]
        out[code] = {"industry": d["industry"], "region": d["region"],
                     "concepts": concepts[:max_concept]}
    return out


def format_profile(profile):
    """把一条档案压成单行文本：电池｜麒麟电池、宁组合、锂矿概念。

    只输出行业和概念；地域标签暂不放进上下文（避免"广东板块"这类噪音）。
    档案为空时返回空串，调用方据此决定是否追加分隔符。
    """
    if not profile:
        return ""
    parts = []
    if profile.get("industry"):
        parts.append(profile["industry"])
    if profile.get("concepts"):
        parts.append("、".join(profile["concepts"]))
    return "｜".join(parts)


def industry_distribution(db, codes, top=5):
    """统计某一批股票的行业分布，返回 [(行业名, 只数), ...]，按只数降序。

    用途：一层粗筛命中几百只时，没法定投看每只，用行业分布告诉模型
    "今天的信号集中在哪些行业"，比逐只罗列更有信息量。

    实现要点：
        - 只统计 board_type='industry'（东财三级行业中的二级）；
        - codes 截断到 MAX_DIST_CODES，控制 SQL 规模；
        - 分批查询后在 Python 里累加，得到跨批次的总数。
    """
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
#  策略卡：把策略库原文原样交给模型，替代过去手写死在提示词里的说明
# ---------------------------------------------------------------------------
def build_strategy_cards(db, sections):
    """按本次日报里出现的策略，生成若干张策略卡。

    参数：
        sections  build_daily_report 的分组结果，每项含 strategy / layer / rows

    返回：
        多行文本；没有任何策略或读库失败时返回空串。

    每张卡的格式：
        [S0001] B1海选｜一层粗筛｜盘前盘后｜v2（2026-09-02 修订）
        说明书：<strategies.description 前 500 字>
        公式：<strategies.formula 前 500 字>

    设计说明：
        - 策略详情不在这里重写，直接引用数据库原文，避免两处口径不一致；
        - 带上版本号与修订日期，让模型知道"这版公式和旧版可能不同"；
        - 若日报里的策略名在策略库中找不到（被删除或改名），仍保留一张占位卡，
          明确告诉模型"这个策略已不可考"，防止它自由发挥。
    """
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
        seen.add(name)                       # 同一策略只生成一张卡
        s = strategies.get(name)
        layer = int(sec.get("layer") or 1)

        # 策略库里找不到：给占位卡，明确告知缺失，避免模型编造逻辑
        if not s:
            cards.append(f"[{name}]｜{layer} 层｜（策略库中未找到，可能已删除或改名）")
            continue

        # 版本信息：v1 视为初始版本，不显示日期；v2 起显示修订日期
        version = 1
        version_day = ""
        try:
            version = db.get_strategy_latest_version(s["id"]) or 1
            if version > 1:
                v = db.get_strategy_version(s["id"], version) or {}
                version_day = str(v.get("created_at") or "")[:10]
        except Exception as e:  # noqa: BLE001
            log.debug("读取策略版本失败 %s: %s", name, e)

        # 标题行：编号｜名称｜层级｜扫描模式｜版本
        head = [f"[{s.get('code') or '-'}] {name}",
                {1: "一层粗筛", 2: "二层精筛"}.get(layer, f"{layer} 层"),
                SCAN_MODE_LABELS.get(s.get("scan_mode"), s.get("scan_mode") or "-"),
                f"v{version}" + (f"（{version_day} 修订）" if version_day else "")]
        card = ["｜".join(head)]

        # 正文：说明书 + 公式，各自限长，防止单张卡吃掉整个上下文预算
        desc = (s.get("description") or "").strip()
        if desc:
            card.append("说明书：" + desc[:MAX_DESC_CHARS])
        formula = (s.get("formula") or "").strip()
        if formula:
            card.append("公式：" + formula[:MAX_FORMULA_CHARS])
        cards.append("\n".join(card))

    if not cards:
        return ""
    # 段落标题 + 卡片之间用空行分隔，便于模型区分边界
    return "【策略卡】（来自本地策略库原文，按此理解策略）\n" + "\n\n".join(cards)


# ---------------------------------------------------------------------------
#  命中清单：单只股票的一行文本
# ---------------------------------------------------------------------------
def _hit_line(row, layer, profile_text=""):
    """把一条命中记录格式化成一行。

    两种写法：
        二层且有综合分：  300750 宁德时代 综合86.4 骨架0.9 SB1核心买点 模具=…
        其余（一层/无分）：300750 宁德时代 收189.32 +3.21%

    之所以要区分：二层的价值在"综合分/骨架分/买点定级/模具"，价格不是重点；
    而个别二层策略执行失败时仍会入库但没有分数，此时退回价格写法，
    避免输出 "综合- 骨架-" 这种会让模型误解的内容。

    profile_text 非空时追加 "｜行业｜概念…"。
    """
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
    """找出「跨策略共振」名单：同一只票被 2 个及以上策略命中。

    返回 [(code, {"name": 名称, "strats": [策略名, ...]}), ...]，
    按命中策略数降序，最多 limit 条。

    为什么单独列：多策略同时命中通常比单策略命中更可靠，
    系统提示里也要求模型优先关注这批票。
    """
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
#  总装：把所有部分拼成最终发给模型的 user 消息
# ---------------------------------------------------------------------------
def build_context(db, rep, max_per_strategy=8, breadth=None, max_chars=5000):
    """把 build_daily_report 的结果装配成上下文文本。

    参数：
        db                Database 实例；传 None 时只输出命中清单（不含策略卡/板块）
        rep               build_daily_report 的返回值
        max_per_strategy  每个策略最多列几只明细（超出只影响明细，行业分布仍按全量统计）
        breadth           市场宽度 dict（可选，来自 db.get_market_breadth）
        max_chars         总字数上限，超出在尾部截断

    返回：
        纯文本，直接作为 messages[1]["content"] 的主体。

    执行顺序（与输出顺序一致）：
        预取策略卡/个股档案 → 头部统计 → 策略卡 → 二层 → 一层 → 跨策略共振 → 截断
    """
    rep = rep or {}
    st = rep.get("stats") or {}
    sections = rep.get("sections") or []
    day = rep.get("day") or datetime.date.today().isoformat()

    # ---- 预取：策略卡 / 个股档案 ----
    # 只取"将要展示的"那部分股票的档案，避免为几百只没露面的票白查库。
    cards = ""
    profiles = {}
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
        except Exception as e:  # noqa: BLE001
            log.debug("生成个股档案失败: %s", e)

    # ---- 头部：日期 / 市场宽度 / 命中合计 / 观察池 ----
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

    # ---- 策略卡：放在命中清单之前，模型先建立"策略是什么"的认知 ----
    if cards:
        lines.append("")
        lines.append(cards)

    # ---- 分层清单：二层在前（更严格、更有价值），一层在后 ----
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

            # 一层命中多，补一行行业分布；少于 5 只时样本太小，不输出
            if layer == 1 and db is not None and count >= 5:
                codes = [r.get("code") for r in (s.get("rows") or [])
                         if r.get("code")]
                try:
                    dist = industry_distribution(db, codes)
                except Exception as e:  # noqa: BLE001
                    log.debug("统计行业分布失败: %s", e)
                    dist = []
                if dist:
                    top = "、".join(f"{n} {c}" for n, c in dist)
                    used = min(len(codes), MAX_DIST_CODES)
                    lines.append(f"  行业分布：{top}（按 {used} 只统计）")

            # 明细行：最多 max_per_strategy 只，每行追加板块档案
            for row in (s.get("rows") or [])[:max_per_strategy]:
                ptxt = format_profile(profiles.get(row.get("code")))
                lines.append(_hit_line(row, layer, ptxt))

    # ---- 跨策略共振：放在最后作为"优先级提示" ----
    ov = _overlaps(sections)
    if ov:
        lines.append("")
        lines.append("【跨策略共振】（同一只票被多个策略同时命中，优先级更高）")
        for code, v in ov:
            lines.append(f"  {code} {v['name']}：{'、'.join(v['strats'])}")

    # ---- 截断：注意是尾部截断，所以策略卡在前、明细在后，
    #      超长时优先丢掉的会是后面的明细（可通过调大 max_chars 或缩短说明书缓解）
    text = "\n".join(lines)
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + "\n……（数据过长已截断）"
    return text
