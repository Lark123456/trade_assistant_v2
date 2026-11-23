# -*- coding: utf-8 -*-
"""AI 日报点评（v0.10.4）。

把当日扫描汇总交给任意 **OpenAI 兼容** 大模型，让它基于用户的策略体系写一段中文点评，
追加到本地 Markdown 日报并随飞书消息一起推送。

支持任意兼容端点（改 Base URL 即可）：

    火山方舟  https://ark.cn-beijing.volces.com/api/v3   模型 doubao-seed-1-6-250615 或 ep-xxxx
    DeepSeek  https://api.deepseek.com/v1                模型 deepseek-chat
    Moonshot  https://api.moonshot.cn/v1                 模型 moonshot-v1-8k
    本地 Ollama http://localhost:11434/v1                模型 qwen2.5:14b

设计原则：
    - **AI 失败不影响日报**：拿不到或报错时退回纯数据版，照常推送；
    - **只喂汇总、不喂全量**：每策略最多 N 只，整段控制在 4000 字以内，省 token；
    - **强约束防编造**：系统提示明确要求只用给定数字，禁止给买卖点位。
"""
from __future__ import annotations

import datetime
import json
import logging
import time
from dataclasses import dataclass

import requests

log = logging.getLogger("ai_summary")

DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = "doubao-seed-1-6-250615"
DEFAULT_MAX_TOKENS = 900
DEFAULT_TIMEOUT = 90
MAX_BRIEF_CHARS = 4000

# settings 键名
K_ENABLED = "ai_enabled"
K_BASE_URL = "ai_base_url"
K_API_KEY = "ai_api_key"
K_MODEL = "ai_model"
K_MAX_TOKENS = "ai_max_tokens"
K_TIMEOUT = "ai_timeout"
K_EXTRA = "ai_prompt_extra"


# ---------------------------------------------------------------------------
#  配置
# ---------------------------------------------------------------------------
def _as_bool(v, default=False):
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on", "是")


@dataclass
class AIConfig:
    """AI 点评配置。"""

    enabled: bool = False
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout: int = DEFAULT_TIMEOUT
    extra_prompt: str = ""

    @classmethod
    def load(cls, db):
        def g(key, default=""):
            return db.get_setting(key, default)

        def gi(key, default):
            try:
                return int(g(key, default) or default)
            except (TypeError, ValueError):
                return default

        return cls(
            enabled=_as_bool(g(K_ENABLED), False),
            base_url=(g(K_BASE_URL, DEFAULT_BASE_URL) or DEFAULT_BASE_URL).strip(),
            api_key=(g(K_API_KEY, "") or "").strip(),
            model=(g(K_MODEL, DEFAULT_MODEL) or DEFAULT_MODEL).strip(),
            max_tokens=max(100, gi(K_MAX_TOKENS, DEFAULT_MAX_TOKENS)),
            timeout=max(10, gi(K_TIMEOUT, DEFAULT_TIMEOUT)),
            extra_prompt=(g(K_EXTRA, "") or "").strip(),
        )

    def save(self, db):
        db.set_setting(K_ENABLED, "1" if self.enabled else "0")
        db.set_setting(K_BASE_URL, self.base_url.strip())
        db.set_setting(K_API_KEY, self.api_key.strip())
        db.set_setting(K_MODEL, self.model.strip())
        db.set_setting(K_MAX_TOKENS, str(self.max_tokens))
        db.set_setting(K_TIMEOUT, str(self.timeout))
        db.set_setting(K_EXTRA, self.extra_prompt.strip())

    def is_configured(self):
        return bool(self.api_key.strip() and self.model.strip() and self.base_url.strip())

    def endpoint(self):
        """把 Base URL 规整成 /chat/completions 完整地址。"""
        base = (self.base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"


# ---------------------------------------------------------------------------
#  提示词
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是一名 A 股短线交易助手，负责为用户写「每日选股日报」的点评部分。

【用户的策略体系，只能用这套逻辑解读，不要臆造别的策略】
1) B1海选（一层粗筛）：白线=EMA(EMA(收盘,10),10) 位于 黄线=(MA14+MA28+MA57+MA114)/4 之上，
   且 KDJ(9,3,3) 的 J 值 < 13。本质是「主力不在成本区之下 + 短期超卖」的海选网，
   条件刻意放宽，所以普跌日命中数会膨胀到数百只，命中数本身不代表都是好票。
