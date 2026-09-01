# -*- coding: utf-8 -*-
"""技术指标计算 + 通达信公式解析器。

内置指标：MA/EMA/MACD/KDJ/RSI/BOLL/ATR/VOL_MA
公式解析：支持 C,O,H,L,V,AMOUNT 数据序列与常用函数
（MA,EMA,SMA,WMA,DMA,SLOPE,REF,HHV,LLV,CROSS,COUNT,BARSLAST,EXIST,
EVERY,FILTER,IF,BETWEEN,ABS,MAX,MIN,SUM,STD）及
运算符 + - * / > < >= <= = != AND OR NOT，可用于自定义指标/选股条件。

通达信脚本风格（v2）：
- 支持 ``变量:=表达式;`` 赋值
- 支持 ``输出:表达式;`` 命名输出（多输出返回 dict，最后一个输出作为信号）
- 支持 ``{...}`` 注释、分号分隔的多语句
- REF 的第二参数既可以是常数，也可以是序列（如 REF(MA(V,5), BARSLAST(金叉))）
"""
import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 常用技术指标（输入 OHLCV DataFrame，输出带指标列的 DataFrame）
# ---------------------------------------------------------------------------
def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """在 df 上追加常用指标列并返回新 DataFrame。"""
    out = df.copy()
    c = out["close"]
    h, l, v = out["high"], out["low"], out["vol"]

    for n in (5, 10, 20, 60):
        out[f"MA{n}"] = c.rolling(n).mean()

    out["EMA12"] = c.ewm(span=12, adjust=False).mean()
    out["EMA26"] = c.ewm(span=26, adjust=False).mean()
    out["MACD_DIF"] = out["EMA12"] - out["EMA26"]
    out["MACD_DEA"] = out["MACD_DIF"].ewm(span=9, adjust=False).mean()
    out["MACD"] = (out["MACD_DIF"] - out["MACD_DEA"]) * 2

    # KDJ（9,3,3）
    low9 = l.rolling(9).min()
    high9 = h.rolling(9).max()
    rsv = (c - low9) / (high9 - low9).replace(0, np.nan) * 100
    out["KDJ_K"] = rsv.ewm(com=2, adjust=False).mean()
    out["KDJ_D"] = out["KDJ_K"].ewm(com=2, adjust=False).mean()
    out["KDJ_J"] = 3 * out["KDJ_K"] - 2 * out["KDJ_D"]

    # RSI
    diff = c.diff()
    gain = diff.clip(lower=0)
    loss = (-diff).clip(lower=0)
    for n in (6, 12, 24):
        ag = gain.ewm(alpha=1 / n, adjust=False).mean()
        al = loss.ewm(alpha=1 / n, adjust=False).mean()
        out[f"RSI{n}"] = 100 - 100 / (1 + ag / al.replace(0, np.nan))

    # BOLL(20,2)
    mid = c.rolling(20).mean()
    std = c.rolling(20).std()
    out["BOLL_MID"] = mid
    out["BOLL_UPPER"] = mid + 2 * std
    out["BOLL_LOWER"] = mid - 2 * std

    # ATR(14)
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    out["ATR14"] = tr.ewm(alpha=1 / 14, adjust=False).mean()

    out["VOL_MA5"] = v.rolling(5).mean()
    out["VOL_MA10"] = v.rolling(10).mean()
    return out


def snapshot_indicators(df: pd.DataFrame) -> dict:
    """取最新一根K线的全部指标值（用于因子快照）。"""
    out = compute_indicators(df)
    row = out.iloc[-1]
    return {k: (None if pd.isna(v) else round(float(v), 4))
            for k, v in row.items() if k not in ("date", "open", "high", "low", "close", "vol", "amount")}


# ---------------------------------------------------------------------------
# 通达信公式解析器
# ---------------------------------------------------------------------------
_SERIES = {"C": "close", "CLOSE": "close", "O": "open", "OPEN": "open",
           "H": "high", "HIGH": "high", "L": "low", "LOW": "low",
           "V": "vol", "VOL": "vol", "AMOUNT": "amount"}

_FUNCS1 = {"ABS": np.abs, "HHV": None, "LLV": None, "MA": None, "EMA": None,
           "REF": None, "COUNT": None, "BARSLAST": None, "SUM": None, "STD": None}
_FUNCS2 = {"MAX": None, "MIN": None, "CROSS": None}

_TOK_RE = None  # 编译于首次使用


def strip_comments(expr: str) -> str:
    """去除通达信风格 { } 注释（嵌套的花括号按一层处理）。"""
    out = []
    depth = 0
    for ch in expr:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


