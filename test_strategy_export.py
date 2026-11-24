# -*- coding: utf-8 -*-
"""策略导出/导入自测（v0.10.5）。

运行：python test_strategy_export.py
覆盖：
    1. 导出包结构（策略 + 历史版本 + 模具库）
    2. 导入到**全新数据库**：策略、公式、别名、父策略引用正确重映射
    3. 重复导入 = 更新而非新增（按编号匹配）
    4. 覆盖导入会清掉本地独有策略
    5. 坏文件被拒绝
    6. DNA 模具库往返（含旧文件自动备份）
"""
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

TMP = tempfile.mkdtemp(prefix="ta_export_")

# 归档写到临时目录，别污染真实 archive/strategies
import config  # noqa: E402

config.ARCHIVE_DIR = os.path.join(TMP, "archive")

import strategy_export  # noqa: E402
from database import Database  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK] " if cond else "  [FAIL] ") + name + (f"  {detail}" if detail and not cond else ""))


# ===========================================================================
print("=== 1. 准备源库并导出 ===")
# ===========================================================================
src = Database(os.path.join(TMP, "src.db"))
sid1 = src.add_strategy(
    "B1海选", kind="formula",
    formula="B1海选:趋势好 AND J<13;", description="海选网", level=1,
    tdx_alias="B1", tags="B1")
sid2 = src.add_strategy(
    "B1十张图", kind="dna", formula='{"templates": [], "threshold": 80}',
    description="二层精筛", level=2, parent_strategy_ids=[sid1], tdx_alias="B1X")
src.add_strategy("单针下30选股", kind="formula", formula="单针:长期>70 AND 短期<30;",
                 level=1, tdx_alias="单针", scan_mode="intraday", enabled=0)

bundle = strategy_export.build_bundle(src, include_templates=False)
check("导出 3 个策略", bundle["counts"]["strategies"] == 3, str(bundle["counts"]))
check("导出历史版本", bundle["counts"]["strategy_versions"] >= 3, str(bundle["counts"]))
check("包格式正确", bundle["format"] == strategy_export.FORMAT)
check("不含密钥字段",
      "settings" not in bundle and "feishu" not in json.dumps(bundle))

path = strategy_export.save_bundle(bundle, os.path.join(TMP, "bak.json"))
check("文件已生成", os.path.exists(path) and os.path.getsize(path) > 100)

# ===========================================================================
print("=== 2. 导入到全新数据库（模拟换机器） ===")
# ===========================================================================
dst = Database(os.path.join(TMP, "dst.db"))
loaded = strategy_export.load_bundle(path)
report = strategy_export.apply_bundle(dst, loaded, include_templates=False)
st = report["stats"]
check("新增 3 个策略", st["added"] == 3, str(st))
check("补入历史版本", st["versions_added"] >= 3, str(st))

rows = dst.list_strategies()
check("策略数量一致", len(rows) == 3, str(len(rows)))
by_name = {r["name"]: r for r in rows}
check("公式原样迁移",
      by_name["B1海选"]["formula"] == "B1海选:趋势好 AND J<13;")
check("TDX 别名保留", by_name["B1海选"]["tdx_alias"] == "B1")
check("层级保留", int(by_name["B1十张图"]["level"]) == 2)
check("节奏保留", by_name["单针下30选股"]["scan_mode"] == "intraday")
check("停用状态保留", int(by_name["单针下30选股"]["enabled"]) == 0)

# 关键：父策略引用必须按新 id 重映射（新库里 id 大概率与源库不同）
pids = json.loads(by_name["B1十张图"]["parent_strategy_ids"] or "[]")
check("父策略引用重映射到新 id",
      pids == [by_name["B1海选"]["id"]], f"{pids} vs {by_name['B1海选']['id']}")
check("旧版 parent_strategy_id 也重映射",
      by_name["B1十张图"]["parent_strategy_id"] == by_name["B1海选"]["id"])

