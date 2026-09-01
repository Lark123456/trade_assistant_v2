# -*- coding: utf-8 -*-
"""通达信自选股板块双向同步（v0.8.2）。

打通 Python 程序与本机通达信（TDX）的自选股板块：
- 导出：把 Python 端选出的票写入 TDX 自定义板块（.blk 纯文本），在通达信里
  直接浏览 K 线、二次筛选；
- 导入：读取通达信里筛好的板块 .blk，一键加入观察池，方便后续设置监控。

## 文件格式（本机 E:\\TDX 实测）
- 板块文件：`<TDX>/T0002/blocknew/<名称>.blk`，纯文本，每行
  `[市场位1位][6位代码]` + CRLF。市场位：0=深 / 1=沪 / 2=北交所。
- 板块注册：`<TDX>/T0002/blocknew/blocknew.cfg`，每条 120 字节：
  字节 0-7 板块名（GBK，0x00 填充）；8-9 类型（00 00）；10-119 文件名
  （GBK，0x00 填充，不带 .blk 后缀）。

## 注意
- 通达信运行中修改 blocknew 目录：新写 .blk 一般重启通达信后可见；
  通达信退出时可能用内存中的板块列表回写 blocknew.cfg。建议：导出后
  重启通达信，或先关通达信再导出（本模块不强制，但 UI 上会提示）。
"""
import logging
import os
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

log = logging.getLogger("tdx_sync")

CFG_RECORD_SIZE = 120   # blocknew.cfg 每条记录字节数
NAME_FIELD = 8          # 板块名字段宽度
TYPE_FIELD = 2          # 类型字段宽度


@dataclass
class TdxStock:
    """TDX 板块中的一只股票。"""
    code: str          # 6 位代码
    market: int        # 0=深 / 1=沪 / 2=北交所

    @property
    def line(self) -> str:
        return f"{self.market}{self.code}"


def tdx_market_digit(code: str) -> int:
    """按代码推断 TDX 市场位：6/9 开头=沪(1)，4/8/92 开头=北交所(2)，其余=深(0)。"""
    code = str(code).zfill(6)
    if code.startswith(("6", "9")) and not code.startswith("92"):
        return 1
    if code.startswith(("4", "8", "92")):
        return 2
    return 0


def parse_blk_line(line: str) -> Optional[TdxStock]:
    """解析一行 `市场位+6位代码`；非法行返回 None。"""
    s = line.strip()
    if len(s) != 7 or not s.isdigit():
        return None
    market = int(s[0])
    code = s[1:]
    if market not in (0, 1, 2):
        return None
    return TdxStock(code=code, market=market)


def read_blk(path: str) -> List[TdxStock]:
    """读取 .blk 文件（容错：跳过空行/非法行）。"""
    stocks: List[TdxStock] = []
    if not os.path.exists(path):
        return stocks
    with open(path, "r", encoding="ascii", errors="ignore") as f:
        for line in f:
            st = parse_blk_line(line)
            if st:
                stocks.append(st)
    return stocks


def write_blk(path: str, stocks: Iterable[TdxStock]) -> int:
    """写 .blk 文件（覆盖）。返回写入条数。"""
    lines, seen = [], set()
    for st in stocks:
        if st.code in seen:
            continue
        seen.add(st.code)
        lines.append(st.line)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="ascii", newline="") as f:
        f.write("\r\n".join(lines) + ("\r\n" if lines else ""))
    return len(lines)


