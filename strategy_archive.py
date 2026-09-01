# -*- coding: utf-8 -*-
"""策略版本归档（v0.6.5）：每个策略按「编号」归集到一个文件夹，
公式/说明书每次变更自动写入一个版本快照文件，作为保险机制的最终底账。

目录结构：
    archive/strategies/S0001/v001_20260815_砖型图翻红.md
    archive/strategies/S0001/v002_20260816_砖型图翻红.md
    ...

编号不变 → 永远归集到同一文件夹；改名不影响归集；删除策略不删除归档。
"""
import datetime
import logging
import pathlib

import config

log = logging.getLogger("strategy_archive")

_FORBIDDEN = '\\/:*?"<>|'


def _safe_name(name):
    cleaned = "".join(c for c in (name or "策略") if c not in _FORBIDDEN).strip()
    return cleaned or "策略"


def archive_strategy_version(rec, version):
    """把策略的一个版本写入归档目录。

    rec 需含 code/name/kind/formula/description/scan_mode。
    返回文件路径；失败返回 None（归档失败不影响主流程，只记日志）。
    """
    try:
        code = rec.get("code") or "S0000"
        # 兼容 str / Path 两种 ARCHIVE_DIR（测试里可能覆盖为 str）
        folder = pathlib.Path(config.ARCHIVE_DIR) / code
        folder.mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now()
        fname = f"v{version:03d}_{now:%Y%m%d}_{_safe_name(rec.get('name', ''))}.md"
        path = folder / fname
        kind_label = "指标存档" if rec.get("kind") == "indicator" else "通达信公式"
        lines = [
            f"# 策略版本归档 {code} v{version}",
            "",
            f"- 策略编号：{code}（稳定不变，归集依据）",
            f"- 版本：v{version}",
            f"- 策略名称：{rec.get('name', '')}",
            f"- 类型：{kind_label}",
            f"- 扫描节奏：{rec.get('scan_mode', 'batch')}",
            f"- 归档时间：{now:%Y-%m-%d %H:%M:%S}",
            "",
            "## 逻辑说明书",
            "",
            (rec.get("description") or "").strip() or "（未填写说明书）",
            "",
            "## 公式原文",
            "",
            "```",
            (rec.get("formula") or "").strip(),
            "```",
            "",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)
    except Exception as e:  # noqa: BLE001 归档失败不影响主流程
        log.warning("策略版本归档失败 %s v%s: %s", rec.get("code"), version, e)
        return None
