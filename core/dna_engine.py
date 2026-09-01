# -*- coding: utf-8 -*-
"""DNA 特征工程 + 黄金模具库管理 + 多维加权 FastDTW 匹配引擎（二层精筛）。

## 数学规格（与用户确认的共识）
- 黄线（多空均线）YL = (MA(C,14) + MA(C,28) + MA(C,57) + MA(C,114)) / 4
- 白线（双重平滑趋势线）WL = EMA(EMA(C,10), 10)
- 三维特征（严格归一化到 [0,1]）：
  1. 价格几何起伏  NormPrice = (C - min(C)) / (max(C) - min(C) + eps)
  2. 成交量阶梯衰减 NormVol   = clip(VOL / MA20(VOL) / 3.0, 0, 1)
  3. 均线支撑偏离  NormBias  = clip((Bias + 0.1) / 0.2, 0, 1)，Bias = (C - YL) / YL
- 加权欧氏距离 weights = (0.5, 0.3, 0.2)，Sakoe-Chiba 窗宽 radius = 4
- 相似度 Score = 100 * exp(-3.5 * Distance / PathLength)

## 候选窗口（共识，v0.7.0）
- 末端锚定最新交易日（不滑动起点——避免选出"形态早已走完"的票）
- 默认按模具长度对齐；可配置固定 N 天
- 长度弹性扫描 [15, 40] 档位（默认开启），每档末端均锚定今天，取最高分
  （解决"真实形态周期 ≠ 模具长度"的漏网问题）
- 全市场直测使用快速模式：仅单档（模具长度）

## 关键实现点
- 指标必须在全量序列上计算（YL 的 MA114 需 114 天预热），
  特征矩阵也只对全量提取一次，各长度档位通过矩阵尾部切片获得（末端锚定）。
"""
import json
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("dna_engine")

# 长度弹性扫描档位（天），覆盖 15~40；模板长度会自动并入档位
DEFAULT_LENGTH_SCAN = [15, 18, 21, 24, 27, 30, 33, 36, 39]
MIN_WINDOW = 15       # 形态周期下限（过短的特征提取无意义）
MAX_WINDOW = 40       # 形态周期上限（过长失去"临近爆发"语义）

# 特征算法版本（模具口径标识，防止新旧特征混用比对）
# v1：维度3 = 黄线 YL 偏离度 ±10% 映射（强势股偏离过大时贴 1.0 天花板，回踩动作丢失）
# v2：维度3 = 动态支撑线偏离度（白线 WL 优先/跌破切黄线 YL），±25% 映射
FEATURE_VERSION = 2

# 维度3 参数：支撑线偏离容忍范围 ±25%（0.5 = 严丝合缝踩在支撑线上）
BIAS_SPAN = 0.25


def windowed_dtw(s1: np.ndarray, s2: np.ndarray, weights, radius=4):
    """窗口化 DTW（numpy 实现，v0.7.1 起替代 fastdtw 库，14x 加速）。

    与 fastdtw(radius=4) 语义一致：返回 (累积加权欧氏距离 Σd, 路径长度)。
    - 窗口（Sakoe-Chiba）：|i-j| < radius + |len(s1)-len(s2)|，
      等长/近等长时为严格 ±radius（规格的"±4 天弹性对齐"），
      长度差大时窗口自动放宽以保证路径可达
    - 距离 = 路径上所有点对加权欧氏距离之和（Score 公式中的 Distance）
    """
    s1 = np.asarray(s1, dtype=float)
    s2 = np.asarray(s2, dtype=float)
    w = np.asarray(weights, dtype=float)
    n, m = len(s1), len(s2)
    band = radius + abs(n - m)
    cost = np.full((n + 1, m + 1), np.inf)
    cost[0, 0] = 0.0
    for i in range(1, n + 1):
        j0 = max(1, i - band)
        j1 = min(m, i + band)
        if j0 > j1:
            continue
        diff = s1[i - 1][None, :] - s2[j0 - 1:j1]
        d = np.sqrt(np.einsum('ij,j->i', diff * diff, w))
        up = cost[i - 1, j0 - 1:j1]      # 对角 cost[i-1, j-1]
        up2 = cost[i - 1, j0:j1 + 1]     # 上 cost[i-1, j]
        prev = np.minimum(up, up2)
        # 行内从左到右传播（left 依赖本行左侧已算值，band 窄，逐列迭代开销小）
        for k in range(j1 - j0 + 1):
            best = prev[k]
            if k > 0:
                b = cost[i, j0 + k - 1]
                if b < best:
                    best = b
            cost[i, j0 + k] = d[k] + best
    # 回溯路径长度（分数公式需要 PathLength）
    i, j = n, m
    plen = 0
    while i > 0 or j > 0:
        plen += 1
        if i == 0:
            j -= 1
        elif j == 0:
            i -= 1
        else:
            k = int(np.argmin([cost[i - 1, j - 1], cost[i - 1, j], cost[i, j - 1]]))
            if k == 0:
                i, j = i - 1, j - 1
            elif k == 1:
                i, j = i - 1, j
            else:
                i, j = i, j - 1
    return float(cost[n, m]), plen


