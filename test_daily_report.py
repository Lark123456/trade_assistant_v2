# -*- coding: utf-8 -*-
"""选股日报 / 飞书推送 / 自动日报页签 自测（v0.10.0）。

运行：python test_daily_report.py
覆盖：
    1. 定时判定 should_fire（启用/时间/工作日/已执行/宽限/补跑）
    2. 飞书消息体与签名（拦截 requests，不真发）
    3. 日报汇总（同策略多次扫描只取最近一次会话、分层、截断、市场宽度容错）
    4. 文档落盘
    5. 自动日报页签：构建、配置读写（offscreen）
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["QT_LOGGING_RULES"] = "*.debug=false"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:  # Windows 控制台默认 GBK，中文会乱码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import datetime
import json

import daily_report as dr
import feishu
from daily_report import DailyReportConfig, build_daily_report, save_report, should_fire
from database import Database
from scanner import ScanResult

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK] " if cond else "  [FAIL] ") + name + (f"  {detail}" if detail and not cond else ""))


# ===========================================================================
print("=== 1. 定时判定 should_fire ===")
# ===========================================================================
BASE = DailyReportConfig(enabled=True, hhmm="15:30", weekday_only=True,
                         catchup=False, last_run="")


def _on(target_wd, hour, minute, base=datetime.date(2026, 11, 23)):
    """取 base 起第一个指定星期几的该时刻（0=周一，5=周六）。"""
    d = base + datetime.timedelta(days=(target_wd - base.weekday()) % 7)
    return datetime.datetime.combine(d, datetime.time(hour, minute))


mon = _on(0, 15, 31)      # 周一 15:31（已过 15:30）
sat = _on(5, 15, 31)      # 周六 15:31
mon_late = _on(0, 19, 0)  # 周一 19:00（超 2 小时宽限）

check("未启用不触发", should_fire(DailyReportConfig(enabled=False), mon)[0] is False)
check("未到点不触发", should_fire(BASE, _on(0, 15, 0))[0] is False)
check("到点触发", should_fire(BASE, mon)[0] is True)
check("周末不执行", should_fire(BASE, sat)[0] is False)
check("今天已执行不重复", should_fire(
    DailyReportConfig(**{**BASE.__dict__, "last_run": mon.date().isoformat()}),
    mon)[0] is False)
check("超宽限不执行", should_fire(BASE, mon_late)[0] is False)
check("超宽限+补跑执行", should_fire(
    DailyReportConfig(**{**BASE.__dict__, "catchup": True}), mon_late)[0] is True)
check("宽限内自动补", should_fire(BASE, _on(0, 16, 30))[0] is True)
check("非工作日限制放开", should_fire(
    DailyReportConfig(**{**BASE.__dict__, "weekday_only": False}), sat)[0] is True)

# ===========================================================================
print("=== 2. 飞书消息体 + 签名 ===")
# ===========================================================================
captured = {}


class _Resp:
    status_code = 200

    def __init__(self, payload=None):
        self._payload = payload if payload is not None else {"code": 0, "msg": "success"}
        self.text = json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


calls = {"token": 0, "msg": 0, "hook": 0}
next_im_error = {"code": None}


def _fake_post(url, json=None, timeout=None, headers=None):
    captured["url"] = url
    captured["payload"] = json
    captured["headers"] = headers
    if "tenant_access_token" in url:
        calls["token"] += 1
        captured["token_url"] = url
        captured["token_payload"] = json
        return _Resp({"code": 0, "msg": "ok",
                      "tenant_access_token": "t-fake-token", "expire": 7200})
    if "/im/v1/messages" in url:
        calls["msg"] += 1
        if next_im_error["code"]:
            code = next_im_error["code"]
            next_im_error["code"] = None
            return _Resp({"code": code, "msg": "mock error"})
        return _Resp({"code": 0, "msg": "success",
                      "data": {"message_id": "om_123"}})
    calls["hook"] += 1
    return _Resp()


feishu.requests.post = _fake_post

# ---- 自定义机器人（Webhook）----
ok, msg = feishu.send_feishu("https://open.feishu.cn/open-apis/bot/v2/hook/abc",
                             "", "标题", ["第一行", "第二行"])
p = captured["payload"]
check("富文本发送成功", ok is True, msg)
check("msg_type=post", p.get("msg_type") == "post")
check("标题正确", p["content"]["post"]["zh_cn"]["title"] == "标题")
check("正文两段", len(p["content"]["post"]["zh_cn"]["content"]) == 2)
check("无密钥时不带 sign", "sign" not in p)

ok2, _ = feishu.send_feishu("https://x/hook/y", "SECRET123", "T", ["a"])
p2 = captured["payload"]
check("带密钥时含 timestamp/sign", "timestamp" in p2 and "sign" in p2,
      f"{p2.get('timestamp')} {p2.get('sign')}")
check("签名与独立计算一致",
      p2.get("sign") == feishu.make_sign(p2.get("timestamp"), "SECRET123"))
check("webhook 打码不泄露全串",
      feishu.mask_webhook("https://open.feishu.cn/open-apis/bot/v2/hook/abcdef123456")
      .endswith("123456"))

# 过长内容截断
ok3, _ = feishu.send_feishu("https://x/hook/y", "", "T", ["x" * 500] * 40)
check("超长内容被截断", len(captured["payload"]["content"]["post"]["zh_cn"]["content"]) < 40)

# 只粘 token / 只粘路径 也要能用（用户实测反馈）
TOKEN = "oc_b41562840b49ca979df302e9a5acff5a"
check("裸 token 自动补全",
      feishu.normalize_webhook(TOKEN)
      == "https://open.feishu.cn/open-apis/bot/v2/hook/" + TOKEN,
      feishu.normalize_webhook(TOKEN))
check("只粘路径自动补全",
      feishu.normalize_webhook("/open-apis/bot/v2/hook/abc")
      == "https://open.feishu.cn/open-apis/bot/v2/hook/abc")
check("完整 URL 原样返回",
      feishu.normalize_webhook("https://open.feishu.cn/open-apis/bot/v2/hook/x")
      == "https://open.feishu.cn/open-apis/bot/v2/hook/x")
check("域名缺协议也能补",
      feishu.normalize_webhook("open.feishu.cn/open-apis/bot/v2/hook/y")
      == "https://open.feishu.cn/open-apis/bot/v2/hook/y")
check("空白输入视为未配置", feishu.normalize_webhook("   ") == "")
ok4, msg4 = feishu.send_feishu(TOKEN, "", "T", ["a"])
check("裸 token 可直接发送成功",
      ok4 is True and captured["url"].endswith("/hook/" + TOKEN),
      f"{ok4} {msg4} {captured.get('url')}")
check("错误码带可操作提示",
      "签名" in feishu._ERROR_HINTS.get(19021, "")
      and "关键词" in feishu._ERROR_HINTS.get(19024, ""))

# ---- 自建应用机器人（app_id + app_secret + receive_id）----
feishu.invalidate_token()
calls.update({"token": 0, "msg": 0})
CREDS_APP = {"mode": "app", "app_id": "cli_test", "app_secret": "secret_test",
             "receive_id": "oc_b41562840b49ca979df302e9a5acff5a",
             "receive_id_type": "chat_id"}
ok5, msg5 = feishu.send_message(CREDS_APP, "选股日报", lines=["第一行", "第二行"])
check("应用机器人发送成功", ok5 is True, msg5)
check("先换 token 再发消息", calls["token"] == 1 and calls["msg"] == 1,
      f"{calls}")
check("token 请求地址正确",
      "auth/v3/tenant_access_token/internal" in captured.get("token_url", ""),
      captured.get("token_url"))
check("token 请求带上 app_id/app_secret",
      (captured.get("token_payload") or {}).get("app_id") == "cli_test"
      and (captured.get("token_payload") or {}).get("app_secret") == "secret_test")
check("发送走 im/v1/messages + receive_id_type",
      "/im/v1/messages" in captured["url"]
      and captured["url"].endswith("receive_id_type=chat_id"), captured["url"])
check("Authorization 头带 tenant token",
      (captured.get("headers") or {}).get("Authorization") == "Bearer t-fake-token",
      str(captured.get("headers")))
check("receive_id 原样传给飞书",
      captured["payload"].get("receive_id") == CREDS_APP["receive_id"])
check("IM 的 content 是 JSON 字符串（不是对象）",
      isinstance(captured["payload"].get("content"), str))
check("IM content 里是 zh_cn 富文本",
      "zh_cn" in json.loads(captured["payload"]["content"]))
check("msg_type=post", captured["payload"].get("msg_type") == "post")

# token 缓存：第二次不再重复换取
ok6, _ = feishu.send_message(CREDS_APP, "选股日报", lines=["第三行"])
check("token 命中缓存不重复换取",
      ok6 is True and calls["token"] == 1 and calls["msg"] == 2, str(calls))

# 纯文本（无 lines）
ok7, _ = feishu.send_message(CREDS_APP, "", text="只有一行文本")
check("纯文本走 msg_type=text", ok7 is True and captured["payload"]["msg_type"] == "text")

# 错误码提示
next_im_error["code"] = 230002
ok8, msg8 = feishu.send_message(CREDS_APP, "x", lines=["y"])
check("机器人不在群时给出提示", ok8 is False and "拉进群" in msg8, msg8)

# 凭证校验
check("应用凭证不全时判定未配置",
      feishu.is_configured({"mode": "app", "app_id": "a"}) is False)
check("应用凭证齐全时判定已配置", feishu.is_configured(CREDS_APP) is True)
check("webhook 模式判定", feishu.is_configured({"mode": "webhook", "webhook": "x"}) is True,
      str(feishu.normalize_webhook("x")))
check("describe 不泄露 secret",
      "secret_test" not in feishu.describe(CREDS_APP))

# ===========================================================================
print("=== 3. 日报汇总 ===")
# ===========================================================================
tmpdir = tempfile.mkdtemp(prefix="ta_daily_")
db = Database(os.path.join(tmpdir, "t.db"))
day = datetime.date.today().isoformat()


def hit(code, name, price, pct, layer=1, **kw):
    return ScanResult(code=code, name=name, market=0, price=price, pct=pct,
                      vol=1000, amount=10000, match_reason="条件成立",
                      signal_date=day, **kw)


# 同一策略扫了两次：旧会话 2 只，新会话 3 只 → 日报只应统计 3 只
db.add_scan_hits("s_old", 16, "B1海选",
                 [hit("000001", "旧一", 10, 1), hit("000002", "旧二", 11, 2)], layer=1)
db.add_scan_hits("s_new", 16, "B1海选",
                 [hit("002869", "金溢科技", 18.0, -3.17),
                  hit("600051", "宁波联合", 6.2, 0.5),
                  hit("300750", "宁德时代", 200.0, 2.0)], layer=1)
# 二层（ScanResult 用 dna_score/dna_template 承载骨架分与模具名，
# 落库时 add_scan_hits 会写入 dtw_score/matched_template 列）
db.add_scan_hits("s_l2", 27, "B1十张图",
                 [hit("002516", "旷达科技", 5.5, 1.2, layer=2,
                      dna_score=78.5, final_score=82.5,
                      dna_template="澄天伟业_经典B1缩量回踩",
                      buy_level="SB1核心买点")], layer=2)

rep = build_daily_report(db, day=day, max_per_strategy=2)
st = rep["stats"]
check("策略数=2", st["strategies"] == 2, str(st))
check("一层命中=3（只取最近会话）", st["layer1"] == 3, str(st))
check("二层命中=1", st["layer2"] == 1, str(st))
check("合计=4", st["total"] == 4, str(st))
check("文档含一层标题", "## 一、一层粗筛" in rep["markdown"])
check("文档含二层标题", "## 二、二层精筛" in rep["markdown"])
check("文档含截断提示", "其余 1 只未逐条列出" in rep["markdown"], rep["markdown"][-500:])
check("二层行含综合分与买点",
      "旷达科技" in rep["markdown"] and "SB1核心买点" in rep["markdown"])
check("旧会话记录未混入", "旧一" not in rep["markdown"] and "旧二" not in rep["markdown"])
check("飞书精简行含策略计数",
      any("【一层·B1海选】3 只" in ln for ln in rep["feishu_lines"]), str(rep["feishu_lines"]))
check("飞书行数受 max_per_strategy 限制",
      sum(1 for ln in rep["feishu_lines"] if "旧" in ln) == 0)

empty = build_daily_report(db, day="2020-01-01")
check("空日期不报错", empty["stats"]["total"] == 0 and "没有任何扫描命中记录" in empty["markdown"])

# 市场宽度：无缓存数据时应为 None 而不是抛错
check("市场宽度无数据返回 None", db.get_market_breadth("2020-01-01") is None)

# ===========================================================================
print("=== 4. 文档落盘 ===")
# ===========================================================================
path = save_report(day, rep["markdown"], directory=tmpdir)
check("文件已生成", os.path.exists(path), path)
with open(path, encoding="utf-8") as f:
    content = f.read()
check("文件内容一致", content == rep["markdown"])

# ===========================================================================
print("=== 5. 配置读写 ===")
# ===========================================================================
cfg = DailyReportConfig(enabled=True, hhmm="16:05", weekday_only=False,
                        mode="app", app_id="cli_cfg", app_secret="sec_cfg",
                        receive_id="oc_cfg", receive_id_type="open_id",
                        webhook="https://open.feishu.cn/open-apis/bot/v2/hook/x",
                        secret="s3cret", max_per_strategy=7,
                        include_watch=False, include_alert=True,
                        save_local=True, catchup=True, last_run=day)
cfg.save(db)
cfg2 = DailyReportConfig.load(db)
check("配置回读一致",
      (cfg2.enabled, cfg2.hhmm, cfg2.weekday_only, cfg2.webhook, cfg2.secret,
       cfg2.max_per_strategy, cfg2.include_watch, cfg2.include_alert,
       cfg2.save_local, cfg2.catchup, cfg2.last_run)
      == (True, "16:05", False, cfg.webhook, "s3cret", 7, False, True, True, True, day),
      str(cfg2))
check("应用机器人配置回读一致",
      (cfg2.mode, cfg2.app_id, cfg2.app_secret, cfg2.receive_id, cfg2.receive_id_type)
      == ("app", "cli_cfg", "sec_cfg", "oc_cfg", "open_id"), str(cfg2))
check("credentials() 结构与 mode 一致",
      cfg2.credentials()["mode"] == "app"
      and cfg2.credentials()["receive_id"] == "oc_cfg")
# 老库没有 feishu_mode 时按 webhook 推断
db.set_setting("feishu_mode", "")
db.set_setting("feishu_webhook", "https://open.feishu.cn/open-apis/bot/v2/hook/old")
check("老配置无 mode 时推断为 webhook",
      DailyReportConfig.load(db).mode == "webhook")
db.set_setting("feishu_mode", "app")
cfg2.mark_ran(db, "2026-01-02")
check("mark_ran 落库", DailyReportConfig.load(db).last_run == "2026-01-02")
check("parse_hhmm 非法回退", dr.parse_hhmm("bad") == (15, 30))
check("parse_hhmm 正常", dr.parse_hhmm("09:05") == (9, 5))

# ===========================================================================
print("=== 5b. 信号日回退（v0.10.3） ===")
# ===========================================================================
TODAY = "2026-11-24"
PREV = "2026-11-21"          # 上一交易日
db2 = Database(os.path.join(tmpdir, "t2.db"))

check("缓存全是旧数据时用缓存最近日",
      dr.resolve_scan_date(db2, probe=None, today=TODAY)[0] == TODAY)  # 空库 → 按今天

# 塞一根旧日K：缓存最大日期停在上一交易日
db2.cache_daily_bars("600519", 1, __import__("pandas").DataFrame([
    {"date": PREV, "open": 1, "high": 1, "low": 1, "close": 1, "vol": 1, "amount": 1}]))
check("探测到上一交易日 → 回退",
      dr.resolve_scan_date(db2, probe=lambda c, m: PREV, today=TODAY)[0] == PREV)
check("回退说明里点明今天没有日K",
      "回退" in dr.resolve_scan_date(db2, probe=lambda c, m: PREV, today=TODAY)[1])
check("探测到今天 → 用今天",
      dr.resolve_scan_date(db2, probe=lambda c, m: TODAY, today=TODAY)[0] == TODAY)
check("探测不可用时按缓存最近日",
      dr.resolve_scan_date(db2, probe=lambda c, m: None, today=TODAY)[0] == PREV)
check("探测抛异常不炸",
      dr.resolve_scan_date(
          db2, probe=lambda c, m: (_ for _ in ()).throw(RuntimeError("x")),
          today=TODAY)[0] == PREV)
check("缓存已含今天时直接返回今天",
      dr.resolve_scan_date(db2, probe=None, today=PREV)[0] == PREV)
check("count_cached_bars_on 统计正确",
      db2.count_cached_bars_on(PREV) == 1 and db2.count_cached_bars_on("2026-01-01") == 0)
db2.close()

# ===========================================================================
print("=== 6. AI 点评（v0.10.4） ===")
# ===========================================================================
import ai_summary  # noqa: E402

AI_REPLY = {
    "choices": [{"message": {"content": (
        "# 今日点评\n"
        "**信号结构**：普跌放大，一层命中 953 只，明显偏多。\n"
        "- 002516 旷达科技：二层通过，综合分最高。\n"
        "| 代码 | 名称 |\n|---|---|\n"
        "风险提示：海选口径偏宽，需次日确认。")}}],
    "usage": {"prompt_tokens": 1200, "completion_tokens": 180},
}
ai_state = {"status": 200, "reply": AI_REPLY, "url": None, "payload": None}


def _fake_ai_post(url, headers=None, json=None, timeout=None):
    ai_state["url"] = url
    ai_state["payload"] = json
    ai_state["headers"] = headers

    class _R:
        status_code = ai_state["status"]
        text = "{}"

        def json(self):
            if isinstance(ai_state["reply"], Exception):
                raise ai_state["reply"]
            return ai_state["reply"]

    return _R()


ai_summary.requests.post = _fake_ai_post

AI_CFG = ai_summary.AIConfig(
    enabled=True, base_url="https://ark.cn-beijing.volces.com/api/v3/",
    api_key="sk-test", model="doubao-test")

check("endpoint 自动拼 /chat/completions",
      AI_CFG.endpoint() == "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
      AI_CFG.endpoint())
check("endpoint 已含完整路径时不重复拼",
      ai_summary.AIConfig(base_url="https://x/v1/chat/completions").endpoint()
      == "https://x/v1/chat/completions")
check("默认配置视为未配置", ai_summary.AIConfig().is_configured() is False)
check("填齐后视为已配置", AI_CFG.is_configured() is True)

brief = ai_summary.build_brief(rep, max_per_strategy=2)
check("上下文含日期", "日期：" in brief)
check("上下文含命中合计", "命中合计" in brief)
check("上下文按层分组", "【二层精筛】" in brief and "【一层粗筛】" in brief)
check("上下文截断到每策略 2 只（涨幅最低的被截掉）",
      "（列前 2 只）" in brief and "金溢科技" not in brief, brief[-300:])
check("上下文不超过字数上限", len(ai_summary.build_brief(rep, 50)) <= 4100)

# 跨策略共振：同一只票被多个策略命中时单独列出
rep_ov = {
    "day": day,
    "stats": {"total": 2, "layer1": 1, "layer2": 1, "strategies": 2},
    "sections": [
        {"layer": 1, "strategy": "B1海选", "count": 1,
         "rows": [{"code": "000001", "name": "甲股", "price": 10.0, "pct": 1.0}]},
        {"layer": 2, "strategy": "B1十张图", "count": 1,
         "rows": [{"code": "000001", "name": "甲股", "final_score": 81.0,
                   "dtw_score": 70.0, "buy_level": "B1底仓买点",
                   "matched_template": "某模具"}]},
    ],
}
brief_ov = ai_summary.build_brief(rep_ov)
check("共振名单被列出",
      "【跨策略共振】" in brief_ov and "B1海选、B1十张图" in brief_ov, brief_ov[-200:])

# 二层但没打分（个别策略执行失败仍入库）→ 退回普通写法，不要出现「综合-」
rep_ns = {
    "day": day, "stats": {"total": 1, "layer1": 0, "layer2": 1, "strategies": 1},
    "sections": [{"layer": 2, "strategy": "B2灾后重建", "count": 1,
                  "rows": [{"code": "000888", "name": "峨眉山A", "price": 12.3,
                            "pct": -1.2, "final_score": None}]}],
}
brief_ns = ai_summary.build_brief(rep_ns)
check("二层无分数时退回普通写法",
      "综合-" not in brief_ns and "收12.30" in brief_ns, brief_ns)

ok_ai, text_ai, info_ai = ai_summary.summarize(rep, AI_CFG, max_per_strategy=2)
check("AI 点评调用成功", ok_ai is True, text_ai)
check("请求打到配置的端点",
      ai_state["url"] == AI_CFG.endpoint(), str(ai_state["url"]))
check("请求带 Bearer Key",
      (ai_state["headers"] or {}).get("Authorization") == "Bearer sk-test")
check("请求体含模型与消息",
      ai_state["payload"]["model"] == "doubao-test"
      and len(ai_state["payload"]["messages"]) == 2)
check("System 提示里写明了策略体系与防编造约束",
      "B1海选" in ai_state["payload"]["messages"][0]["content"]
      and "不得编造" in ai_state["payload"]["messages"][0]["content"])
check("User 提示里带上了数据",
      "命中合计" in ai_state["payload"]["messages"][1]["content"])
check("去掉了 Markdown 记号",
      "#" not in text_ai and "**" not in text_ai and "|" not in text_ai,
      text_ai)
check("保留了正文要点", "风险提示" in text_ai and "旷达科技" in text_ai)
check("token 用量被解析", info_ai.get("prompt_tokens") == 1200)

# 额度/认证失败要给出可读结论
ai_state["status"] = 401
ok_bad, text_bad, _ = ai_summary.summarize(rep, AI_CFG)
check("401 给出认证失败提示", ok_bad is False and "认证失败" in text_bad, text_bad)
ai_state["status"] = 200
ai_state["reply"] = {"error": {"message": "bad"}}
ok_bad2, text_bad2, _ = ai_summary.summarize(rep, AI_CFG)
check("异常返回结构不崩", ok_bad2 is False and "结构异常" in text_bad2, text_bad2)
ai_state["reply"] = AI_REPLY

ok_t, msg_t = ai_summary.test_ai(AI_CFG)
check("test_ai 成功", ok_t is True and "可用" in msg_t, msg_t)
check("test_ai 未配置时提示",
      ai_summary.test_ai(ai_summary.AIConfig())[0] is False)

AI_CFG.save(db)
ai_loaded = ai_summary.AIConfig.load(db)
check("AI 配置回读一致",
      (ai_loaded.enabled, ai_loaded.api_key, ai_loaded.model,
       ai_loaded.base_url.rstrip("/") == AI_CFG.base_url.rstrip("/"))
      == (True, "sk-test", "doubao-test", True), str(ai_loaded))

# ===========================================================================
print("=== 7. 自动日报页签（offscreen） ===")
# ===========================================================================
try:
    from PyQt5.QtCore import QTime
    from PyQt5.QtWidgets import QApplication

    from data_feed import DataFeed
    from ui.scan_center import ScanCenter
    from workers import Worker

    app = QApplication.instance() or QApplication(sys.argv)
    worker = Worker(DataFeed(), db)
    sc = ScanCenter(worker, db)
    check("页签数量=4", sc.tabs.count() == 4, str(sc.tabs.count()))
    check("第 4 个页签是自动日报", sc.tabs.tabText(3) == "自动日报")
    check("配置已在界面上回填", sc.feishu_secret.text() == "s3cret")
    check("时间控件回填 16:05", sc.daily_time.time().toString("HH:mm") == "16:05")
    check("推送方式回填为应用机器人", sc.feishu_mode.currentData() == "app")
    check("App ID / 接收方回填",
          sc.feishu_app_id.text() == "cli_cfg" and sc.feishu_receive_id.text() == "oc_cfg")
    check("接收方类型回填 open_id",
          sc.feishu_receive_type.currentData() == "open_id")
    check("应用模式下 Webhook 输入框禁用",
          sc.feishu_webhook.isEnabled() is False and sc.feishu_app_id.isEnabled() is True)

    # 切到自定义机器人：控件启用关系应反过来
    sc.feishu_mode.setCurrentIndex(sc.feishu_mode.findData("webhook"))
    check("自定义机器人模式下 Webhook 可用",
          sc.feishu_webhook.isEnabled() is True and sc.feishu_app_id.isEnabled() is False)
    sc.feishu_mode.setCurrentIndex(sc.feishu_mode.findData("app"))

    sc.daily_enabled.setChecked(True)
    sc.daily_time.setTime(QTime(14, 45))
    sc.daily_weekday.setChecked(True)
    sc.feishu_app_id.setText("cli_new")
    sc.feishu_app_secret.setText("sec_new")
    sc.feishu_receive_id.setText("oc_new")
    sc.daily_max.setValue(3)
    sc._save_daily_cfg()
    saved = DailyReportConfig.load(db)
    check("界面保存生效", (saved.enabled, saved.hhmm, saved.max_per_strategy)
          == (True, "14:45", 3), f"{saved.enabled} {saved.hhmm} {saved.max_per_strategy}")
    check("界面保存应用凭证生效",
          (saved.mode, saved.app_id, saved.app_secret, saved.receive_id)
          == ("app", "cli_new", "sec_new", "oc_new"),
          f"{saved.mode} {saved.app_id} {saved.receive_id}")

    # v0.10.4：AI 配置回填与保存
    check("AI 配置回填到界面",
          sc.ai_model.text() == "doubao-test" and sc.ai_api_key.text() == "sk-test",
          f"{sc.ai_model.text()} / {sc.ai_api_key.text()}")
    sc.ai_enabled.setChecked(True)
    sc.ai_model.setText("deepseek-chat")
    sc.ai_base_url.setText("https://api.deepseek.com/v1")
    sc.ai_api_key.setText("sk-new")
    sc.ai_extra.setText("少提普涨")
    sc._save_daily_cfg()
    ai_saved = ai_summary.AIConfig.load(db)
    check("界面保存 AI 配置生效",
          (ai_saved.enabled, ai_saved.model, ai_saved.base_url, ai_saved.api_key,
           ai_saved.extra_prompt)
          == (True, "deepseek-chat", "https://api.deepseek.com/v1", "sk-new", "少提普涨"),
          str(ai_saved))
    check("运行日志有记录", "配置已保存" in sc.daily_log_view.toPlainText())

    # 未到点/未启用时不触发扫描
    sc.daily_enabled.setChecked(False)
    sc._on_daily_tick()
    check("未启用时 tick 不触发", sc._daily_pending is False)

    sc.deleteLater()
    worker.stop()
except Exception as e:  # noqa: BLE001
    import traceback
    traceback.print_exc()
    check("页签构建", False, str(e))

db.close()

# ===========================================================================
print()
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")
