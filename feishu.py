# -*- coding: utf-8 -*-
"""飞书推送（v0.10.2）：支持两种接入方式。

1. **自建应用机器人**（app_id + app_secret + receive_id）—— 推荐，权限与审计可控
   ::

       POST /open-apis/auth/v3/tenant_access_token/internal   {app_id, app_secret}
       POST /open-apis/im/v1/messages?receive_id_type=chat_id
            Authorization: Bearer <tenant_access_token>
            {receive_id, msg_type, content}

   需要应用开通 `im:message:send_as_bot` 权限，并把机器人拉进目标群
   （或用 open_id / user_id / email 发给个人）。

2. **自定义机器人**（Webhook）—— 最简单，只有一条 URL
   ::

       POST https://open.feishu.cn/open-apis/bot/v2/hook/<token>

用法（统一入口）::

    creds = {"mode": "app", "app_id": "...", "app_secret": "...",
             "receive_id": "oc_xxx", "receive_id_type": "chat_id"}
    send_message(creds, "标题", lines=["第一行", "第二行"])

    creds = {"mode": "webhook", "webhook": "https://open.feishu.cn/...", "secret": ""}
    send_message(creds, "标题", lines=[...])

配置键（settings 表）见 ``daily_report.py``：``feishu_mode`` / ``feishu_webhook`` /
``feishu_secret`` / ``feishu_app_id`` / ``feishu_app_secret`` /
``feishu_receive_id`` / ``feishu_receive_id_type``。

参考：
    - 自定义机器人 https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot
    - 发送消息 https://open.feishu.cn/document/server-docs/im-v1/message/create
"""
import base64
import hashlib
import hmac
import json
import logging
import time

import requests

log = logging.getLogger("feishu")

# 飞书单条消息体上限约 20KB；这里按字符数保守截断，留足 JSON 包装余量
MAX_CHARS = 6000
TIMEOUT = 10

HOOK_PREFIX = "https://open.feishu.cn/open-apis/bot/v2/hook/"
TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
SEND_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

_HOOK_PATHS = ("open-apis/bot/v2/hook/",)
_KNOWN_HOSTS = ("open.feishu.cn/", "www.feishu.cn/", "open.larksuite.com/",
                "open.larkoffice.com/")

# 应用机器人：接收方 ID 类型
RECEIVE_ID_TYPES = {
    "chat_id": "群 chat_id（oc_ 开头，机器人需在群里）",
    "open_id": "用户 open_id（ou_ 开头）",
    "user_id": "用户 user_id",
    "email": "用户邮箱",
}

# 自定义机器人错误码 → 可操作提示
_ERROR_HINTS = {
    19001: "Webhook 地址不对（请重新复制群机器人里完整的 Webhook）",
    19002: "请求体格式不合法",
    19003: "机器人已被移出该群，请重新添加",
    19021: "签名校验失败：核对「签名密钥」，并确认本机时间准确",
    19022: "IP 不在机器人白名单内",
    19024: "消息里没有命中机器人设置的「自定义关键词」",
}

# 应用机器人（IM API）错误码 → 提示
_IM_HINTS = {
    230001: "参数错误：检查 receive_id 与「接收方类型」是否匹配",
    230002: "机器人不在该群 / 会话不存在：先把应用机器人拉进群",
    230006: "机器人能力未开启",
    230013: "用户不在应用的可用范围内（开发者后台 → 权限 → 可用范围）",
    99991663: "tenant_access_token 无效，请检查 app_id / app_secret",
    99991664: "tenant_access_token 无效，请检查 app_id / app_secret",
    99991672: "应用缺少发消息权限：开通 im:message:send_as_bot 并发布版本",
    99991668: "应用未发布 / 版本未生效",
}

# 换取 token 的错误码 → 提示
_TOKEN_HINTS = {
    10003: "app_id 不存在",
    10014: "app_secret 不正确",
    40004: "参数错误：app_id 或 app_secret 为空",
}

# tenant_access_token 进程内缓存：{(app_id, app_secret): (token, 过期时间戳)}
_TOKEN_CACHE = {}
_TOKEN_MARGIN = 120          # 提前 2 分钟视为过期，避免边界失败


