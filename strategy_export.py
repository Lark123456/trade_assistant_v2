# -*- coding: utf-8 -*-
"""策略迁移：一键导出 / 导入（v0.10.5）。

导出成**一个 JSON 文件**，内容：
    strategies          全部策略（公式/条件/层级/父策略/通达信别名/说明书）
    strategy_versions   每个策略的历史版本
    dna_templates       DNA 模具库（npz 二进制 base64 + meta json）

**不包含**：行情缓存、扫描历史、观察池、交易记录，以及 settings 里的任何密钥
（飞书 App Secret / AI API Key 都不在导出范围内）。

导入时按 `code`（优先）或 `name` 匹配：已存在则更新、不存在则新增；
父策略引用按新旧 id 映射自动重写，因此可以安全迁移到**全新机器**（id 必然不同）。
"""
from __future__ import annotations

import base64
import datetime
import json
import logging
import os
import shutil

import config

log = logging.getLogger("strategy_export")

FORMAT = "trade_assistant.strategies"
VERSION = 1
DNA_DIR_NAME = "dna_storage"
NPZ_NAME = "golden_templates.npz"
META_NAME = "templates_meta.json"


# ---------------------------------------------------------------------------
#  路径
# ---------------------------------------------------------------------------
def dna_dir():
    return os.path.join(str(config.BASE_DIR), DNA_DIR_NAME)


# ---------------------------------------------------------------------------
#  组装 / 落盘 / 读取
# ---------------------------------------------------------------------------
def build_bundle(db, include_templates=True):
    """把当前策略库打包成可移植的 dict。"""
    strategies = db.export_strategies()
    versions = db.export_strategy_versions()
    bundle = {
        "format": FORMAT,
        "version": VERSION,
        "app": "trade_assistant",
        "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "strategies": strategies,
        "strategy_versions": versions,
    }
    tpl_count = 0
    if include_templates:
        tpl = export_templates()
        if tpl:
            bundle["dna_templates"] = tpl
            tpl_count = tpl.get("count") or 0
    bundle["counts"] = {
        "strategies": len(strategies),
        "strategy_versions": len(versions),
        "dna_templates": tpl_count,
    }
    return bundle


def save_bundle(bundle, path):
    """写 JSON（UTF-8，缩进 2，便于人工查看/版本管理）。"""
    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2)
    log.info("策略已导出: %s（%s）", path, bundle.get("counts"))
    return path


def load_bundle(path):
    """读取并校验导出文件，返回 bundle；格式不对抛 ValueError。"""
    with open(path, "r", encoding="utf-8") as f:
        bundle = json.load(f)
    if not isinstance(bundle, dict):
        raise ValueError("文件内容不是有效的策略备份（顶层不是对象）")
    if bundle.get("format") != FORMAT:
        raise ValueError(
            f"不是本系统的策略备份文件（format={bundle.get('format')!r}，"
            f"应为 {FORMAT!r}）")
    ver = bundle.get("version")
    if not isinstance(ver, int) or ver > VERSION:
        raise ValueError(f"备份文件版本 {ver} 高于当前程序支持的 {VERSION}，请升级程序")
    if not isinstance(bundle.get("strategies"), list):
        raise ValueError("备份文件缺少 strategies 列表")
    return bundle


def summarize(bundle):
    """给界面展示的一句话摘要。"""
    c = bundle.get("counts") or {}
    n_s = c.get("strategies", len(bundle.get("strategies") or []))
    n_v = c.get("strategy_versions", len(bundle.get("strategy_versions") or []))
    n_t = c.get("dna_templates")
    if n_t is None:
        n_t = (bundle.get("dna_templates") or {}).get("count", 0)
    bits = [f"策略 {n_s} 个", f"历史版本 {n_v} 条"]
    bits.append(f"DNA 模具 {n_t} 个" if n_t else "不含 DNA 模具")
    when = bundle.get("exported_at") or "未知时间"
    return f"{'，'.join(bits)}\n导出时间：{when}"


# ---------------------------------------------------------------------------
#  DNA 模具库
# ---------------------------------------------------------------------------
def export_templates():
    """把 dna_storage 里的模具库读出来（npz 转 base64）。无文件返回 None。"""
    d = dna_dir()
    npz = os.path.join(d, NPZ_NAME)
    if not os.path.exists(npz):
        return None
    try:
        with open(npz, "rb") as f:
            raw = f.read()
        meta = {}
        meta_path = os.path.join(d, META_NAME)
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        tpl = {
            "count": len(meta) if isinstance(meta, dict) else 0,
            "npz_b64": base64.b64encode(raw).decode("ascii"),
            "meta": meta,
        }
        log.info("DNA 模具已打包：%d 个，npz %d 字节", tpl["count"], len(raw))
        return tpl
    except Exception as e:  # noqa: BLE001
        log.warning("导出 DNA 模具失败: %s", e)
        return None


def import_templates(tpl, backup=True):
    """把导出的模具库写回 dna_storage（默认先把旧文件备份成 .bak-时间戳）。

    返回一句话说明。
    """
    if not tpl or not tpl.get("npz_b64"):
        return "未包含 DNA 模具，跳过"
    d = dna_dir()
    os.makedirs(d, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    npz = os.path.join(d, NPZ_NAME)
    meta_path = os.path.join(d, META_NAME)
    try:
        if backup:
            for p in (npz, meta_path):
                if os.path.exists(p):
                    shutil.copy2(p, f"{p}.bak-{stamp}")
        with open(npz, "wb") as f:
            f.write(base64.b64decode(tpl["npz_b64"]))
        if tpl.get("meta") is not None:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(tpl["meta"], f, ensure_ascii=False, indent=2)
        n = len(tpl.get("meta") or {})
        log.info("DNA 模具已导入：%d 个（旧文件备份后缀 .bak-%s）", n, stamp)
        return f"DNA 模具已导入 {n} 个" + (f"（旧文件已备份为 .bak-{stamp}）" if backup else "")
    except Exception as e:  # noqa: BLE001
        log.warning("导入 DNA 模具失败: %s", e)
        return f"DNA 模具导入失败：{e}"


# ---------------------------------------------------------------------------
#  应用
# ---------------------------------------------------------------------------
def apply_bundle(db, bundle, mode="merge", include_templates=True):
    """把 bundle 写进数据库（必要的话也恢复模具库）。返回报告 dict。"""
    stats = db.import_strategy_bundle(bundle, mode=mode)
    tpl_msg = "未导入 DNA 模具"
    if include_templates and bundle.get("dna_templates"):
        tpl_msg = import_templates(bundle["dna_templates"])
    return {"stats": stats, "templates": tpl_msg, "mode": mode}


def format_report(report):
    """把 apply_bundle 的结果转成给用户看的中文多行文本。"""
    st = report.get("stats") or {}
    mode = "覆盖导入" if report.get("mode") == "replace" else "合并导入"
    lines = [
        f"{mode}完成：",
        f"  新增策略        {st.get('added', 0)} 个",
        f"  更新策略        {st.get('updated', 0)} 个",
        f"  跳过            {st.get('skipped', 0)} 个",
        f"  补入历史版本    {st.get('versions_added', 0)} 条",
        f"  {report.get('templates') or ''}",
    ]
    if st.get("versions_skipped"):
        lines.append(f"  （另有 {st['versions_skipped']} 条版本属于已删除的策略，未导入）")
    errs = st.get("errors") or []
    if errs:
        lines.append("")
        lines.append("需要注意：")
        for e in errs[:8]:
            lines.append(f"  · {e}")
    return "\n".join(lines)
