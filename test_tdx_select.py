"""测试 TdxSyncDialog 导入时的勾选/筛选逻辑（无 GUI 渲染，验证内部方法）。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 直接在临时目录建 TDX + 用真实的 watch_pool SQLite，验证 _do_import 的"只导入勾选"逻辑
import shutil
test_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tdx_test_select")
if os.path.exists(test_root):
    shutil.rmtree(test_root, ignore_errors=True)
os.makedirs(os.path.join(test_root, "T0002", "blocknew"), exist_ok=True)

# 创建板块
from tdx_sync import TdxBlockSync, TdxStock
sync = TdxBlockSync(test_root)
sync.export_block(["600000", "002940", "688799"], "候选观察池")
print(f"板块已创建: {sync.import_block('候选观察池')}")

# 创建真实 Database + Worker
from database import Database
import uuid, os
db_path = os.path.join(test_root, "test.db")
if os.path.exists(db_path):
    os.remove(db_path)
db = Database(db_path)

# 加入 002940、688799 到观察池（600000 不在池）
db.add_watch(code="002940", name="昂利康", market=0, trigger_indicator="测试", reason="t", status="观察中", add_date="2026-08-15")
db.add_watch(code="688799", name="华锐精密", market=1, trigger_indicator="测试", reason="t", status="观察中", add_date="2026-08-15")

watch_codes = {str(r["code"]) for r in db.list_watch()}
print(f"当前观察池: {sorted(watch_codes)}")

# 创建 worker stub + dialog
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication
app = QApplication.instance() or QApplication(sys.argv)

class FakeWorker:
    def get_stock_df(self):
        import pandas as pd
        return pd.DataFrame([
            {"code": "600000", "name": "浦发银行"},
            {"code": "002940", "name": "昂利康"},
            {"code": "688799", "name": "华锐精密"},
        ])

from ui.tdx_sync_dialog import TdxSyncDialog
dlg = TdxSyncDialog(db, FakeWorker(), lambda: watch_codes, lambda: [])

# 配置：板块路径 + 加载板块
dlg.path_edit.setText(test_root)
dlg._reload_blocks()
print(f"板块下拉框: {[dlg.block_combo.itemText(i) for i in range(dlg.block_combo.count())]}")
print(f"当前选择板块: {dlg.block_combo.currentText()}")

# 点预览
dlg._preview_import()
print(f"\n预览后行数: {dlg.preview_table.rowCount()}")
print(f"预览后 import_hint: {dlg.import_hint.text()}")
print("预览内容:")
for r in range(dlg.preview_table.rowCount()):
    code = dlg.preview_table.item(r, 1).text()
    name = dlg.preview_table.item(r, 2).text()
    in_watch = dlg.preview_table.item(r, 4).text()
    checked = dlg.preview_table.item(r, 0).checkState() == Qt.Checked
    print(f"  [{r}] {code} {name} 在池={in_watch or '否'} 勾选={checked}")

# 默认应该只显示未在池的（仅未在池=True）
# 002940/688799 在池，应该被过滤掉，只剩 600000
assert dlg.preview_table.rowCount() == 1, f"预期1行，实际{dlg.preview_table.rowCount()}"
assert dlg.preview_table.item(0, 1).text() == "600000", f"应该是600000，实际{dlg.preview_table.item(0,1).text()}"
assert dlg.preview_table.item(0, 0).checkState() == Qt.Checked, "默认应勾选"
print("\n[OK] '仅未在池'过滤 + 默认勾选 600000")

# 取消"仅未在池"，看全部
dlg.only_new_check.setChecked(False)
print(f"\n取消'仅未在池'后行数: {dlg.preview_table.rowCount()} (应该=3)")
for r in range(dlg.preview_table.rowCount()):
    code = dlg.preview_table.item(r, 1).text()
    name = dlg.preview_table.item(r, 2).text()
    in_watch = dlg.preview_table.item(r, 4).text()
    checked = dlg.preview_table.item(r, 0).checkState() == Qt.Checked
    print(f"  [{r}] {code} {name} 在池={in_watch or '否'} 勾选={checked}")
# 默认勾选状态：未在池的勾上，在池的不勾
assert dlg.preview_table.rowCount() == 3
assert dlg.preview_table.item(0, 0).checkState() == Qt.Checked  # 600000 未在池 → 勾
assert dlg.preview_table.item(1, 0).checkState() == Qt.Unchecked  # 002940 在池 → 不勾
assert dlg.preview_table.item(2, 0).checkState() == Qt.Unchecked  # 688799 在池 → 不勾
print("[OK] 默认勾选策略正确（未在池=勾上，在池=不勾）")

# 反选：全部反向后 002940/688799 应该勾上、600000 不勾
dlg._invert_checks()
for r in range(dlg.preview_table.rowCount()):
    chk = dlg.preview_table.item(r, 0).checkState() == Qt.Checked
    code = dlg.preview_table.item(r, 1).text()
    print(f"  反选后 {code}: {chk}")
assert dlg.preview_table.item(0, 0).checkState() == Qt.Unchecked  # 600000
assert dlg.preview_table.item(1, 0).checkState() == Qt.Checked    # 002940
assert dlg.preview_table.item(2, 0).checkState() == Qt.Checked    # 688799
print("[OK] 反选正确")

# 全不选 → 导入应弹"未勾选"提示
dlg._set_all_checks(Qt.Unchecked)
class FakeMsg:
    @staticmethod
    def information(*a, **k):
        return "未勾选"
        # Print message for visibility
        print(f"  [msg.information] {a[1] if len(a)>1 else ''}")
import ui.tdx_sync_dialog as dlg_mod
orig_info = dlg_mod.QMessageBox.information
dlg_mod.QMessageBox.information = lambda *a, **k: print("  [已阻止] QMessageBox.information called")
try:
    dlg._do_import()
    print("[OK] 全不选时未触发导入")
except Exception as e:
    print(f"[FAIL] {e}")

# 勾上全部，再导入
dlg._set_all_checks(Qt.Checked)
import ui.tdx_sync_dialog as dlg_mod
# 防止 QMessageBox.information 弹窗
dlg_mod.QMessageBox.information = lambda *a, **k: None
dlg._do_import()
# 检查 002940/600000/688799 都不在池中，已被加入（002940/688799 是新加，600000 不在池→加入）
watch_after = {str(r["code"]) for r in db.list_watch()}
print(f"导入后观察池: {sorted(watch_after)}")
# 600000 是新加入的（之前不在池）；002940/688799 已在池跳过
assert "600000" in watch_after, "600000 应被加入"
assert "002940" in watch_after
assert "688799" in watch_after
print("[OK] 全勾导入后 3 只都在池（600000 新加 + 002940/688799 已是池）")

# ================= v0.8.5：日历二级观察池导入（独立于一级观察池） =================
print("\n=== 日历二级观察池导入（dest=calendar:日期） ===")
sync.export_block(["600000", "002940", "601988", "000001"], "日历候选")
day = "2026-08-20"


def cal_watch_codes():
    """当日二级观察池可见集合 = watch_pool(add_date=day) ∪ tdx_imports(add_date=day)。"""
    codes = {str(w["code"]) for w in db.list_watch()
             if (w.get("add_date") or "") == day}
    codes |= {str(t["code"]) for t in db.list_tdx_imports(add_date=day)}
    return codes


dlg2 = TdxSyncDialog(db, FakeWorker(), cal_watch_codes, lambda: [],
                     dest=f"calendar:{day}")
dlg2.path_edit.setText(test_root)
dlg2._reload_blocks()
assert "日历候选" in [dlg2.block_combo.itemText(i) for i in range(dlg2.block_combo.count())]
dlg2.block_combo.setCurrentIndex(dlg2.block_combo.findText("日历候选"))
dlg2._preview_import()
print(f"预览行数: {dlg2.preview_table.rowCount()} (应=4，均未在该日)")
assert dlg2.preview_table.rowCount() == 4
assert dlg2.calendar_date == day and day in dlg2.dest_label_text and "一级观察池" in dlg2.dest_label_text
print("[OK] 日历目标识别 + 当日去重集合正确（4 只均未在该日 → 全部可勾选）")

dlg2._set_all_checks(Qt.Checked)
dlg2._do_import()
imports = db.list_tdx_imports(add_date=day)
imp_codes = sorted({t["code"] for t in imports})
print(f"tdx_imports({day}): {imp_codes}")
assert imp_codes == ["000001", "002940", "600000", "601988"], imp_codes
# 一级观察池不应被这次导入影响
watch_after_cal = {str(r["code"]) for r in db.list_watch()}
assert "601988" not in watch_after_cal, "601988 不应进一级观察池"
assert "000001" not in watch_after_cal, "000001 不应进一级观察池"
print("[OK] 日历导入只进 tdx_imports，不污染一级观察池")

# 再次预览：4 只均已在该日 → 默认「仅未在池」过滤后 0 行
dlg2._preview_import()
print(f"重复预览行数: {dlg2.preview_table.rowCount()} (应=0)")
assert dlg2.preview_table.rowCount() == 0
print("[OK] 日历目标去重正确（已在该日的自动过滤）")

# 清理
db.close()
shutil.rmtree(test_root, ignore_errors=True)
print("\n全部测试通过")