class FormulaError(ValueError):
    pass


class _Token:
    __slots__ = ("kind", "value")

    def __init__(self, kind, value):
        self.kind = kind   # num / id / op
        self.value = value


def _tokenize(expr: str):
    import re
    global _TOK_RE
    if _TOK_RE is None:
        # 标识符支持中文（通达信变量名常用中文，如 白线/黄线/砖型图）
        _TOK_RE = re.compile(
            r"\s*(?:(\d+\.?\d*)|([A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*)"
            r"|(:=|<=|>=|==|!=|[+\-*/<>=(),;:]))")
    tokens, pos = [], 0
    while pos < len(expr):
        m = _TOK_RE.match(expr, pos)
        if not m:
            if expr[pos:].strip() == "":  # 只剩空白则正常结束
                break
            raise FormulaError(f"无法解析的位置 {pos}: {expr[pos:]}")
        num, ident, op = m.groups()
        if num:
            tokens.append(_Token("num", float(num)))
        elif ident:
            tokens.append(_Token("id", ident.upper()))
        else:
            tokens.append(_Token("op", op))
        pos = m.end()
    return tokens


def _split_statements(tokens):
    """按分号把 token 流拆成语句。"""
    stmts, cur = [], []
    for t in tokens:
        if t.kind == "op" and t.value == ";":
            if cur:
                stmts.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        stmts.append(cur)
    return stmts


def script_lines(expr: str):
    """把脚本拆成语句行，返回 [(名称或None, 是否输出, tokens), ...]。"""
    tokens = _tokenize(strip_comments(expr))
    lines = []
    for st in _split_statements(tokens):
        if (len(st) >= 3 and st[0].kind == "id" and st[1].kind == "op"
                and st[1].value in (":=", ":")):
            lines.append((st[0].value.upper(), st[1].value == ":", st[2:]))
        else:
            lines.append((None, False, st))
    return lines


