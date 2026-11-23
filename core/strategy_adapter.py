# -*- coding: utf-8 -*-
"""策略适配层（v0.9.0）

将系统内所有的策略（内置预设 + 用户自定义）封装为标准可调用函数：

    strategy_func(df: DataFrame) -> Series[bool]

注册到 ``REGISTERED_STRATEGIES`` 字典供回测引擎与策略管理模块共同调用。

约定：
  - 输入 df 至少含 open/high/low/close/vol/amount 六列；可被 indicators.compute_indicators
    处理以获得 MA/MACD/KDJ/RSI 等辅助列；
  - 输出 Series[bool]，长度与 df 行数相同；
  - 信号成立的索引即为「决策日 T」，回测引擎将以次日开盘价 Open_{t+1} 买入。

本模块**不引入数据库依赖**，确保回测、UI、文档、单元测试场景下都能直接导入。
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

log = logging.getLogger("strategy_adapter")

# 缓存 indicators 导入
_indicators = None


def _indicators_module():
    """懒加载 indicators 模块（避免循环导入）。"""
    global _indicators
    if _indicators is None:
        try:
            from indicators import (compute_indicators, evaluate_formula,
                                    formula_signal, _last_true)
            _indicators = {
                "compute_indicators": compute_indicators,
                "evaluate_formula": evaluate_formula,
                "formula_signal": formula_signal,
                "_last_true": _last_true,
            }
        except Exception as e:  # noqa: BLE001
            log.warning("indicators 导入失败: %s", e)
            _indicators = {}
    return _indicators


# --------------------------------------------------------------------------
# 工具函数：构造「全列均为 False」的 Series（出错时兜底）
# --------------------------------------------------------------------------
def _false_series(df: pd.DataFrame) -> pd.Series:
    return pd.Series(False, index=df.index)


# --------------------------------------------------------------------------
# 内置预设策略（与 strategy_presets.PRESET_STRATEGIES 一一对应）
# --------------------------------------------------------------------------
def _ensure_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """确保 df 含有 MA/MACD/KDJ 等辅助列（按需追加）。"""
    ind = _indicators_module()
    fn = ind.get("compute_indicators") if ind else None
    if fn is None:
        return df
    needed = ("MA5", "MA10", "MA20", "MA60",
              "EMA12", "EMA26", "MACD_DIF", "MACD_DEA", "MACD",
              "KDJ_K", "KDJ_D", "KDJ_J")
    if all(c in df.columns for c in needed):
        return df
    try:
        return fn(df)
    except Exception as e:  # noqa: BLE001
        log.debug("compute_indicators 失败: %s", e)
        return df


def _formula_func(formula: str, name: str = "formula") -> Callable[[pd.DataFrame], pd.Series]:
    """封装一条通达信公式为可调用函数。

    输出 Series[bool]：对每根 K 线调用 evaluate_formula 截断到该日的结果。
    """
    ind = _indicators_module()
    fn = ind.get("evaluate_formula") if ind else None
    if fn is None:
        return lambda df: _false_series(df)

    def _call(df: pd.DataFrame) -> pd.Series:
        if df is None or df.empty or not formula.strip():
            return _false_series(df)
        df2 = _ensure_indicators(df.copy())
        n = len(df2)
        out = np.zeros(n, dtype=bool)
        try:
            # 逐日截断：对每个 i，把 df 截到 [0..i] 然后求值
            # 为优化性能，按指标列已算好的情况下，只需要一次 evaluate_formula 取整列
            res = fn(df2, formula)
            arr = np.asarray(res)
            if arr.ndim == 0:
                out[:] = bool(arr)
            else:
                arr = arr.astype(bool) if arr.dtype != bool else arr
                if arr.shape[0] == n:
                    out = arr.copy()
        except Exception as e:  # noqa: BLE001
            log.debug("公式 %s 求值失败: %s", name, e)
            return pd.Series(out, index=df.index)
        return pd.Series(out, index=df.index)

    return _call


# 内置策略函数表（key=策略名，value=callable(df) -> Series[bool]）
_BUILTIN_STRATEGY_FUNCS: Dict[str, Callable[[pd.DataFrame], pd.Series]] = {}


def _register_builtins():
    """从 strategy_presets.PRESET_STRATEGIES 注册所有公式类策略。"""
    global _BUILTIN_STRATEGY_FUNCS
    try:
        from strategy_presets import PRESET_STRATEGIES
    except Exception as e:  # noqa: BLE001
        log.warning("strategy_presets 导入失败: %s", e)
        return

    funcs: Dict[str, Callable[[pd.DataFrame], pd.Series]] = {}
    for p in PRESET_STRATEGIES:
        name = p.get("name", "")
        formula = (p.get("formula") or "").strip()
        if not name or not formula:
            continue
        kind = p.get("kind", "formula")
        if kind != "formula":
            continue
        funcs[name] = _formula_func(formula, name=name)
    _BUILTIN_STRATEGY_FUNCS = funcs


# --------------------------------------------------------------------------
# 注册表：name -> callable(df) -> Series[bool]
# 任何外部模块都可以通过 ``REGISTERED_STRATEGIES`` 拿到所有可调用策略。
# --------------------------------------------------------------------------
REGISTERED_STRATEGIES: Dict[str, Callable[[pd.DataFrame], pd.Series]] = {}


def refresh_registry(extra: Optional[Dict[str, Callable[[pd.DataFrame], pd.Series]]] = None,
                     replace: bool = False) -> Dict[str, Callable[[pd.DataFrame], pd.Series]]:
    """刷新注册表。

    extra: 额外要注册的策略（key 覆盖时优先级更高）
    replace: True 时清空所有内置条目后再注册；默认 False（合并）
    """
    if not _BUILTIN_STRATEGY_FUNCS:
        _register_builtins()
    if replace:
        REGISTERED_STRATEGIES.clear()
    REGISTERED_STRATEGIES.update(_BUILTIN_STRATEGY_FUNCS)
    if extra:
        REGISTERED_STRATEGIES.update(extra)
    return REGISTERED_STRATEGIES


def register_strategy(name: str,
                      func: Callable[[pd.DataFrame], pd.Series],
                      override: bool = False) -> None:
    """注册一条策略（供 UI / 自定义策略使用）。

    override=False 时同名策略忽略（保护内置）；override=True 时覆盖。
    """
    if not name:
        return
    if name in REGISTERED_STRATEGIES and not override:
        return
    REGISTERED_STRATEGIES[name] = func


def register_from_formula(name: str, formula: str) -> None:
    """把一条通达信公式字符串注册为可调用策略（同名覆盖）。"""
    if not name or not formula or not formula.strip():
        return
    REGISTERED_STRATEGIES[name] = _formula_func(formula, name=name)


def unregister_strategy(name: str) -> bool:
    """移除一条策略（仅移除注册表中可调用函数，不影响数据库）。"""
    return REGISTERED_STRATEGIES.pop(name, None) is not None


def get_strategy_func(name: str) -> Optional[Callable[[pd.DataFrame], pd.Series]]:
    """按名称获取策略可调用函数；找不到时返回 None。"""
    if not REGISTERED_STRATEGIES:
        refresh_registry()
    return REGISTERED_STRATEGIES.get(name)


def list_strategy_names() -> List[str]:
    """列出当前所有已注册的策略名。"""
    if not REGISTERED_STRATEGIES:
        refresh_registry()
    return sorted(REGISTERED_STRATEGIES.keys())


# --------------------------------------------------------------------------
# 辅助：把数据库中的 strategy 字典（formula 字段）转为可调用函数
# --------------------------------------------------------------------------
def build_func_from_db_row(row: Dict[str, Any]) -> Optional[Callable[[pd.DataFrame], pd.Series]]:
    """根据数据库中的策略记录构造可调用函数。

    - formula 类：直接用通达信公式解析
    - dna 类：返回 None（DNA 策略无法用纯 K 线信号表达，暂不在回测引擎支持）
    """
    if not row:
        return None
    kind = row.get("kind", "formula")
    if kind == "dna":
        return None
    name = row.get("name", "db_strategy")
    formula = (row.get("formula") or "").strip()
    if not formula:
        return None
    return _formula_func(formula, name=name)


def register_from_db_rows(rows: Iterable[Dict[str, Any]],
                          override: bool = False) -> int:
    """把数据库里的多条策略记录批量注册到 REGISTERED_STRATEGIES。

    返回成功注册的条数。
    """
    n = 0
    for r in rows:
        name = r.get("name", "")
        if not name:
            continue
        if name in REGISTERED_STRATEGIES and not override:
            continue
        func = build_func_from_db_row(r)
        if func is None:
            continue
        REGISTERED_STRATEGIES[name] = func
        n += 1
    return n


# --------------------------------------------------------------------------
# 模块导入即初始化（延迟刷新，避免在 import 时跑公式校验）
# --------------------------------------------------------------------------
def ensure_initialized() -> None:
    """确保注册表已加载内置策略（一般无需手动调用）。"""
    if not REGISTERED_STRATEGIES:
        refresh_registry()


# 模块加载即注册内置策略
ensure_initialized()