class DNAFeatureExtractor:
    """特征工程器：将原始行情提取为三维无量纲 DNA 特征矩阵。"""

    @staticmethod
    def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
        """计算通达信多空黄线 YL 与白线 WL（须在全量序列上计算，保证预热）。"""
        df = df.copy()
        c = df["close"]
        df["ma14"] = c.rolling(14, min_periods=1).mean()
        df["ma28"] = c.rolling(28, min_periods=1).mean()
        df["ma57"] = c.rolling(57, min_periods=1).mean()
        df["ma114"] = c.rolling(114, min_periods=1).mean()
        df["yl"] = (df["ma14"] + df["ma28"] + df["ma57"] + df["ma114"]) / 4
        df["wl"] = c.ewm(span=10, adjust=False).mean().ewm(span=10, adjust=False).mean()
        return df

    @staticmethod
    def extract_matrix(df: pd.DataFrame) -> np.ndarray:
        """生成 N x 3 的纯数学特征矩阵（无量纲 [0,1]，时间正序）。

        维度3（支撑线贴合度，v2）：动态支撑线——股价在白线 WL 上方运行时
        以白线为支撑（灵敏反映"回踩白线"动作；强势股远离黄线时不再贴 1.0 天花板），
        跌破白线后切换为黄线 YL 衡量（破位后看中期支撑是否完好）。
        0.5 = 严丝合缝踩在支撑线上；>0.5 在支撑线上方运行；<0.5 跌破支撑。
        映射范围 ±BIAS_SPAN（25%）。
        """
        if len(df) < 5:
            raise ValueError("行情数据长度不足以提取特征矩阵")

        c = df["close"].values.astype(float)
        v = df["vol"].values.astype(float)
        yl = df["yl"].values.astype(float)
        wl = df["wl"].values.astype(float)

        # 1. 价格波形 (0~1)
        p_min, p_max = np.min(c), np.max(c)
        norm_price = (c - p_min) / (p_max - p_min + 1e-8)

        # 2. 量能衰减 (0~1)
        v_ma20 = pd.Series(v).rolling(window=20, min_periods=1).mean().values
        norm_vol = np.clip((v / (v_ma20 + 1e-8)) / 3.0, 0, 1)

        # 3. 支撑线贴合度 (0~1)：动态支撑线（白线优先，破位切黄线）
        ref = np.where(c >= wl, wl, yl)
        bias = (c - ref) / (ref + 1e-8)
        norm_bias = np.clip((bias + BIAS_SPAN) / (2 * BIAS_SPAN), 0, 1)

        return np.column_stack([norm_price, norm_vol, norm_bias])