class TdxBlockSync:
    """通达信 blocknew 目录的双向同步器。"""

    def __init__(self, tdx_root: str):
        # tdx_root: 通达信安装根目录（含 tdxw.exe），如 E:\\TDX
        self.tdx_root = tdx_root
        self.block_dir = os.path.join(tdx_root, "T0002", "blocknew")
        self.cfg_path = os.path.join(self.block_dir, "blocknew.cfg")

    # ------------------------------------------------------------ 校验
    @staticmethod
    def is_valid_root(tdx_root: str) -> bool:
        """判断目录是否像通达信根目录（有 T0002/blocknew 子目录）。"""
        return os.path.isdir(os.path.join(tdx_root, "T0002", "blocknew"))

    # ------------------------------------------------------------ 板块枚举
    def list_blocks(self) -> List[Tuple[str, str]]:
        """从 blocknew.cfg 解析板块列表，返回 [(板块名, 文件名), ...]（按 cfg 顺序）。

        若 cfg 解析失败，回退为枚举目录下的 .blk 文件（文件名即板块名）。
        """
        blocks: List[Tuple[str, str]] = []
        if os.path.exists(self.cfg_path):
            with open(self.cfg_path, "rb") as f:
                data = f.read()
            gbk = "gbk"
            for off in range(0, len(data) - CFG_RECORD_SIZE + 1, CFG_RECORD_SIZE):
                rec = data[off:off + CFG_RECORD_SIZE]
                name = rec[:NAME_FIELD].decode(gbk, errors="ignore").rstrip("\x00 ").strip()
                # v0.8.2 实测：文件名在 50-119 段（10-49 为 40 字节 0x00 保留区）
                fname = rec[50:CFG_RECORD_SIZE].decode(gbk, errors="ignore").rstrip("\x00 ").strip()
                if name and fname:
                    blocks.append((name, fname))
        if not blocks:
            # 回退：目录枚举
            try:
                for fn in sorted(os.listdir(self.block_dir)):
                    if fn.lower().endswith(".blk"):
                        blocks.append((fn[:-4], fn[:-4]))
            except OSError:
                pass
        return blocks

    def block_path(self, fname_or_name: str) -> str:
        """板块文件绝对路径（兼容传文件名或板块名）。"""
        name = fname_or_name
        if not name.lower().endswith(".blk"):
            name = f"{name}.blk"
        return os.path.join(self.block_dir, os.path.basename(name))

    # ------------------------------------------------------------ 导出
    def export_block(self, codes: Iterable[str], block_name: str,
                     overwrite: bool = True) -> Tuple[int, str]:
        """把代码列表导出为通达信自定义板块。

        - codes: 6 位代码可迭代（市场位自动推断）
        - block_name: 板块名。⚠️ TDX 上限 8 字节 GBK（4 个汉字 / 8 个字母数字），
          超长部分会被截断（TDX 自身限制，非本模块限制）
        - overwrite: True=覆盖同名板块；False=追加到已有内容后

        返回 (写入条数, 板块文件路径)。
        """
        safe_name = "".join(c for c in block_name
                            if c not in '\\/:*?"<>|').strip() or "程序选股"
        # v0.8.10：不再截断——完整名用于 .blk 文件名与 cfg 的 70 字节 filename 字段，
        # cfg 的 8 字节 name 字段在 _register_block 里仍截断（TDX 文件格式限制）。
        path = self.block_path(safe_name)
        stocks = [TdxStock(code=str(c).zfill(6), market=tdx_market_digit(c))
                  for c in codes if str(c).strip()]
        if overwrite:
            n = write_blk(path, stocks)
        else:
            existing = read_blk(path)
            n = write_blk(path, existing + stocks)
        # 注册到 blocknew.cfg（已存在同名则跳过注册）
        self._register_block(safe_name)
        return n, path

    def _register_block(self, block_name: str) -> None:
        """把板块追加注册到 blocknew.cfg（已存在同名则跳过）。

        直接扫描 cfg 二进制记录（不走 list_blocks 的目录回退路径），
        避免 cfg 不存在时 list_blocks 因回退到目录枚举而误判为"已注册"。
        """
        gbkc = "gbk"
        data = b""
        if os.path.exists(self.cfg_path):
            with open(self.cfg_path, "rb") as f:
                data = f.read()
            for off in range(0, len(data) - CFG_RECORD_SIZE + 1, CFG_RECORD_SIZE):
                rec = data[off:off + CFG_RECORD_SIZE]
                name = rec[:NAME_FIELD].decode(gbkc, errors="ignore").rstrip("\x00 ").strip()
                fname = rec[50:CFG_RECORD_SIZE].decode(gbkc, errors="ignore").rstrip("\x00 ").strip()
                if name == block_name or fname == block_name:
                    return  # cfg 中已注册
        name_b = block_name.encode(gbkc)[:NAME_FIELD].ljust(NAME_FIELD, b"\x00")
        type_b = b"\x00" * TYPE_FIELD
        # v0.8.2 实测布局（E:\TDX 真实 cfg 逐字节验证）：
        #   0-7   板块名（左对齐，0x00 填充）
        #   8-9   类型（00 00）
        #   10-49 40 字节 0x00 保留区
        #   50-119 文件名（左对齐，0x00 填充，不带 .blk 后缀）
        reserved = b"\x00" * 40
        fname_b = block_name.encode(gbkc)[:70].ljust(70, b"\x00")
        record = name_b + type_b + reserved + fname_b
        # 备份后写入
        try:
            with open(self.cfg_path + ".bak", "wb") as f:
                f.write(data)
        except OSError:
            pass
        with open(self.cfg_path, "wb") as f:
            f.write(data + record)
        log.info("已注册通达信板块: %s", block_name)

    # ------------------------------------------------------------ 导入
    def import_block(self, block_name: str) -> List[TdxStock]:
        """读取板块全部股票。"""
        return read_blk(self.block_path(block_name))
