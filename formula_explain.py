# -*- coding: utf-8 -*-
"""公式 → 中文释义：把通达信脚本逐条翻译成人话（保险机制第一层）。

用于策略说明书与单票诊断：让不懂代码的用户核对「机器实现 == 我的本意」。
"""
import numpy as np
import pandas as pd

from indicators import FormulaError, script_lines

# 数据序列 → 中文
_SERIES_CN = {
    "C": "收盘价", "CLOSE": "收盘价", "O": "开盘价", "OPEN": "开盘价",
    "H": "最高价", "HIGH": "最高价", "L": "最低价", "LOW": "最低价",
    "V": "成交量", "VOL": "成交量", "AMOUNT": "成交额",
}

# 内置指标列 → 中文
_COL_CN = {
    "MA5": "5日均线", "MA10": "10日均线", "MA20": "20日均线", "MA60": "60日均线",
    "EMA12": "12日指数均线", "EMA26": "26日指数均线",
    "MACD_DIF": "MACD的DIF", "MACD_DEA": "MACD的DEA", "MACD": "MACD柱",
    "KDJ_K": "KDJ的K值", "KDJ_D": "KDJ的D值", "KDJ_J": "KDJ的J值",
    "RSI6": "RSI(6)", "RSI12": "RSI(12)", "RSI24": "RSI(24)",
    "BOLL_MID": "布林中轨", "BOLL_UPPER": "布林上轨", "BOLL_LOWER": "布林下轨",
    "ATR14": "ATR(14)", "VOL_MA5": "5日均量", "VOL_MA10": "10日均量",
}

_CMP_CN = {">": "大于", "<": "小于", ">=": "大于等于", "<=": "小于等于",
           "=": "等于", "==": "等于", "!=": "不等于"}


def _num_text(v):
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


class _Explainer:
    """递归下降渲染器：token 流 → 中文描述。"""

    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self):
        t = self.peek()
        if t is None:
            raise FormulaError("表达式不完整")
        self.pos += 1
        return t

    def expr_or(self):
        left = self.expr_and()
        while self.peek() and self.peek().value == "OR":
            self.pos += 1
            right = self.expr_and()
            left = f"({left} 或 {right})"
        return left

    def expr_and(self):
        left = self.expr_not()
        while self.peek() and self.peek().value == "AND":
            self.pos += 1
            right = self.expr_not()
            left = f"({left} 且 {right})"
        return left

    def expr_not(self):
        if self.peek() and self.peek().value == "NOT":
            self.pos += 1
            return f"(不满足：{self.expr_not()})"
        return self.expr_cmp()

    def expr_cmp(self):
        left = self.expr_add()
        t = self.peek()
        if t and t.kind == "op" and t.value in _CMP_CN:
            self.pos += 1
            right = self.expr_add()
            return f"{left} {_CMP_CN[t.value]} {right}"
        return left

    def expr_add(self):
        left = self.expr_mul()
        while self.peek() and self.peek().value in ("+", "-"):
            op = self.take().value
            right = self.expr_mul()
            left = f"({left} {'加' if op == '+' else '减'} {right})"
        return left

    def expr_mul(self):
        left = self.expr_unary()
        while self.peek() and self.peek().value in ("*", "/"):
            op = self.take().value
            right = self.expr_unary()
            left = f"({left} {'乘' if op == '*' else '除以'} {right})"
        return left

    def expr_unary(self):
        if self.peek() and self.peek().value == "-":
            self.pos += 1
            return f"(负 {self.expr_unary()})"
        return self.primary()

    def primary(self):
        t = self.take()
        if t.kind == "num":
            return _num_text(t.value)
        if t.kind == "op" and t.value == "(":
            val = self.expr_or()
            self.take()
            return val
        if t.kind == "id":
            name = t.value
            if name in _SERIES_CN:
                return _SERIES_CN[name]
            if name in _COL_CN:
                return _COL_CN[name]
            if self.peek() and self.peek().value == "(":
                return self.call(name)
            return name  # 脚本变量名保留原文（如 白线/昨天B1）
        raise FormulaError(f"无法释义的记号: {t.value}")

    def call(self, name):
        self.take()  # '('
        args = [self.expr_or()]
        while self.peek() and self.peek().value == ",":
            self.pos += 1
            args.append(self.expr_or())
        self.take()  # ')'
        return self._render_call(name, args)

    def _render_call(self, name, args):
        def need(n):
            if len(args) != n:
                raise FormulaError(f"{name} 需要 {n} 个参数")
        if name == "MA":
            need(2)
            return f"{args[0]}的{args[1]}日均线"
        if name == "EMA":
            need(2)
            return f"{args[0]}的{args[1]}日指数平滑均线"
        if name == "SMA":
            need(3)
            return f"{args[0]}的SMA平滑(周期{args[1]},权重{args[2]})"
        if name == "WMA":
            need(2)
            return f"{args[0]}的{args[1]}日加权均线"
        if name == "DMA":
            need(2)
            return f"{args[0]}的动态均线(系数{args[1]})"
        if name == "SLOPE":
            need(2)
            return f"{args[0]}的{args[1]}周期斜率"
        if name == "REF":
            need(2)
            return f"{args[1]}周期前的{args[0]}"
        if name == "HHV":
            need(2)
            return f"{args[0]}的{args[1]}周期最高值"
        if name == "LLV":
            need(2)
            return f"{args[0]}的{args[1]}周期最低值"
        if name == "SUM":
            need(2)
            return f"{args[0]}的{args[1]}周期累计和"
        if name == "STD":
            need(2)
            return f"{args[0]}的{args[1]}周期标准差"
        if name == "ABS":
            need(1)
            return f"{args[0]}的绝对值"
        if name == "MAX":
            need(2)
            return f"{args[0]}与{args[1]}的较大值"
        if name == "MIN":
            need(2)
            return f"{args[0]}与{args[1]}的较小值"
        if name == "CROSS":
            need(2)
            return f"{args[0]}上穿{args[1]}"
        if name == "COUNT":
            need(2)
            return f"最近{args[1]}周期内「{args[0]}」成立的天数"
        if name == "BARSLAST":
            need(1)
            return f"距离上一次「{args[0]}」成立的周期数"
        if name == "EXIST":
            need(2)
            return f"最近{args[1]}周期内存在「{args[0]}」"
        if name == "EVERY":
            need(2)
            return f"最近{args[1]}周期内始终满足「{args[0]}」"
        if name == "FILTER":
            need(2)
            return f"「{args[0]}」触发后的{args[1]}周期内不再重复"
        if name == "IF":
            need(3)
            return f"若{args[0]}，则为{args[1]}，否则为{args[2]}"
        if name == "BETWEEN":
            need(3)
            return f"{args[0]}介于{args[1]}与{args[2]}之间"
        return f"{name}({', '.join(args)})"


