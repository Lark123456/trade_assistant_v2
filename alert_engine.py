# -*- coding: utf-8 -*-
"""提醒引擎：行情轮询时检查提醒条件，返回待通知事件。

类型：
- price     : 价格  >= / <= 阈值
- percent   : 涨跌幅 >= / <= 阈值
- volume    : 成交量(手) >= / <= 阈值
- macd_cross: MACD 金叉/死叉（需要日K，由调用方传入指标后的 DataFrame）
- kdj       : KDJ 金叉/死叉
- rsi       : RSI 超买/超卖（threshold 为 80/20，operator >= 超买、<= 超卖）
- strategy_signal: 策略信号（v0.6.5，联动策略管理；params 存策略编号+版本+公式快照）
"""
import datetime
import logging

log = logging.getLogger("alerts")

COOLDOWN_SECONDS = 3600  # 同一提醒默认冷却时间（1小时）


def _cross_up(a, b):
    return bool(a[-1] > b[-1] and a[-2] <= b[-2])


def _cross_down(a, b):
    return bool(a[-1] < b[-1] and a[-2] >= b[-2])


def check_quote_alerts(alerts, quote, now=None):
    """对单只股票的一行行情执行提醒检查（price/percent/volume）。"""
    now = now or datetime.datetime.now()
    events = []
    for a in alerts:
        if not a.get("enabled"):
            continue
        if str(a["code"]) != str(quote["code"]):
            continue
        atype, op, thr = a["alert_type"], a["operator"], float(a["threshold"] or 0)
        params = a.get("params") or {}
        cooldown = float(params.get("cooldown", COOLDOWN_SECONDS))
        last = a.get("last_triggered_at")
        if last:
            try:
                last_dt = datetime.datetime.fromisoformat(last)
                if (now - last_dt).total_seconds() < cooldown:
                    continue
            except ValueError:
                pass
        value, text = None, ""
        if atype == "price":
            value = float(quote.get("price") or 0)
            text = f"价格 {value:.2f}"
        elif atype == "percent":
            value = float(quote.get("pct") or 0)
            text = f"涨跌幅 {value:.2f}%"
        elif atype == "volume":
            value = float(quote.get("vol") or 0)
            text = f"成交量 {value:.0f}手"
        if value is None:
            continue
        hit = (op == ">=" and value >= thr) or (op == "<=" and value <= thr) \
            or (op == ">" and value > thr) or (op == "<" and value < thr)
        if hit:
            name = a.get("name") or quote.get("name", "")
            events.append({
                "alert_id": a["id"], "code": a["code"], "name": name,
                "message": f"{name}({a['code']}) {text} {op} {thr:g} 触发提醒",
            })
    return events


def check_bar_alerts(alerts, code, df_indicators, now=None):
    """对带指标列的日K DataFrame 执行提醒检查（macd_cross/kdj/rsi）。"""
    now = now or datetime.datetime.now()
    events = []
    for a in alerts:
        if not a.get("enabled"):
            continue
        if str(a["code"]) != str(code):
            continue
        atype, op = a["alert_type"], a["operator"]
        params = a.get("params") or {}
        cooldown = float(params.get("cooldown", COOLDOWN_SECONDS))
        last = a.get("last_triggered_at")
        if last:
            try:
                last_dt = datetime.datetime.fromisoformat(last)
                if (now - last_dt).total_seconds() < cooldown:
                    continue
            except ValueError:
                pass
        if len(df_indicators) < 2:
            continue
        hit, message = False, ""
        if atype == "macd_cross":
            dif, dea = df_indicators["MACD_DIF"], df_indicators["MACD_DEA"]
            if op == "cross_up":
                hit, message = _cross_up(dif.to_numpy(), dea.to_numpy()), "MACD金叉"
            elif op == "cross_down":
                hit, message = _cross_down(dif.to_numpy(), dea.to_numpy()), "MACD死叉"
        elif atype == "kdj":
            k, d = df_indicators["KDJ_K"], df_indicators["KDJ_D"]
            if op == "cross_up":
                hit, message = _cross_up(k.to_numpy(), d.to_numpy()), "KDJ金叉"
            elif op == "cross_down":
                hit, message = _cross_down(k.to_numpy(), d.to_numpy()), "KDJ死叉"
        elif atype == "rsi":
            thr = float(a["threshold"] or 0)
            v = float(df_indicators["RSI6"].iloc[-1] or 0)
            if op == ">=" and v >= thr:
                hit, message = True, f"RSI6={v:.1f} 超买(>={thr:g})"
            elif op == "<=" and v <= thr:
                hit, message = True, f"RSI6={v:.1f} 超卖(<={thr:g})"
        elif atype == "strategy_signal":
            # 策略信号提醒（v0.6.5）：params 存创建时的公式快照 + 策略编号/版本
            formula = (params.get("formula") or "").strip()
            if formula and len(df_indicators) >= 30:
                try:
                    from indicators import formula_signal
                    sig, _labels = formula_signal(df_indicators, formula)
                    if sig:
                        ver = params.get("strategy_version")
                        hit, message = True, (
                            f"策略「{params.get('strategy_name', '')}」信号触发"
                            + (f"（v{ver}）" if ver else ""))
                except Exception as e:  # noqa: BLE001 公式异常不阻断其他提醒
                    log.debug("策略信号判定异常 %s: %s", a["code"], e)
        if hit:
            name = a.get("name") or ""
            events.append({
                "alert_id": a["id"], "code": code, "name": name,
                "message": f"{name}({code}) {message} 触发提醒",
            })
    return events


def describe_alert(a):
    """提醒的人类可读描述（用于界面列表）。"""
    tpl = {
        "price": "价格", "percent": "涨跌幅", "volume": "成交量",
        "macd_cross": "MACD", "kdj": "KDJ", "rsi": "RSI6",
        "strategy_signal": "策略信号",
    }
    if a["alert_type"] == "strategy_signal":
        p = a.get("params") or {}
        ver = p.get("strategy_version")
        return ("策略信号 · " + str(p.get("strategy_name", "?"))
                + (f"（v{ver}）" if ver else ""))
    op = {"cross_up": "金叉", "cross_down": "死叉", ">=": ">=",
          "<=": "<=", ">": ">", "<": "<"}.get(a["operator"], a["operator"])
    if a["alert_type"] in ("macd_cross", "kdj"):
        return f"{tpl.get(a['alert_type'], a['alert_type'])} {op}"
    if a["alert_type"] == "rsi":
        return f"{tpl.get('rsi')} {op} {a['threshold']:g}"
    return f"{tpl.get(a['alert_type'], a['alert_type'])} {op} {a['threshold']:g}"