2) B2普通 / B2灾后重建：B1 次日的确认信号（放量起涨），个股层面才谈得上买点确认。
3) 砖型图翻红：底部结构转强；单针下30：长下影超卖形态。两者是形态类辅助策略。
4) B1十张图（二层精筛）：对一层候选做 DNA 骨架比对（9 个「经典B1缩量回踩」模具）
   再加微观规则加减分，综合分 ≥80 才通过，是最严的一层。
   买点定级：SB1核心买点 强于 B1底仓买点。

【写作要求】
- 中文，纯文本，禁止 Markdown 语法（不要 #、*、-、表格、代码块），每行不超过 40 个汉字。
- 总共 6~10 行，控制在 500 字以内。
- 数据里若出现【跨策略共振】名单，说明这些票被多个策略同时命中，可信度高于单策略命中。
- 结构：
  第 1 行：一句话总结今天的信号结构（哪个策略放量/萎缩、是否属于普跌放大）；
  中间若干行：3~5 只重点标的，格式「代码 名称：一句话理由」，优先级为
      二层通过 > 出现在【跨策略共振】名单里的 > 单一策略大量命中里的代表；
  最后 1 行：一句风险提示（例如海选口径偏宽、需次日确认）。
- 只使用用户给出的数字和标的，**不得编造**任何代码、价格、指标、行业消息。
  数据不足时少写，不要凑。