def explain_line(tokens) -> str:
    return _Explainer(tokens).expr_or()


def explain_formula(expr: str):
    """逐条翻译公式，返回 [{"name", "output", "text"}, ...]。"""
    out = []
    for name, is_output, tokens in script_lines(expr):
        try:
            text = explain_line(tokens)
        except FormulaError as e:
            text = f"（无法释义：{e}）"
        out.append({"name": name, "output": is_output, "text": text})
    return out


def format_explain_lines(expr: str) -> str:
    """把释义行拼成可读文本。"""
    rows = []
    for item in explain_formula(expr):
        head = f"{item['name']}（信号）" if item["output"] else \
            (item["name"] if item["name"] else "表达式")
        rows.append(f"{head} = {item['text']}")
    return "\n".join(rows)


# 供单票诊断使用的取值展示
def format_value(val) -> str:
    """把脚本变量最新值格式化成展示文本。"""
    arr = np.asarray(val)
    if arr.ndim == 0:
        v = float(arr)
    else:
        try:
            v = float(np.nan_to_num(arr[-1], nan=np.nan))
        except (TypeError, ValueError):
            v = np.nan
    if np.isnan(v):
        return "数据不足(空)"
    if abs(v) >= 1000 or (v != 0 and abs(v) < 0.001):
        return f"{v:.4g}"
    return f"{v:.3f}".rstrip("0").rstrip(".")


def is_bool_array(val) -> bool:
    arr = np.asarray(val)
    return arr.dtype == bool


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    demo = ("白线:=EMA(EMA(C,10),10);"
            "黄线:=(MA(C,14)+MA(C,28)+MA(C,57)+MA(C,114))/4;"
            "趋势好:=白线>黄线;"
            "昨天B1:=REF(KDJ_J,1)<13;"
            "完美:B2达标 AND H=MAX(C,O);")
    print(format_explain_lines(demo))