# ---------------------------------------------------------------------------
#  自定义机器人（Webhook）
# ---------------------------------------------------------------------------
def normalize_webhook(url):
    """把用户粘进来的各种形态补全成完整 Webhook 地址。

    飞书群里复制的可能是：完整 URL、``open-apis/bot/v2/hook/xxx``、
    或者只有最后那串 token。后两种都自动补全。
    """
    u = (url or "").strip().strip('"').strip("'").strip()
    if not u:
        return ""
    low = u.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return u
    u = u.lstrip("/")
    low = u.lower()
    for host in _KNOWN_HOSTS:
        if low.startswith(host):
            return "https://" + u
    for path in _HOOK_PATHS:
        if low.startswith(path):
            return "https://open.feishu.cn/" + u
    return HOOK_PREFIX + u


def mask_webhook(url):
    """日志用：先补全成完整地址，再只保留头部与尾部。"""
    u = normalize_webhook(url)
    if not u:
        return "(未配置)"
    return u[:38] + "…" + u[-6:] if len(u) > 48 else u


def mask_secret(value, keep=3):
    """日志用：密钥只留首尾几位。"""
    s = (value or "").strip()
    if not s:
        return "(未配置)"
    if len(s) <= keep * 2:
        return "*" * len(s)
    return s[:keep] + "…" + s[-2:]


