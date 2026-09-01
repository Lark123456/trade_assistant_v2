# -*- coding: utf-8 -*-
"""选股 → 通达信自动同步（v0.8.10）。

盘前/盘后全市场扫描（mode="batch"）每次扫完一个策略后，自动把命中代码同步到通达信
自定义板块，板块名 = `YYMMDD·<策略名>`（例 `260831·B1十张图`）。
- TDX 路径自动检测（Windows 常见如 E:\\TDX、C:\\new_tdx 等），可通过 settings `tdx.path` 覆盖
- 设置 `tdx.auto_sync` = "0" 可关闭
- TDX 不存在或路径无效时静默失败（仅返回 ok=False，不抛异常）

注意：TDX `.cfg` 记录里"板块名"字段只有 8 字节 GBK 硬限制（v0.8.2 实测确认），所以
TDX 里**显示**的板块名会被截到 8 字节——但 `.blk` 文件名和 `.cfg` 里 70 字节的 filename
字段都会存完整名，用户按"开头形似自动归为子板块"的逻辑仍能工作（共享 `YYMMDD·` 前缀）。
"""
from __future__ import annotations

import datetime
import logging
from typing import List, Optional, Tuple

log = logging.getLogger("auto_tdx_sync")

SETTING_AUTO = "tdx.auto_sync"   # "1" / "0"
SETTING_PATH = "tdx.path"          # 自定义 TDX 安装根目录


def mmdd(date: Optional[datetime.date] = None) -> str:
    """月日 无前导零(例 2026-08-31 → '831'),省 GBK 字节给策略名."""
    d = date or datetime.date.today()
    return f"{d.month}{d.day}"


def _gbk_truncate_to_bytes(s: str, max_bytes: int) -> str:
    """按 GBK 编码截断到 ≤ max_bytes 字节(避免截半个汉字)."""
    raw = s.encode("gbk", errors="ignore")[:max_bytes]
    return raw.decode("gbk", errors="ignore")


def make_block_name(strategy_name: str, date: Optional[datetime.date] = None) -> str:
    """组装板块名: MMDD + 策略名(总 GBK 字节 ≤ 8, 去掉分隔符省空间).

    v0.8.12: 由之前的 `YYMMDD·策略名` 改为 `MMDD+策略名`。
    - MMDD 占 3 字节 ASCII, 留 5 字节 GBK 给策略名 (汉字策略名约 2 个汉字 + 字母前缀)。
    - 总 8 字节 GBK (TDX cfg name 字段硬限制)。
    - 完整名仍写入 .blk 文件名 + cfg 的 70 字节 filename 字段 (用户按前缀分组仍能识别)。

    date 接受 datetime.date 对象 或 "YYYY-MM-DD" 字符串；传 None 或无效值时用今天。
    """
    # 防御性归一化: 字符串/None → datetime.date
    if date is None:
        date = datetime.date.today()
    elif isinstance(date, str):
        try:
            date = datetime.date.fromisoformat(date)
        except ValueError:
            date = datetime.date.today()
    name = "".join(c for c in (strategy_name or "").strip()
                    if c not in '\\/:*?"<>|').strip()
    if not name:
        name = "未命名"
    return mmdd(date) + _gbk_truncate_to_bytes(name, 8 - 3)


def _resolve_tdx_root(db, fallback: Optional[str] = None) -> Optional[str]:
    """从 settings 取 tdx.path；否则用默认探测；否则 None."""
    # 1. settings 覆盖
    try:
        path = db.get_setting(SETTING_PATH)
        if path and isinstance(path, str) and path.strip():
            return path.strip()
    except Exception:
        pass
    # 2. 探测常见路径
    from ui.tdx_sync_dialog import detect_tdx_root
    detected = detect_tdx_root()
    if detected:
        return detected
    # 3. fallback
    return fallback


def auto_sync_codes_to_tdx(db, strategy_name: str, codes: List[str],
                             date: Optional[datetime.date] = None
                             ) -> Tuple[bool, str]:
    """把一组股票代码同步到通达信板块（板块名 = YYMMDD·<策略名>）。

    返回 (ok, message)。失败不抛异常。
    """
    # 1. 开关检查
    try:
        flag = db.get_setting(SETTING_AUTO)
        if flag is not None and str(flag).strip() in ("0", "false", "False", "no", "off"):
            return False, "自动同步已关闭（settings: tdx.auto_sync）"
    except Exception:
        pass

    # 2. 空列表直接跳过
    clean_codes = [str(c).strip().zfill(6) for c in (codes or []) if str(c).strip()]
    if not clean_codes:
        return False, "无命中代码, 跳过同步"

    # 3. 解析 TDX 路径
    tdx_root = _resolve_tdx_root(db)
    if not tdx_root:
        return False, "未找到通达信安装路径（可设 settings: tdx.path）"

    # 4. 校验根目录确实是通达信安装目录（必须已存在 T0002/blocknew，
    #    否则 write_blk 的 makedirs 会在错误路径下静默造目录）
    try:
        from tdx_sync import TdxBlockSync
    except ImportError as e:  # noqa: BLE001
        return False, f"tdx_sync 模块不可用：{e}"
    if not TdxBlockSync.is_valid_root(tdx_root):
        return False, (f"通达信目录无效（缺少 T0002\\blocknew）：{tdx_root}"
                       f"；可在 settings 里设 tdx.path 指向正确的安装根目录")

    # 5. 导出
    try:
        # date 可能是 "YYYY-MM-DD" 字符串(扫描链路传入) 或 datetime.date 对象,
        # make_block_name 需要 .month/.day 属性. 解析失败时退回今天.
        if isinstance(date, str):
            try:
                date = datetime.date.fromisoformat(date)
            except ValueError:
                date = datetime.date.today()
        block_name = make_block_name(strategy_name, date)
        sync = TdxBlockSync(tdx_root)
        n, path = sync.export_block(clean_codes, block_name, overwrite=True)
        msg = (f"✅ 同步 {n} 只到通达信板块「{block_name}」\n"
               f"   路径: {path}\n   TDX 根目录: {tdx_root}")
        return True, msg
    except Exception as e:  # noqa: BLE001
        log.warning("TDX 自动同步失败: %s", e)
        return False, f"TDX 同步失败：{type(e).__name__}: {e}"