class TemplateManager:
    """模具库持久化管理器：双重管理 .npz 矩阵与 .json 元数据（硬性要求）。

    - golden_templates.npz  纯 numpy 二维特征矩阵（内部键 t0/t1/...，规避中文键风险）
    - templates_meta.json   每个模具的元信息（代码/名称/策略分类/起止日期/天数等）
    """

    def __init__(self, storage_dir=None):
        self.storage_dir = str(storage_dir or (Path(__file__).resolve().parent.parent / "dna_storage"))
        self.npz_path = os.path.join(self.storage_dir, "golden_templates.npz")
        self.meta_path = os.path.join(self.storage_dir, "templates_meta.json")
        os.makedirs(self.storage_dir, exist_ok=True)
        self.templates = {}      # name -> np.ndarray
        self.metadata = {}       # name -> dict
        self._key_map = {}       # name -> npz 键
        self.load_templates()

    # ------------------------------------------------------------ 持久化
    def save_template(self, name: str, matrix: np.ndarray, meta_info: dict):
        """保存（覆盖）一个模具：矩阵入 npz，元数据入 json。
        自动写入当前特征算法版本（_feature_version），防止新旧口径模具混用。"""
        name = str(name).strip()
        if not name:
            raise ValueError("模具名称不能为空")
        matrix = np.asarray(matrix, dtype=float)
        if matrix.ndim != 2 or matrix.shape[1] != 3:
            raise ValueError("模具矩阵必须为 N x 3")
        meta_info = dict(meta_info or {})
        meta_info["_feature_version"] = FEATURE_VERSION
        self.templates[name] = matrix
        self.metadata[name] = meta_info
        self._rewrite()
        log.info("模具已保存: %s (%d 天, feature_version=%d)",
                 name, len(matrix), FEATURE_VERSION)

    def delete_template(self, name: str):
        if name in self.templates:
            del self.templates[name]
            del self.metadata[name]
            self._rewrite()
            log.info("模具已删除: %s", name)

    def _rewrite(self):
        # npz 键使用安全索引键，name->key 映射写入 metadata 的 _npz_key 字段
        arrays = {}
        key_map = {}
        for i, name in enumerate(self.templates):
            key = f"t{i}"
            arrays[key] = self.templates[name]
            key_map[name] = key
            self.metadata[name] = dict(self.metadata.get(name) or {})
            self.metadata[name]["_npz_key"] = key
        self._key_map = key_map
        try:
            np.savez(self.npz_path, **arrays)
        except Exception as e:  # noqa: BLE001
            log.error("模具 npz 写入失败: %s", e)
            raise
        with open(self.meta_path, "w", encoding="utf-8") as f:
            json.dump(self.metadata, f, ensure_ascii=False, indent=2)

    def load_templates(self):
        self.templates = {}
        self.metadata = {}
        self._key_map = {}
        if os.path.exists(self.npz_path):
            try:
                data = np.load(self.npz_path, allow_pickle=False)
                key_map = {}
                if os.path.exists(self.meta_path):
                    with open(self.meta_path, "r", encoding="utf-8") as f:
                        self.metadata = json.load(f)
                    for name, meta in self.metadata.items():
                        k = meta.get("_npz_key")
                        if k and k in data.files:
                            key_map[name] = k
                            self.templates[name] = data[k]
                # 兼容无元数据/元数据缺失键的情况：按 npz 键顺序兜底
                if not self.templates:
                    for k in data.files:
                        self.templates.setdefault(f"t{len(self.templates)}", data[k])
                self._key_map = key_map
            except Exception as e:  # noqa: BLE001
                log.error("模具库加载失败: %s", e)
        elif os.path.exists(self.meta_path):
            with open(self.meta_path, "r", encoding="utf-8") as f:
                self.metadata = json.load(f)

    def list_templates(self):
        """[(name, meta), ...] 按保存顺序。"""
        return [(n, self.metadata.get(n, {})) for n in self.templates]


