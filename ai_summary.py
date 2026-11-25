# -*- coding: utf-8 -*-
"""AI 日报点评（v0.11.1）。

【这个文件负责什么】
    把当日扫描汇总交给任意 **OpenAI 兼容** 大模型，让它基于用户自己的策略体系
    写一段中文点评，追加到本地 Markdown 日报，并随飞书消息一起推送。

【它在整个流程里的位置】
    选股模块 / 自动日报
        └─ daily_report.build_daily_report()   汇总 scan_history → rep
        └─ 本文件 summarize(rep, cfg, db=db)
              ├─ build_brief() → ai_context.build_context()  拼出 user 消息
              ├─ _post_completion()                        发 HTTP 请求
              └─ _tidy()                                   清理 Markdown 记号
    数据中心「AI 上下文预览」
        └─ 直接调用 ai_context.build_context()，展示即将发送的文本

【支持的模型端点】（OpenAI 兼容即可，改 Base URL 即可切换）
    火山方舟  https://ark.cn-beijing.volces.com/api/v3   模型 doubao-seed-1-6-250615 或 ep-xxxx
    DeepSeek  https://api.deepseek.com/v1                模型 deepseek-chat
    Moonshot  https://api.moonshot.cn/v1                 模型 moonshot-v1-8k
    本地 Ollama http://localhost:11434/v1                模型 qwen2.5:14b

【设计原则】
    - **AI 失败不影响日报**：拿不到或报错时退回纯数据版，照常推送；
    - **只喂汇总、不喂全量**：每策略最多 N 只，整段受 MAX_BRIEF_CHARS 限制；
    - **策略说明不手写**：策略卡由 ai_context 从数据库现取，避免两处口径不一致；
    - **强约束防编造**：系统提示要求只用给定数据，禁止给买卖点位。
"""
from __future__ import annotations

import datetime
import json
import logging
import time
from dataclasses import dataclass

import requests

log = logging.getLogger("ai_summary")

# ---------------------------------------------------------------------------
#  默认参数与 settings 键名
# ---------------------------------------------------------------------------
# 不填设置时使用的默认端点与模型（火山方舟）
DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = "doubao-seed-1-6-250615"

# 单次回复的最大 token 数；点评要求 500 字以内，900 留有余量
DEFAULT_MAX_TOKENS = 900

# 单次 HTTP 超时（秒）。模型排队时可能较慢，给足 90 秒
DEFAULT_TIMEOUT = 90

# 喂给模型的上下文总字数上限（含策略卡、命中清单、板块标签）
# 超长时由 ai_context.build_context 在尾部截断
MAX_BRIEF_CHARS = 8000

# settings 表里的配置键（Database.get_setting / set_setting 使用）
K_ENABLED = "ai_enabled"          # 是否启用 AI 点评
K_BASE_URL = "ai_base_url"        # OpenAI 兼容端点
K_API_KEY = "ai_api_key"          # API Key
K_MODEL = "ai_model"              # 模型名 / 推理接入点 ID
K_MAX_TOKENS = "ai_max_tokens"    # 回复 token 上限
K_TIMEOUT = "ai_timeout"          # HTTP 超时
K_EXTRA = "ai_prompt_extra"       # 用户附加要求，追加到系统提示末尾


# ---------------------------------------------------------------------------
#  配置读写
# ---------------------------------------------------------------------------
def _as_bool(v, default=False):
    """把 settings 里的字符串解析成布尔值。

    兼容 "1/true/yes/on/是" 等多种写法；空值或 None 时返回 default。
    """
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on", "是")