- 不给具体买卖点位、不承诺收益、不推荐仓位。"""


# ---------------------------------------------------------------------------
#  数据摘要（喂给模型的上下文）
# ---------------------------------------------------------------------------
def _brief_line_for_hit(row, layer):
    code = row.get("code") or ""
    name = row.get("name") or ""
    # 二层但没打分（个别策略执行失败时仍会入库）→ 退回普通写法，避免出现「综合- 骨架-」
    if layer == 2 and row.get("final_score") is not None:
        return (f"  {code} {name} 综合{_num(row.get('final_score'), 1)} "
                f"骨架{_num(row.get('dtw_score'), 1)} "
                f"{row.get('buy_level') or '-'} "
                f"模具={row.get('matched_template') or '-'}")
    return (f"  {code} {name} 收{_num(row.get('price'), 2)} "
            f"{_pct(row.get('pct'))}")


def _overlaps(rep, limit=10):
    """跨策略共振：同一只票被 ≥2 个策略命中（含一/二层）。"""
    seen = {}
    for s in (rep.get("sections") or []):
        strat = s.get("strategy") or "?"
        for row in (s.get("rows") or []):
            code = row.get("code")
            if not code:
                continue
            d = seen.setdefault(code, {"name": row.get("name") or "", "strats": []})
            if strat not in d["strats"]:
                d["strats"].append(strat)
    items = [(c, v) for c, v in seen.items() if len(v["strats"]) >= 2]
    items.sort(key=lambda kv: -len(kv[1]["strats"]))
    return items[:limit]


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


def build_brief(rep, max_per_strategy=8, breadth=None):
    """把 build_daily_report 的结果压成一段紧凑文本（控制字数）。"""
    rep = rep or {}
    st = rep.get("stats") or {}
    day = rep.get("day") or datetime.date.today().isoformat()
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

    for layer, label in ((2, "二层精筛"), (1, "一层粗筛")):
        part = [s for s in (rep.get("sections") or []) if s.get("layer") == layer]
        if not part:
            continue
        lines.append("")
        lines.append(f"【{label}】")
        for s in part:
            lines.append(f"{s.get('strategy')}：{s.get('count')} 只"
                         + (f"（列前 {min(max_per_strategy, s.get('count', 0))} 只）"
                            if s.get("count", 0) > max_per_strategy else ""))
            for row in (s.get("rows") or [])[:max_per_strategy]:
                lines.append(_brief_line_for_hit(row, layer))

    # 跨策略共振：比单个策略的命中更有信息量
    ov = _overlaps(rep)
    if ov:
        lines.append("")
        lines.append("【跨策略共振】（同一只票被多个策略同时命中，优先级更高）")
        for code, v in ov:
            lines.append(f"  {code} {v['name']}：{'、'.join(v['strats'])}")

    text = "\n".join(lines)
    if len(text) > MAX_BRIEF_CHARS:
        text = text[:MAX_BRIEF_CHARS] + "\n……（数据过长已截断）"
    return text


# ---------------------------------------------------------------------------
#  调用
# ---------------------------------------------------------------------------
def _post_completion(cfg, messages):
    """POST /chat/completions，返回 (ok, content 或 错误说明, 附加信息)。"""
    if not cfg.is_configured():
        return False, "AI 未配置（需要 Base URL / API Key / 模型名）", {}

    url = cfg.endpoint()
    payload = {
        "model": cfg.model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": cfg.max_tokens,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    t0 = time.time()
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=cfg.timeout)
    except Exception as e:  # noqa: BLE001
        log.warning("AI 请求异常: %s", e)
        return False, f"网络异常: {e}", {}

    elapsed = time.time() - t0
    body = (r.text or "")[:400]
    if r.status_code == 401:
        return False, "认证失败（401）：API Key 不对或已失效", {}
    if r.status_code == 403:
        return False, "无权限（403）：检查该 Key 是否开通了此模型", {}
    if r.status_code == 404:
        return False, f"接口不存在（404）：检查 Base URL 与模型名。{body}", {}
    if r.status_code == 429:
        return False, "请求过于频繁或额度不足（429）", {}
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {body}", {}

    try:
        j = r.json()
    except ValueError:
        return False, f"返回非 JSON: {body}", {}

    try:
        content = (j["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return False, f"返回结构异常: {body}", {}

    usage = j.get("usage") or {}
    info = {
        "elapsed": round(elapsed, 1),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
    }
    if not content:
        return False, "模型返回空内容", info
    return True, content, info


def format_info(info):
    """把调用信息压成一句「12.3s，in 3200，out 480」。"""
    info = info or {}
    bits = []
    if info.get("elapsed") is not None:
        bits.append(f"{info['elapsed']}s")
    if info.get("prompt_tokens") is not None:
        bits.append(f"in {info['prompt_tokens']}")
    if info.get("completion_tokens") is not None:
        bits.append(f"out {info['completion_tokens']}")
    return "，".join(bits)


def _tidy(text):
    """去掉模型偶尔带出的 Markdown 记号，保证飞书纯文本观感一致。"""
    out = []
    for raw in (text or "").splitlines():
        ln = raw.strip()
        ln = ln.lstrip("#").strip()
        ln = ln.replace("**", "").replace("__", "").strip()
        if ln.startswith(("- ", "* ", "• ")):
            ln = ln[2:].strip()
        if ln.startswith("|") and ln.endswith("|"):
            ln = ln.strip("|").replace("|", " ").strip()
        if set(ln) <= set("-—| "):     # 表格分隔行
            continue
        if ln:
            out.append(ln)
    return "\n".join(out)


def summarize(rep, cfg, max_per_strategy=8, breadth=None):
    """生成 AI 点评。返回 (ok, 文本或错误说明, 附加信息)。"""
    brief = build_brief(rep, max_per_strategy=max_per_strategy, breadth=breadth)
    system = SYSTEM_PROMPT
    if cfg.extra_prompt:
        system += "\n\n【用户的额外要求】\n" + cfg.extra_prompt
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "以下是今日扫描数据，请按要求写点评：\n\n" + brief},
    ]
    ok, content, info = _post_completion(cfg, messages)
    if not ok:
        return False, content, info
    info["brief_chars"] = len(brief)
    return True, _tidy(content), info


def test_ai(cfg):
    """测试连通性：发一个极小的请求。返回 (ok, 说明)。"""
    if not cfg.is_configured():
        return False, "AI 未配置（需要 Base URL / API Key / 模型名）"
    messages = [{"role": "user", "content": "回复两个字：可用"}]
    ok, content, info = _post_completion(cfg, messages)
    if ok:
        return True, f"AI 可用：{content.strip()[:40]}（{format_info(info)}）"
    return False, content