class DNAPatternMatcher:
    """多维加权 FastDTW 骨架相似度匹配引擎（二层精筛核心）。"""

    def __init__(self, template_manager: TemplateManager, radius=4,
                 weights=(0.5, 0.3, 0.2), decay_lambda=3.5,
                 window_mode="template_len", length_scan=True):
        """
        window_mode: "template_len" 按模具长度对齐 / "fixed" 固定 N 天
        length_scan: True=长度弹性扫描（默认档位+模具长度）/ False=单档
        """
        self.tm = template_manager
        self.radius = radius
        self.weights = np.array(weights, dtype=float)
        self.decay_lambda = decay_lambda
        self.window_mode = window_mode
        self.fixed_days = 25
        self.length_scan = length_scan

    # ------------------------------------------------------------ 距离/打分
    def _score(self, dist, path_len):
        return 100.0 * np.exp(-self.decay_lambda * dist / max(path_len, 1))

    # ------------------------------------------------------------ 长度档位
    def _candidate_lengths(self, template_len: int):
        """候选长度档位（天），全部末端锚定最新交易日。"""
        if self.window_mode == "fixed":
            return [self.fixed_days]
        base = [template_len]
        if self.length_scan:
            for L in DEFAULT_LENGTH_SCAN:
                if MIN_WINDOW <= L <= MAX_WINDOW and L not in base:
                    base.append(L)
        return sorted(base)

    # ------------------------------------------------------------ 单候选匹配
    def match_candidate(self, feature_matrix: np.ndarray,
                        template_matrix: np.ndarray,
                        dates=None) -> dict:
        """候选全量特征矩阵 vs 单个模具，各长度档位取最高分。

        feature_matrix: Nx3（全量，时间正序）
        template_matrix: Mx3（模具）
        dates: 可选日期列表（与 feature_matrix 等长），用于返回最佳区间
        返回 {score, best_len, best_start, best_end, all_scores}
        """
        lengths = self._candidate_lengths(len(template_matrix))
        best = None
        all_scores = {}
        for L in lengths:
            if L > len(feature_matrix):
                continue
            cand = feature_matrix[-L:]
            dist, path_len = windowed_dtw(cand, template_matrix, self.weights,
                                          self.radius)
            score = float(round(self._score(dist, path_len), 2))
            all_scores[L] = score
            if best is None or score > best[0]:
                best = (score, L)
        if best is None:
            return {"score": 0.0, "best_len": 0, "best_start": None,
                    "best_end": None, "all_scores": all_scores}
        score, best_len = best
        best_start = best_end = None
        if dates is not None and len(dates) >= best_len:
            best_end = str(dates[-1])
            best_start = str(dates[-best_len])
        return {"score": score, "best_len": best_len,
                "best_start": best_start, "best_end": best_end,
                "all_scores": all_scores}

    # ------------------------------------------------------------ 批量匹配
    def current_templates(self):
        """返回与当前特征算法版本匹配的模具 {name: matrix}。
        旧口径（feature_version != FEATURE_VERSION）模具不参与比对，防止特征混用。"""
        out = {}
        for name, matrix in self.tm.templates.items():
            ver = self.tm.metadata.get(name, {}).get("_feature_version")
            if ver != FEATURE_VERSION:
                log.warning("模具「%s」为旧版特征口径(feature_version=%s，当前=%s)，"
                            "已跳过；请删除后在模具制作器中重新制作",
                            name, ver, FEATURE_VERSION)
                continue
            out[name] = matrix
        return out

    def match_batch(self, candidates, progress=None, cancelled=None):
        """对候选列表逐票逐模具匹配，输出排行榜。

        candidates: [{"code","name","market","df"}...] df 为全量日K（含 date/close/vol）
        返回 [{"code","name","market","score","best_template","best_len",
               "best_start","best_end","all_templates","reason"}...] 按分数降序
        """
        templates = self.current_templates()
        if not templates:
            raise ValueError("模具库为空或均为旧版特征口径，请先在模具制作器中重新制作模具")
        results = []
        total = len(candidates)
        for i, cand in enumerate(candidates):
            if cancelled and cancelled():
                break
            if progress:
                progress(i, total, cand.get("name", ""))
            try:
                df = cand["df"]
                if df is None or len(df) < MIN_WINDOW + 5:
                    continue
                fdf = DNAFeatureExtractor.calculate_indicators(df)
                feat = DNAFeatureExtractor.extract_matrix(fdf)
                dates = fdf["date"].astype(str).tolist()
                best = None
                all_t = {}
                for t_name, t_matrix in templates.items():
                    r = self.match_candidate(feat, t_matrix, dates)
                    all_t[t_name] = r["score"]
                    if best is None or r["score"] > best["score"]:
                        best = {**r, "template": t_name}
                if best and best["score"] > 0:
                    results.append({
                        "code": cand.get("code"), "name": cand.get("name"),
                        "market": cand.get("market"),
                        "score": best["score"], "best_template": best["template"],
                        "best_len": best["best_len"],
                        "best_start": best["best_start"], "best_end": best["best_end"],
                        "all_templates": all_t,
                        "reason": f"DNA[{best['template']}] {best['score']}分 "
                                  f"({best['best_len']}天 {best.get('best_start')}~{best.get('best_end')})",
                    })
            except Exception as e:  # noqa: BLE001
                log.debug("DNA 匹配 %s 失败: %s", cand.get("code"), e)
        results.sort(key=lambda r: r["score"], reverse=True)
        return results