def make_sign(timestamp, secret):
    """飞书签名算法：以 ``"<timestamp>\\n<secret>"`` 为 key 对空串做 HMAC-SHA256，再 base64。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"),
                      digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _with_sign(payload, secret):
    if not secret or not secret.strip():
        return payload
    ts = str(int(time.time()))
    payload = dict(payload)
    payload["timestamp"] = ts
    payload["sign"] = make_sign(ts, secret.strip())
    return payload


def _clip_lines(lines):
    """按字符预算截断行列表，返回 (lines, 是否截断)。"""
    out, used, cut = [], 0, False
    for ln in lines:
        ln = "" if ln is None else str(ln)
        if used + len(ln) + 1 > MAX_CHARS:
            cut = True
            break
        out.append(ln)
        used += len(ln) + 1
    return out, cut


def _hint_of(mapping, code):
    try:
        return mapping.get(int(code))
    except (TypeError, ValueError):
        return None


def _post_webhook(webhook, payload):
    """POST 到自定义机器人 webhook，返回 (ok, 说明)。"""
    url = normalize_webhook(webhook)
    if not url:
        return False, "未配置飞书 Webhook"
    try:
        r = requests.post(url, json=payload, timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001
        log.warning("飞书推送异常: %s", e)
        return False, f"网络异常: {e}"

    body = (r.text or "")[:300]
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {body}"

    try:
        j = r.json()
    except ValueError:
        return False, f"返回非 JSON: {body}"

    # 新接口 code/msg，旧接口 StatusCode/StatusMessage
    code = j.get("code", j.get("StatusCode"))
    msg = j.get("msg", j.get("StatusMessage", ""))
    if code == 0:
        return True, "飞书推送成功"
    log.warning("飞书推送被拒: %s %s", code, msg)
    hint = _hint_of(_ERROR_HINTS, code)
    return False, f"飞书返回错误 {code}: {msg}" + (f"（{hint}）" if hint else "")


def send_feishu_post(webhook, secret, title, lines):
    """自定义机器人 · 富文本消息（msg_type=post）。"""
    if not normalize_webhook(webhook):
        return False, "未配置飞书 Webhook"

    clipped, cut = _clip_lines(lines or [])
    if cut:
        clipped.append("……（内容过长已截断，完整版见本地文档）")

    payload = {
        "msg_type": "post",
        "content": {"post": {"zh_cn": {
            "title": (title or "选股日报")[:100],
            "content": [[{"tag": "text", "text": ln}] for ln in clipped] or
                       [[{"tag": "text", "text": "（无内容）"}]],
        }}},
    }
    return _post_webhook(webhook, _with_sign(payload, secret))


def send_feishu_text(webhook, secret, text):
    """自定义机器人 · 纯文本消息（msg_type=text）。"""
    if not normalize_webhook(webhook):
        return False, "未配置飞书 Webhook"
    clipped, cut = _clip_lines((text or "").splitlines())
    if cut:
        clipped.append("……（已截断）")
    payload = {"msg_type": "text", "content": {"text": "\n".join(clipped)}}
    return _post_webhook(webhook, _with_sign(payload, secret))


# ---------------------------------------------------------------------------
#  自建应用机器人（app_id + app_secret + receive_id）
# ---------------------------------------------------------------------------
def get_tenant_token(app_id, app_secret, force=False):
    """取 tenant_access_token，返回 (ok, token 或错误说明)。进程内带缓存。"""
    app_id = (app_id or "").strip()
    app_secret = (app_secret or "").strip()
    if not app_id or not app_secret:
        return False, "未配置 App ID / App Secret"

    key = (app_id, app_secret)
    now = time.time()
    if not force:
        cached = _TOKEN_CACHE.get(key)
        if cached and cached[1] > now:
            return True, cached[0]

    try:
        r = requests.post(TOKEN_URL,
                          json={"app_id": app_id, "app_secret": app_secret},
                          timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001
        log.warning("获取 tenant_access_token 异常: %s", e)
        return False, f"网络异常: {e}"

    try:
        j = r.json()
    except ValueError:
        return False, f"换取 token 返回非 JSON: {(r.text or '')[:200]}"

    code = j.get("code", j.get("StatusCode"))
    if code == 0:
        token = j.get("tenant_access_token") or j.get("app_access_token")
        if not token:
            return False, "换取 token 成功但返回里没有 tenant_access_token"
        try:
            expire = int(j.get("expire") or 7200)
        except (TypeError, ValueError):
            expire = 7200
        _TOKEN_CACHE[key] = (token, now + max(60, expire - _TOKEN_MARGIN))
        return True, token

    msg = j.get("msg", "")
    hint = _hint_of(_TOKEN_HINTS, code)
    log.warning("换取 tenant_access_token 失败: %s %s", code, msg)
    return False, f"换取 token 失败 {code}: {msg}" + (f"（{hint}）" if hint else "")


def invalidate_token(app_id=None, app_secret=None):
    """清掉 token 缓存（换密钥或想强制刷新时用）。"""
    if app_id is None:
        _TOKEN_CACHE.clear()
        return
    key = ((app_id or "").strip(), (app_secret or "").strip())
    _TOKEN_CACHE.pop(key, None)


def _build_im_content(title, lines, text=None):
    """构造 IM API 的 (msg_type, content 字符串)。

    注意 IM API 的 content 是 **JSON 字符串**，不是对象。
    """
    if lines:
        clipped, cut = _clip_lines(lines)
        if cut:
            clipped.append("……（内容过长已截断，完整版见本地文档）")
        post = {"zh_cn": {
            "title": (title or "选股日报")[:100],
            "content": [[{"tag": "text", "text": ln}] for ln in clipped] or
                       [[{"tag": "text", "text": "（无内容）"}]],
        }}
        return "post", json.dumps(post, ensure_ascii=False)

    raw = text if text is not None else "\n".join([title or ""] + list(lines or []))
    clipped, cut = _clip_lines((raw or "").splitlines())
    if cut:
        clipped.append("……（已截断）")
    return "text", json.dumps({"text": "\n".join(clipped)}, ensure_ascii=False)


def send_app_message(app_id, app_secret, receive_id, receive_id_type,
                     title, lines=None, text=None):
    """自建应用机器人发消息，返回 (ok, 说明)。"""
    receive_id = (receive_id or "").strip()
    if not receive_id:
        return False, "未配置接收方 ID（chat_id / open_id / user_id / email）"
    rid_type = (receive_id_type or "chat_id").strip() or "chat_id"

    ok, token = get_tenant_token(app_id, app_secret)
    if not ok:
        return False, token

    msg_type, content = _build_im_content(title, lines, text)
    url = f"{SEND_URL}?receive_id_type={rid_type}"
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json; charset=utf-8"},
            json={"receive_id": receive_id, "msg_type": msg_type,
                  "content": content},
            timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001
        log.warning("应用机器人发送异常: %s", e)
        return False, f"网络异常: {e}"

    body = (r.text or "")[:300]
    try:
        j = r.json()
    except ValueError:
        return False, f"HTTP {r.status_code} 返回非 JSON: {body}"

    code = j.get("code", j.get("StatusCode"))
    msg = j.get("msg", j.get("StatusMessage", ""))
    if code == 0:
        mid = (j.get("data") or {}).get("message_id") or ""
        return True, "飞书推送成功" + (f"（message_id={mid}）" if mid else "")

    # token 失效：清缓存后重试一次
    if code in (99991663, 99991664, 99991665):
        log.info("tenant_access_token 失效，强制刷新后重试一次")
        invalidate_token(app_id, app_secret)
        ok2, token2 = get_tenant_token(app_id, app_secret, force=True)
        if ok2:
            try:
                r2 = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {token2}",
                             "Content-Type": "application/json; charset=utf-8"},
                    json={"receive_id": receive_id, "msg_type": msg_type,
                          "content": content},
                    timeout=TIMEOUT)
                j2 = r2.json()
                if j2.get("code", j2.get("StatusCode")) == 0:
                    mid = (j2.get("data") or {}).get("message_id") or ""
                    return True, "飞书推送成功" + (f"（message_id={mid}）" if mid else "")
                code, msg = j2.get("code"), j2.get("msg", "")
            except Exception as e:  # noqa: BLE001
                log.warning("重试发送异常: %s", e)

    log.warning("应用机器人发送被拒: %s %s", code, msg)
    hint = _hint_of(_IM_HINTS, code)
    return False, f"飞书返回错误 {code}: {msg}" + (f"（{hint}）" if hint else "")


# ---------------------------------------------------------------------------
#  统一入口
# ---------------------------------------------------------------------------
def is_configured(creds):
    """判断推送凭证是否填全（用于保存/发送前校验）。"""
    creds = creds or {}
    if (creds.get("mode") or "app") == "webhook":
        return bool(normalize_webhook(creds.get("webhook")))
    return bool((creds.get("app_id") or "").strip()
                and (creds.get("app_secret") or "").strip()
                and (creds.get("receive_id") or "").strip())


def describe(creds):
    """日志用的一句话描述（脱敏）。"""
    creds = creds or {}
    if (creds.get("mode") or "app") == "webhook":
        return f"自定义机器人 {mask_webhook(creds.get('webhook'))}"
    return (f"应用机器人 app_id={mask_secret(creds.get('app_id'))} "
            f"→ {(creds.get('receive_id_type') or 'chat_id')}="
            f"{mask_secret(creds.get('receive_id'), 4)}")


def send_message(creds, title, lines=None, text=None):
    """统一推送入口：按 creds['mode'] 走应用机器人或自定义机器人。

    应用机器人失败（权限/接收方问题）时**不**降级到 webhook——两者凭证体系不同，
    但同一方式内 post 失败会降级纯文本重试。
    """
    creds = creds or {}
    mode = (creds.get("mode") or "app").strip()

    if mode == "webhook":
        if lines:
            ok, msg = send_feishu_post(creds.get("webhook"), creds.get("secret"),
                                       title, lines)
            if ok:
                return ok, msg
            log.info("富文本发送失败（%s），降级纯文本重试", msg)
            plain = "\n".join([title or ""] + list(lines))
            return send_feishu_text(creds.get("webhook"), creds.get("secret"), plain)
        return send_feishu_text(creds.get("webhook"), creds.get("secret"),
                                text or title or "")

    return send_app_message(creds.get("app_id"), creds.get("app_secret"),
                            creds.get("receive_id"),
                            creds.get("receive_id_type") or "chat_id",
                            title, lines=lines, text=text)


def test_message(creds):
    """测试连通性：发一条短消息。返回 (ok, 说明)。"""
    if not is_configured(creds):
        return False, "推送凭证没填全"
    return send_message(
        creds, "交易系统测试",
        lines=["交易系统测试 ✅", "飞书推送配置成功，选股日报将发送到这里。"])


# ---------------- 兼容旧接口 ----------------
def send_feishu(webhook, secret, title, lines=None, text=None):
    """旧接口（仅自定义机器人），保留给老调用点。"""
    return send_message({"mode": "webhook", "webhook": webhook, "secret": secret},
                        title, lines=lines, text=text)


def test_feishu(webhook, secret):
    """旧接口（仅自定义机器人）测试发送。"""
    return test_message({"mode": "webhook", "webhook": webhook, "secret": secret})