class _Evaluator:
    def __init__(self, df: pd.DataFrame, env: dict = None):
        self.df = df
        self.env = env or {}
        self.n = len(df)
        self._pos = 0
        self._tokens = []

    @staticmethod
    def _series_to_array(series):
        return series.to_numpy(dtype=float)

    def _series(self, name):
        return self.df[name].to_numpy(dtype=float)

    def parse(self, expr):
        self._tokens = _tokenize(expr)
        self._pos = 0
        if not self._tokens:
            raise FormulaError("空公式")
        val = self._expr_or()
        if self._pos != len(self._tokens):
            raise FormulaError(f"多余的内容: {self._tokens[self._pos:][:5]}")
        return val

    # ------------------------------------------------------------ 语法
    def _peek(self):
        return self._tokens[self._pos] if self._pos < len(self._tokens) else None

    def _take(self, op=None):
        t = self._peek()
        if t is None or (op is not None and t.value != op):
            raise FormulaError(f"期望 '{op}'，得到 {t.value if t else '结尾'}")
        self._pos += 1
        return t

    def _expr_or(self):
        left = self._expr_and()
        while self._peek() and self._peek().value == "OR":
            self._pos += 1
            right = self._expr_and()
            left = (left | right) if isinstance(left, np.ndarray) or isinstance(right, np.ndarray) \
                else bool(left or right)
        return left

    def _expr_and(self):
        left = self._expr_not()
        while self._peek() and self._peek().value == "AND":
            self._pos += 1
            right = self._expr_not()
            left = (left & right) if isinstance(left, np.ndarray) or isinstance(right, np.ndarray) \
                else bool(left and right)
        return left

    def _expr_not(self):
        if self._peek() and self._peek().value == "NOT":
            self._pos += 1
            val = self._expr_not()
            return ~val if isinstance(val, np.ndarray) else not val
        return self._expr_cmp()

    def _expr_cmp(self):
        left = self._expr_add()
        t = self._peek()
        if t and t.kind == "op" and t.value in (">", "<", ">=", "<=", "=", "==", "!="):
            self._pos += 1
            right = self._expr_add()
            op = {"=": "=="}.get(t.value, t.value)
            if isinstance(left, np.ndarray) or isinstance(right, np.ndarray):
                import operator
                fn = {"==": operator.eq, "!=": operator.ne,
                      ">": operator.gt, "<": operator.lt,
                      ">=": operator.ge, "<=": operator.le}[op]
                with np.errstate(invalid="ignore"):
                    res = fn(left, right)
                return np.nan_to_num(res, nan=False)
            return {"==": left == right, "!=": left != right,
                    ">": left > right, "<": left < right,
                    ">=": left >= right, "<=": left <= right}[op]
        return left

    def _expr_add(self):
        left = self._expr_mul()
        while self._peek() and self._peek().value in ("+", "-"):
            op = self._take().value
            right = self._expr_mul()
            left = left + right if op == "+" else left - right
        return left

    def _expr_mul(self):
        left = self._expr_unary()
        while self._peek() and self._peek().value in ("*", "/"):
            op = self._take().value
            right = self._expr_unary()
            if op == "*":
                left = left * right
            else:
                with np.errstate(divide="ignore", invalid="ignore"):
                    left = np.divide(left, right) if isinstance(left, np.ndarray) or isinstance(right, np.ndarray) \
                        else (left / right if right != 0 else float("nan"))
        return left

    def _expr_unary(self):
        if self._peek() and self._peek().value == "-":
            self._pos += 1
            return -self._expr_unary()
        return self._primary()

    def _primary(self):
        t = self._take()
        if t.kind == "num":
            return t.value
        if t.kind == "op" and t.value == "(":
            val = self._expr_or()
            self._take(")")
            return val
        if t.kind == "id":
            if t.value in _SERIES:
                return self._series(_SERIES[t.value])
            if t.value in self.env:  # 脚本变量（:= 赋值）
                return self.env[t.value]
            if t.value in self.df.columns:  # 直接引用已计算的指标列
                return self._series(t.value)
            if self._peek() and self._peek().value == "(":
                return self._call(t.value)
            raise FormulaError(f"未知标识符: {t.value}")
        raise FormulaError(f"意外的记号: {t.value}")

    def _call(self, name):
        self._take("(")
        args = [self._expr_or()]
        while self._peek() and self._peek().value == ",":
            self._pos += 1
            args.append(self._expr_or())
        self._take(")")
        return self._apply(name, args)

    def _apply(self, name, args):
        n = len(args)
        if name == "CROSS":
            if n != 2:
                raise FormulaError("CROSS 需要两个参数")
            a, b = args
            a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
            prev_a = np.roll(a, 1); prev_a[0] = np.nan
            prev_b = np.roll(b, 1); prev_b[0] = np.nan
            with np.errstate(invalid="ignore"):
                return np.nan_to_num((a > b) & (prev_a <= prev_b), nan=False)
        if name in ("MAX", "MIN"):
            if n != 2:
                raise FormulaError(f"{name} 需要两个参数")
            fn = np.maximum if name == "MAX" else np.minimum
            return fn(np.asarray(args[0], dtype=float), np.asarray(args[1], dtype=float))
        if name in ("BARSLAST", "ABS"):
            if n != 1:
                raise FormulaError(f"{name} 需要一个参数")
            x = np.asarray(args[0], dtype=float)
            if name == "ABS":
                return np.abs(x)
            return _barslast(np.nan_to_num(x) > 0)
        # ------------------------- 三参数函数
        if name == "IF":
            if n != 3:
                raise FormulaError("IF 需要三个参数: IF(条件,真值,假值)")
            cond = np.nan_to_num(np.asarray(args[0], dtype=float)) > 0
            a = np.asarray(args[1], dtype=float)
            b = np.asarray(args[2], dtype=float)
            return np.where(cond, a, b)
        if name == "SMA":  # 通达信 SMA(X,N,M): Y=(M*X+(N-M)*Y')/N
            if n != 3:
                raise FormulaError("SMA 需要三个参数: SMA(X,N,M)")
            x = np.asarray(args[0], dtype=float)
            N, M = int(round(float(args[1]))), int(round(float(args[2])))
            if N <= 0 or M <= 0 or M > N:
                raise FormulaError("SMA 参数需满足 0 < M <= N")
            return _sma(x, N, M)
        if name == "BETWEEN":
            if n != 3:
                raise FormulaError("BETWEEN 需要三个参数: BETWEEN(X,下界,上界)")
            a = np.asarray(args[0], dtype=float)
            lo = np.asarray(args[1], dtype=float)
            hi = np.asarray(args[2], dtype=float)
            return (a >= lo) & (a <= hi)
        # ------------------------- 双参数函数
        if n != 2:
            raise FormulaError(f"{name} 参数个数不对: {name}(x,N)")
        x = np.asarray(args[0], dtype=float)
        if name == "DMA":  # DMA(X,A): Y=A*X+(1-A)*Y'
            a = float(args[1])
            if not (0 < a <= 1):
                raise FormulaError("DMA 系数需在 (0,1] 之间")
            return _dma(x, a)
        if name == "REF":  # 第二参数可为序列（如 BARSLAST 的结果）
            return _ref(x, args[1])
        N = int(round(float(args[1])))
        if N <= 0:
            raise FormulaError(f"{name} 周期必须为正整数")
        if name == "MA":
            return _rolling(x, N, "mean")
        if name == "EMA":
            return _ewm(x, N)
        if name == "WMA":  # 加权均线，最新权重最大
            return _wma(x, N)
        if name == "SLOPE":  # 线性回归斜率
            return _slope(x, N)
        if name == "HHV":
            return _rolling(x, N, "max")
        if name == "LLV":
            return _rolling(x, N, "min")
        if name == "SUM":
            return _rolling(x, N, "sum")
        if name == "STD":
            return _rolling(x, N, "std")
        if name == "COUNT":
            return _rolling(np.nan_to_num(x), N, "sum")
        cond_arr = np.nan_to_num(x) > 0
        if name == "EXIST":
            return _rolling(cond_arr.astype(float), N, "max") > 0
        if name == "EVERY":
            return _rolling(cond_arr.astype(float), N, "min") > 0
        if name == "FILTER":
            return _filter(cond_arr, N)
        raise FormulaError(f"不支持的函数: {name}")


