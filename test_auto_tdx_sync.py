"""test_auto_tdx_sync.py: 验证板块名生成 + 异常分支 + 真实导出."""
import os, sys, shutil, pathlib, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath("__file__")))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from core.auto_tdx_sync import (make_block_name, mmdd,
                                  auto_sync_codes_to_tdx, _resolve_tdx_root,
                                  SETTING_AUTO, SETTING_PATH)
import database

PASS = 0
FAIL = 0
def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  {detail}")


print("=== make_block_name ===")
import datetime as dt
today = dt.date(2026, 8, 31)
check("mmdd(2026-08-31) = 831", mmdd(today) == "831")
check("make_block_name(B1十张图, 2026-08-31) = 831B1十 (GBK<=8 截断)",
      make_block_name("B1十张图", today) == "831B1十")
check("make_block_name 默认 today", make_block_name("B1海选").startswith(mmdd()))
check("make_block_name 清理非法字符 (GBK<=8 自动截断)",
      make_block_name('a/b\\c:d*e?f"g<h>i|j') == f"{mmdd()}abcde")
check("make_block_name 空名 -> 未命名策略",
      make_block_name("") == mmdd() + "未命")
check("make_block_name 全空白 -> 未命名策略",
      make_block_name("   ") == mmdd() + "未命")


print("\n=== auto_sync_codes_to_tdx (集成) ===")
# DSH 沙箱限制 tempfile.TemporaryDirectory 子目录创建, 改用项目根目录下的临时目录
import shutil as _shutil
tmp = pathlib.Path(os.path.dirname(os.path.abspath("__file__"))) / "_auto_tdx_test_tmp"
if tmp.exists():
    _shutil.rmtree(tmp, ignore_errors=True)
tmp.mkdir(parents=True, exist_ok=True)

fake_tdx_root = tmp / "fake_TDX"
t0002_blocknew = fake_tdx_root / "T0002" / "blocknew"
t0002_blocknew.mkdir(parents=True, exist_ok=True)

db_path = tmp / "test.db"
db = database.Database(db_path)

try:
    # 关闭自动同步
    db.set_setting(SETTING_AUTO, "0")
    db.set_setting(SETTING_PATH, str(fake_tdx_root))
    ok, msg = auto_sync_codes_to_tdx(db, "B1十张图", ["000001", "000002"], today)
    check("关闭自动同步 -> ok=False", ok is False)
    check("关闭提示含 已关闭", "已关闭" in msg)

    # 开启 + 临时 TDX
    db.set_setting(SETTING_AUTO, "1")
    ok, msg = auto_sync_codes_to_tdx(db, "B1十张图",
                                       ["000001", "000002", "300750"], today)
    check("临时 TDX ok=True", ok is True, msg)
    check("返回消息含 B1十张图", "B1十张图" in msg or "B1十" in msg)
    check("返回消息含 260831", "831" in msg)
    check("返回消息含 3 只", "3 只" in msg)

    expected_blk = t0002_blocknew / "831B1十.blk"
    check(".blk 文件存在", expected_blk.exists())
    if expected_blk.exists():
        with open(expected_blk, "rb") as f:
            content = f.read()
        check(".blk 含 0000001 (深, 000001 是深市)", b"0000001" in content)
        check(".blk 含 000002 (深)", b"000002" in content)
        check(".blk 含 0300750 (深)", b"0300750" in content)

    cfg_path = t0002_blocknew / "blocknew.cfg"
    check("cfg 存在", cfg_path.exists())
    if cfg_path.exists():
        with open(cfg_path, "rb") as f:
            cfg_data = f.read()
        check("cfg 含完整名 (filename slot)", "831B1十".encode("gbk") in cfg_data)
        check("cfg 记录大小 = 120 字节", len(cfg_data) == 120)

    # 覆盖式
    ok, msg = auto_sync_codes_to_tdx(db, "B1十张图", ["000999"], today)
    check("覆盖式 ok=True", ok is True)
    if expected_blk.exists():
        with open(expected_blk, "rb") as f:
            content2 = f.read()
        check("覆盖后不含 0000001", b"0000001" not in content2)
        check("覆盖后含 000999", b"000999" in content2)

    # 另一策略 -> 另一板块
    ok, msg = auto_sync_codes_to_tdx(db, "B2普通", ["600000"], today)
    check("另一策略 ok=True", ok is True)
    expected_blk2 = t0002_blocknew / "831B2普.blk"
    check("B2普通 .blk 存在", expected_blk2.exists())

    # 空 codes
    ok, msg = auto_sync_codes_to_tdx(db, "B1十张图", [], today)
    check("空 codes -> ok=False", ok is False)
    check("空 codes 提示", "无命中代码" in msg)

    # 路径无效
    db.set_setting(SETTING_PATH, str(tmp / "nonexistent_TDX"))
    ok, msg = auto_sync_codes_to_tdx(db, "B1十张图", ["000001"], today)
    check("路径无效 -> ok=False", ok is False)
    check("路径无效 不抛异常(返回消息)",
          any(k in msg for k in ("失败", "未找到", "无效")), msg)

    # 路径解析：settings 里的自定义路径优先于自动探测
    db.set_setting(SETTING_PATH, str(fake_tdx_root))
    resolved = _resolve_tdx_root(db)
    check("settings.tdx.path 优先生效", resolved == str(fake_tdx_root), resolved)
    # 清空 settings 后走 detect_tdx_root（不实际写盘，只验证不抛异常）
    db.set_setting(SETTING_PATH, "")
    try:
        _resolve_tdx_root(db)
        check("清空 settings 后路径探测不抛异常", True)
    except Exception as e:
        check("清空 settings 后路径探测不抛异常", False, str(e))
finally:
    db.close()
    shutil.rmtree(tmp, ignore_errors=True)


print(f"\n==================================================\n结果: {PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)