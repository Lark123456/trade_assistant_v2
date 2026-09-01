# -*- coding: utf-8 -*-
"""选股 → 通达信自动同步（v0.8.10 / v0.8.13）。

盘前/盘后全市场扫描（mode="batch"）每次扫完一个策略后，自动把命中代码同步到通达信
自定义板块，板块名 = `MMDD<策略名截断>`（例 `91B1海`，TDX cfg 8字节 GBK 硬限制）。
- TDX 路径自动检测（Windows 常见如 E:\\TDX、C:\\new_tdx 等），可通过 settings `tdx.path` 覆盖
- 设置 `tdx.auto_sync` = "0" 可关闭
- TDX 不存在或路径无效时静默失败（仅返回 ok=False，不抛异常）

v0.8.13：.blk 文件名采用完整日期格式（`2026-9-1·B1海选.blk`，70 字节 cfg filename 字段同样存完整名），
TDX 板块管理里看到的板块名依然是8字节短名（cfg name 字段硬限制），但打开 T0002\blocknew 文件夹
按文件名字母序排列时，同一天扫描的板块会自然聚到一起（共享 `YYYY-M-D·` 前缀）。
- TDX 自定义板块是平铺结构，没有"按前缀自动归集到子菜单"的官方机制，文件名前缀排序是
  唯一能让用户感知到日期归类的折中方案。
"""
from __future__ import annotations

import datetime
import logging
from typing import List, Optional, Tuple

log = logging.getLogger("auto_tdx_sync")

SETTING_AUTO = "tdx.auto_sync"   # "1" / "0"
SETTING_PATH = "tdx.path"          # 自定义 TDX 安装根目录
NAME_FIELD_LIMIT = 8              # cfg name 字段最大 GBK 字节数 (TDX 硬限制)


def mmdd(date: Optional[datetime.date] = None) -> str:
    """月日 无前导零(例 2026-08-31 → '831'),省 GBK 字节给策略名."""
    d = date or datetime.date.today()
    return f"{d.month}{d.day}"


def _gbk_truncate_to_bytes(s: str, max_bytes: int) -> str:
    """按 GBK 编码截断到 ≤ max_bytes 字节(避免截半个汉字)."""
    raw = s.encode("gbk", errors="ignore")[:max_bytes]
    return raw.decode("gbk", errors="ignore")


def make_block_name(strategy_name: str, date: Optional[datetime.date] = None,
                   alias: Optional[str] = None) -> str:
    """组装**显示在 TDX 板块管理里的板块名**（cfg name 字段 8 字节 GBK 硬限制）。

    v0.8.12: 由之前的 `YYMMDD·策略名` 改为 `MMDD+策略名`。
    - MMDD 占 3 字节 ASCII, 留 5 字节 GBK 给策略名 (汉字策略名约 2 个汉字 + 字母前缀)。
    - 总 8 字节 GBK (TDX cfg name 字段硬限制)。
    - .blk 文件名 和 cfg filename 字段（70字节）用更完整格式 make_block_filename()。

    v0.8.15: 支持 alias 参数 — 若传 alias (用户为 TDX 自定义的短名, ≤8字节),
    直接用它做 cfg name 字段, 不再截断 strategy_name. 这样长策略名也能完整显示.

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
    # v0.8.15: 优先用 alias (用户自定义短名). 仅 GBK 安全截断到 8 字节.
    if alias:
        alias = "".join(c for c in alias.strip()
                        if c not in '\\/:*?"<>|').strip()
        if alias:
            return _gbk_truncate_to_bytes(alias, NAME_FIELD_LIMIT)
    name = "".join(c for c in (strategy_name or "").strip()
                    if c not in '\\/:*?"<>|').strip()
    if not name:
        name = "未命名"
    return mmdd(date) + _gbk_truncate_to_bytes(name, 8 - 3)


def make_block_filename(strategy_name: str, date: Optional[datetime.date] = None) -> str:
    """组装**.blk 文件名 / cfg filename 字段**：完整日期 + 中点 + 策略名(70 字节内)。

    例: 2026-9-1·B1海选
    - YYYY-M-D 前缀(无前导零)便于文件名字母序排列时同日期自然聚合
    - · 分隔符(v0.8.13 引入, 比纯 MMDD 短前缀辨识度高)
    - 总长受 cfg filename 70 字节 GBK 限制, 长策略名会被 GBK 安全截断
    """
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
    date_prefix = f"{date.year}-{date.month}-{date.day}"
    # 70 字节预算: 日期前缀 + · + 策略名
    raw = date_prefix + "·" + name
    return _gbk_truncate_to_bytes(raw, 70)


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
                             date: Optional[datetime.date] = None,
                             tdx_alias: Optional[str] = None
                             ) -> Tuple[bool, str]:
    """把一组股票代码同步到通达信板块.

    - cfg name 字段(8字节 GBK)用 tdx_alias(优先) 或 strategy_name(被截断)
    - cfg filename + .blk 文件名 用完整日期·策略名(70字节内)
    - tdx_alias: db.strategies.tdx_alias 字段(用户自定义短名). 不传则自动查 db

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
        # make_block_name/make_block_filename 需要 .month/.day 属性. 解析失败时退回今天.
        if isinstance(date, str):
            try:
                date = datetime.date.fromisoformat(date)
            except ValueError:
                date = datetime.date.today()
        # v0.8.15: 优先用 tdx_alias (用户为 TDX 板块管理自定义的短名) 做 cfg name;
        # 自动从 db 查 (按 strategy_name 匹配 name 列).
        if tdx_alias is None:
            try:
                row = db._conn.execute(
                    "SELECT tdx_alias FROM strategies WHERE name=? LIMIT 1",
                    (strategy_name,)).fetchone()
                tdx_alias = (row["tdx_alias"] if row and row["tdx_alias"] else None)
            except Exception:
                tdx_alias = None
        # v0.8.13: TDX cfg name 字段(8 字节 GBK 硬限制)用短名, .blk 文件名/cfg filename(70 字节)
        # 用完整日期格式, 便于文件夹按字母序排列时同日期板块自然聚合
        short_name = make_block_name(strategy_name, date, alias=tdx_alias)  # TDX 板块管理里显示
        filename = make_block_filename(strategy_name, date)  # .blk 文件名 + cfg filename
        sync = TdxBlockSync(tdx_root)
        n, path = sync.export_block(clean_codes, filename, overwrite=True)
        msg = (f"✅ 同步 {n} 只到通达信板块「{short_name}」\n"
               f"   路径: {path}\n   TDX 根目录: {tdx_root}")
        return True, msg
    except Exception as e:  # noqa: BLE001
        log.warning("TDX 自动同步失败: %s", e)
        return False, f"TDX 同步失败：{type(e).__name__}: {e}"