def _rolling(x, n, how):
    s = pd.Series(x)
    return s.rolling(n).agg(how).to_numpy(dtype=float)


def _ewm(x, n):
    return pd.Series(x).ewm(span=n, adjust=False).mean().to_numpy(dtype=float)


def _sma(x, n, m):
    """通达信 SMA(X,N,M)：Y=(M*X+(N-M)*Y')/N，等价于 alpha=M/N 的指数平滑。"""
    return pd.Series(x).ewm(alpha=m / n, adjust=False).mean().to_numpy(dtype=float)


def _wma(x, n):
    """加权移动平均，窗口内权重 1..N（最新权重最大）。"""
    w = np.arange(1, n + 1, dtype=float)
    return pd.Series(x).rolling(n).apply(lambda v: float(np.dot(w, v) / w.sum()),
                                         raw=True).to_numpy(dtype=float)


def _dma(x, a):
    """DMA(X,A)：Y=A*X+(1-A)*Y'。"""
    return pd.Series(x).ewm(alpha=a, adjust=False).mean().to_numpy(dtype=float)


def _slope(x, n):
    """n 周期线性回归斜率。"""
    s = pd.Series(x)
    t = pd.Series(np.arange(len(x), dtype=float))
    cov = s.rolling(n).cov(t)
    var = t.rolling(n).var()
    return (cov / var).to_numpy(dtype=float)


def _filter(cond, n):
    """FILTER(条件,N)：条件为真发出信号后，屏蔽其后 N 个周期。"""
    out = np.zeros(len(cond), dtype=bool)
    suppress = 0
    for i in range(len(cond)):
        if suppress > 0:
            suppress -= 1
            continue
        if cond[i]:
            out[i] = True
            suppress = n
    return out


def _barslast(cond):
    """距离上一次条件为真的K线数（含当前），从未出现则为当前索引+1。"""
    idx = np.where(cond)[0]
    out = np.full(len(cond), np.nan)
    if len(idx) == 0:
        return np.arange(1, len(cond) + 1, dtype=float)
    last = -1
    for i in range(len(cond)):
        if cond[i]:
            last = i
        out[i] = i - last
    return out


def _ref(x, n_arg):
    """REF(X,N)：取 N 周期前的 X。N 可以是常数或序列。"""
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    arr = np.asarray(n_arg)
    if arr.ndim == 0:
        try:
            nv = float(arr)
        except (TypeError, ValueError):
            return out
        if np.isnan(nv) or nv < 0 or nv >= len(x):
            return out
        n0 = int(nv)
        out[n0:] = x[:-n0]
        return out
    # 序列回看：out[i] = x[i - n[i]]，越界/非法为 NaN
    narr = np.nan_to_num(arr.astype(float), nan=float(len(x)) + 1)
    for i in range(len(x)):
        ni = narr[i]
        if ni < 0 or ni >= len(x):
            continue
        j = i - int(ni)
        if 0 <= j < len(x):
            out[i] = x[j]
    return out