# ===========================================================================
print("=== 3. 重复导入 = 更新而非重复新增 ===")
# ===========================================================================
report2 = strategy_export.apply_bundle(dst, loaded, include_templates=False)
check("第二次导入全部为更新",
      report2["stats"]["added"] == 0 and report2["stats"]["updated"] == 3,
      str(report2["stats"]))
check("策略数量没有翻倍", len(dst.list_strategies()) == 3)

# ===========================================================================
print("=== 4. 覆盖导入 ===")
# ===========================================================================
dst.add_strategy("本地临时策略", kind="formula", formula="C>O;", level=1)
check("覆盖前有 4 个", len(dst.list_strategies()) == 4)
report3 = strategy_export.apply_bundle(dst, loaded, mode="replace",
                                       include_templates=False)
names = [r["name"] for r in dst.list_strategies()]
check("覆盖导入重建 3 个", len(names) == 3, str(names))
check("本地独有策略被清掉", "本地临时策略" not in names)
check("覆盖导入报告为 replace", report3["mode"] == "replace")

# ===========================================================================
print("=== 5. 坏文件被拒绝 ===")
# ===========================================================================
bad = os.path.join(TMP, "bad.json")
with open(bad, "w", encoding="utf-8") as f:
    json.dump({"hello": "world"}, f)
try:
    strategy_export.load_bundle(bad)
    check("非法格式被拒绝", False, "没有抛异常")
except ValueError as e:
    check("非法格式被拒绝", "不是本系统的策略备份" in str(e), str(e))

future = os.path.join(TMP, "future.json")
with open(future, "w", encoding="utf-8") as f:
    json.dump({"format": strategy_export.FORMAT, "version": 99,
               "strategies": []}, f)
try:
    strategy_export.load_bundle(future)
    check("未来版本被拒绝", False)
except ValueError as e:
    check("未来版本被拒绝", "高于当前程序支持" in str(e), str(e))

# ===========================================================================
print("=== 6. DNA 模具库往返 ===")
# ===========================================================================
import numpy as np  # noqa: E402

old_base = config.BASE_DIR
try:
    app1 = pathlib.Path(TMP) / "app1"
    dna1 = app1 / "dna_storage"
    dna1.mkdir(parents=True, exist_ok=True)
    np.savez(os.path.join(dna1, "golden_templates.npz"),
             t0=np.arange(6, dtype=float).reshape(3, 2))
    with open(os.path.join(dna1, "templates_meta.json"), "w", encoding="utf-8") as f:
        json.dump({"甲模具": {"code": "000001", "days": 3}}, f, ensure_ascii=False)

    config.BASE_DIR = app1
    tpl = strategy_export.export_templates()
    check("模具被打包", tpl and tpl["count"] == 1, str(tpl and tpl["count"]))

    bundle_tpl = strategy_export.build_bundle(dst, include_templates=True)
    check("导出包含模具", bundle_tpl["counts"]["dna_templates"] == 1)

    app2 = pathlib.Path(TMP) / "app2"
    (app2 / "dna_storage").mkdir(parents=True, exist_ok=True)
    # 目标已有一个旧模具文件，导入时应该被备份
    (app2 / "dna_storage" / "golden_templates.npz").write_bytes(b"OLD")
    config.BASE_DIR = app2
    msg = strategy_export.import_templates(bundle_tpl["dna_templates"])
    restored = (app2 / "dna_storage" / "golden_templates.npz").read_bytes()
    check("模具文件已恢复", restored[:2] == b"PK", str(restored[:8]))
    check("旧文件已备份",
          any(p.name.startswith("golden_templates.npz.bak-")
              for p in (app2 / "dna_storage").iterdir()))
    check("回执里有数量", "1" in msg, msg)
finally:
    config.BASE_DIR = old_base

# ===========================================================================
print("=== 7. 给用户看的报告文案 ===")
# ===========================================================================
text = strategy_export.format_report(report)
check("报告含新增数", "新增策略" in text and "3" in text, text)
check("报告含模具说明", "模具" in text)

src.close()
dst.close()
print()
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")