@dataclass
class AIConfig:
    """AI 点评配置。

    所有字段都来自 settings 表，由选股模块的「AI 配置」区域读写。
    单独做成 dataclass 是为了：调用时只传一个对象，且可离线构造（测试用）。
    """

    enabled: bool = False         # 是否启用（False 时日报不调用模型）
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout: int = DEFAULT_TIMEOUT
    extra_prompt: str = ""        # 附加要求，会拼在系统提示后面

    @classmethod
    def load(cls, db):
        """从 settings 表读取配置；缺失项用默认值兜底。"""

        def g(key, default=""):
            return db.get_setting(key, default)

        def gi(key, default):
            """整数读取：脏数据（空串/非数字）时退回默认值，不让日报崩掉。"""
            try:
                return int(g(key, default) or default)
            except (TypeError, ValueError):
                return default

        return cls(
            enabled=_as_bool(g(K_ENABLED), False),
            base_url=(g(K_BASE_URL, DEFAULT_BASE_URL) or DEFAULT_BASE_URL).strip(),
            api_key=(g(K_API_KEY, "") or "").strip(),
            model=(g(K_MODEL, DEFAULT_MODEL) or DEFAULT_MODEL).strip(),
            # 给下限，防止误填 0 导致模型无法输出
            max_tokens=max(100, gi(K_MAX_TOKENS, DEFAULT_MAX_TOKENS)),
            timeout=max(10, gi(K_TIMEOUT, DEFAULT_TIMEOUT)),
            extra_prompt=(g(K_EXTRA, "") or "").strip(),
        )

    def save(self, db):
        """把当前配置写回 settings 表（界面点保存时调用）。"""
        db.set_setting(K_ENABLED, "1" if self.enabled else "0")
        db.set_setting(K_BASE_URL, self.base_url.strip())
        db.set_setting(K_API_KEY, self.api_key.strip())
        db.set_setting(K_MODEL, self.model.strip())
        db.set_setting(K_MAX_TOKENS, str(self.max_tokens))
        db.set_setting(K_TIMEOUT, str(self.timeout))
        db.set_setting(K_EXTRA, self.extra_prompt.strip())

    def is_configured(self):
        """三个必填项（端点/Key/模型）都齐了才算可用。"""
        return bool(self.api_key.strip() and self.model.strip() and self.base_url.strip())

    def endpoint(self):
        """把 Base URL 规整成完整的 /chat/completions 地址。

        用户可能填 "https://api.deepseek.com/v1"，也可能直接填完整路径，
        两种写法都要能工作。
        """
        base = (self.base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return base + "/chat/completions"


# ---------------------------------------------------------------------------
#  系统提示（role=system）
#  说明：策略细节不写在这里，改由 user 消息里的【策略卡】提供，
#       这样用户在策略管理里改名/改公式后，提示词不用跟着改。
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是一名 A 股短线交易助手，负责为用户写「每日选股日报」的点评部分。

【数据说明书（重要）】
- 用户消息里的【策略卡】直接来自用户本地策略库，含策略定位、说明书与公式。
  解读策略时**只能以策略卡原文为准**，不要套用你记忆里的其他交易流派。
- 策略卡带版本号：同一策略不同版本逻辑可能不同，按本次给出的版本理解。
- 个股后面的「行业｜概念」来自本地板块库，是客观标签，不要改写成别的说法。
- 一层粗筛条件刻意放宽，普跌日命中数百只属正常，命中数本身不代表都是好票。

【写作要求】
- 中文，纯文本，禁止 Markdown 语法（不要 #、*、-、表格、代码块），每行不超过 40 个汉字。
- 总共 6~10 行，控制在 500 字以内。
- 数据里若出现【跨策略共振】名单，说明这些票被多个策略同时命中，可信度高于单策略命中。
- 结构：
  第 1 行：一句话总结今天的信号结构（哪个策略放量/萎缩、是否属于普跌放大）；
  中间若干行：3~5 只重点标的，格式「代码 名称：一句话理由」，优先级为
      二层通过 > 出现在【跨策略共振】名单里的 > 单一策略大量命中里的代表；
  最后 1 行：一句风险提示（例如海选口径偏宽、需次日确认）。
- 只使用上文提供的代码、价格、指标、行业与概念，**不得编造**任何标的、数字或消息。
  上下文里没有的股票代码一律不许出现；数据不足时少写，不要凑。
- 提到板块轮动时，只能引用给出的「行业分布」，不要自行补充行业逻辑。
- 不给具体买卖点位、不承诺收益、不推荐仓位。"""


# ---------------------------------------------------------------------------
#  数据摘要（role=user 的内容来源）
# ---------------------------------------------------------------------------
def build_brief(rep, max_per_strategy=8, breadth=None, db=None):
    """把日报结果压成上下文文本。

    这是一个薄封装，真正的装配逻辑在 ai_context.build_context：
        - 传 db：输出策略卡 + 个股行业/概念 + 一层行业分布（生产路径）；
        - 不传 db：只输出命中清单（旧格式，便于离线单测）。

    参数 max_per_strategy 控制每个策略列几只明细，不影响行业分布的统计口径。
    """
    from ai_context import build_context
    return build_context(db, rep, max_per_strategy=max_per_strategy,
                         breadth=breadth, max_chars=MAX_BRIEF_CHARS)


# ---------------------------------------------------------------------------
#  HTTP 调用
# ---------------------------------------------------------------------------
def _post_completion(cfg, messages):
    """POST /chat/completions，返回 (ok, content 或 错误说明, 附加信息)。

    返回约定（三个返回值缺一不可，调用方据此决定是否降级）：
        ok=True   → content 为模型正文，附加信息含耗时与 token 用量；
        ok=False  → content 为可直接展示给用户的中文错误说明；
        附加信息始终是 dict，网络失败时为空 dict。

    这里把常见 HTTP 状态码翻译成用户能看懂的话，避免界面弹出原始 JSON。
    """
    if not cfg.is_configured():
        return False, "AI 未配置（需要 Base URL / API Key / 模型名）", {}

    url = cfg.endpoint()
    # temperature 0.3：日报点评需要稳定、少发散，不需要创意
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
        # 网络层异常（超时、DNS、TLS 等）统一归为"网络异常"
        log.warning("AI 请求异常: %s", e)
        return False, f"网络异常: {e}", {}

    elapsed = time.time() - t0
    body = (r.text or "")[:400]      # 错误时只留前 400 字，避免刷屏

    # ---- 常见错误码 → 中文提示 ----
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

    # OpenAI 兼容格式：choices[0].message.content
    try:
        content = (j["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return False, f"返回结构异常: {body}", {}

    # usage 可能缺失（部分兼容端点不返回），缺失时对应字段为 None
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
    """把调用信息压成一句「12.3s，in 3200，out 480」，用于界面日志。"""
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
    """清理模型偶尔带出的 Markdown 记号。

    为什么需要：系统提示已经要求纯文本，但模型仍可能加 #、**、列表符号或表格，
    而飞书消息和本地 md 都按纯文本排版，残留记号会很难看。
    这里逐行去掉标题井号、加粗符号、列表前缀，并丢弃表格分隔行。
    """
    out = []
    for raw in (text or "").splitlines():
        ln = raw.strip()
        ln = ln.lstrip("#").strip()
        ln = ln.replace("**", "").replace("__", "").strip()
        if ln.startswith(("- ", "* ", "• ")):
            ln = ln[2:].strip()
        if ln.startswith("|") and ln.endswith("|"):
            ln = ln.strip("|").replace("|", " ").strip()
        if set(ln) <= set("-—| "):     # 表格分隔行（|---|）
            continue
        if ln:
            out.append(ln)
    return "\n".join(out)


def summarize(rep, cfg, max_per_strategy=8, breadth=None, db=None):
    """生成 AI 点评。返回 (ok, 文本或错误说明, 附加信息)。

    组装两条消息：
        system = SYSTEM_PROMPT (+ 用户在界面填的「额外要求」)
        user   = "以下是今日扫描数据，请按要求写点评：" + 上下文文本

    参数：
        rep               build_daily_report 的结果
        cfg               AIConfig 实例
        max_per_strategy  每个策略列几只明细
        breadth           市场宽度（可选）
        db                传了才装配策略卡与板块档案；不传退化为旧格式

    注意：本函数不抛异常（调用方仍可 try），失败一律通过返回值表达，
    保证日报主流程不会因为 AI 挂掉而中断。
    """
    brief = build_brief(rep, max_per_strategy=max_per_strategy,
                        breadth=breadth, db=db)
    system = SYSTEM_PROMPT
    if cfg.extra_prompt:
        # 用户附加要求放在系统提示末尾，优先级高于通用写作要求
        system += "\n\n【用户的额外要求】\n" + cfg.extra_prompt
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "以下是今日扫描数据，请按要求写点评：\n\n" + brief},
    ]
    ok, content, info = _post_completion(cfg, messages)
    if not ok:
        return False, content, info
    # 记录实际上下文长度，便于排查"是不是被截断了"
    info["brief_chars"] = len(brief)
    return True, _tidy(content), info


def test_ai(cfg):
    """测试连通性：发一个极小的请求。返回 (ok, 说明)。

    界面上的「测试 AI」按钮调用它，只验证端点/Key/模型是否可用，
    不发送任何真实数据。
    """
    if not cfg.is_configured():
        return False, "AI 未配置（需要 Base URL / API Key / 模型名）"
    messages = [{"role": "user", "content": "回复两个字：可用"}]
    ok, content, info = _post_completion(cfg, messages)
    if ok:
        return True, f"AI 可用：{content.strip()[:40]}（{format_info(info)}）"
    return False, content