def evaluate_script(df: pd.DataFrame, expr: str):
    """执行整段通达信脚本，返回 (输出dict或None, 环境dict, 最后值, 语句行列表)。

    - 输出dict：所有 ``名称:表达式;`` 命名输出（顺序保持）；无命名输出时为 None
    - 环境dict：全部 ``:=`` 赋值与命名输出的最新数组
    - 语句行列表：[(名称或None, 是否输出, 值), ...]，供诊断逐条展示
    """
    env = {}
    outputs = {}
    lines = []
    last_val = None
    for name, is_output, st in script_lines(expr):
        ev = _Evaluator(df, env)
        ev._tokens = st
        ev._pos = 0
        val = ev._expr_or()
        if ev._pos != len(st):
            raise FormulaError(f"多余的内容: {st[ev._pos:][:5]}")
        if name is not None:
            env[name] = val
        lines.append((name, is_output, val))
        last_val = val
        if is_output:
            outputs[name] = val
    return outputs if outputs else None, env, last_val, lines


def evaluate_formula(df: pd.DataFrame, expr: str):
    """解析并求值公式：无命名输出返回整列结果；多输出返回 dict。"""
    outputs, _env, last, _lines = evaluate_script(df, expr)
    if outputs:
        return outputs
    return last


def _last_true(val):
    arr = np.asarray(val)
    if arr.ndim == 0:
        return bool(arr)
    return bool(np.nan_to_num(arr[-1], nan=0.0) > 0)


def _last_num(val):
    try:
        arr = np.asarray(val)
        if arr.ndim == 0:
            return float(arr)
        return float(np.nan_to_num(arr[-1], nan=0.0))
    except (TypeError, ValueError):
        return 0.0


def formula_signal(df: pd.DataFrame, expr: str):
    """判断公式在最新一根K线的信号，返回 (是否成立, 附加标注dict)。

    多输出公式：最后一个命名输出作为信号；其余命名输出以「名称: 数值」返回，
    供扫描结果标注（如 B2 的 完美/稍欠）。
    """
    outputs, _env, last, _lines = evaluate_script(df, expr)
    if outputs:
        keys = list(outputs)
        sig = _last_true(outputs[keys[-1]])
        labels = {k: _last_num(v) for k, v in outputs.items() if k != keys[-1]}
        return sig, labels
    return _last_true(last), {}


def formula_last_signal(df: pd.DataFrame, expr: str):
    """判断公式在最新一根K线是否成立（用于选股/提醒）。"""
    return formula_signal(df, expr)[0]


# 已知数据列（用于语法校验）
_KNOWN_COLS = ["open", "high", "low", "close", "vol", "amount",
               "MA5", "MA10", "MA20", "MA60", "EMA12", "EMA26",
               "MACD_DIF", "MACD_DEA", "MACD", "KDJ_K", "KDJ_D", "KDJ_J",
               "RSI6", "RSI12", "RSI24", "BOLL_MID", "BOLL_UPPER",
               "BOLL_LOWER", "ATR14", "VOL_MA5", "VOL_MA10"]


def check_formula(expr: str):
    """校验公式语法与函数支持情况，返回 (ok, 错误信息)。"""
    if not expr or not expr.strip():
        return False, "公式不能为空"
    n = 30
    dummy = pd.DataFrame({
        col: np.linspace(10, 20, n) + (np.arange(n) % 5) * 0.1
        for col in _KNOWN_COLS
    })
    try:
        evaluate_formula(dummy, expr)
        return True, ""
    except FormulaError as e:
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        return False, f"公式错误: {e}"


# 预置常用选股公式（可编辑）
PRESET_FORMULAS = {
    "MACD金叉": "CROSS(MACD_DIF, MACD_DEA)",
    "MACD死叉": "CROSS(MACD_DEA, MACD_DIF)",
    "MA5上穿MA10": "CROSS(MA(C,5), MA(C,10))",
    "放量1.5倍": "V > REF(V,1) * 1.5",
    "放量2倍": "V > REF(V,1) * 2",
    "突破20日高点": "C > REF(HHV(H,20),1)",
    "KDJ金叉": "CROSS(KDJ_K, KDJ_D)",
    "KDJ超卖": "KDJ_K < 20",
    "RSI超卖": "RSI6 < 20",
    "创60日新低": "L < LLV(L,60)",
    "连续3日放量": "EVERY(V > REF(V,1), 3)",
    "5日内MACD金叉": "EXIST(CROSS(MACD_DIF, MACD_DEA), 5)",
    "收盘价在MA20上下2%内": "BETWEEN(C, MA(C,20)*0.98, MA(C,20)*1.02)",
    "金叉后首次回调": "FILTER(CROSS(MA(C,5), MA(C,10)), 3) AND C <= MA(C,5)",
}
