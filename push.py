# -*- coding: utf-8 -*-
"""推送通知：Server酱（微信） / Bark（iOS）。配置存于 settings：push_channel / push_key。"""
import logging
import urllib.parse

import requests

log = logging.getLogger("push")

CHANNELS = {"none": "不推送", "serverchan": "Server酱(微信)", "bark": "Bark(iOS)"}


def send_push(channel, key, title, body):
    """发送推送，返回 (ok, 说明)。channel: none/serverchan/bark。"""
    if not channel or channel == "none" or not key:
        return False, "未配置推送"
    try:
        if channel == "serverchan":
            r = requests.post(
                f"https://sctapi.ftqq.com/{key.strip()}.send",
                data={"title": title[:32], "desp": body}, timeout=10)
            ok = r.status_code == 200 and r.json().get("code") == 0
            return ok, "Server酱" + ("成功" if ok else f"失败({r.text[:80]})")
        if channel == "bark":
            url = f"https://api.day.app/{key.strip()}/{urllib.parse.quote(title)}/{urllib.parse.quote(body)}"
            r = requests.get(url, timeout=10)
            ok = r.status_code == 200
            return ok, "Bark" + ("成功" if ok else f"失败({r.status_code})")
    except Exception as e:  # noqa: BLE001
        log.warning("推送失败: %s", e)
        return False, f"推送异常: {e}"
    return False, "未知渠道"


def test_push(channel, key):
    """发送测试推送。"""
    ok, msg = send_push(channel, key, "交易系统测试", "推送配置测试成功 ✅")
    return ok, msg
