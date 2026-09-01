# -*- coding: utf-8 -*-
"""复现 DNA 模具制作器第二次抓取无反应的问题。

用 mock 线程模拟快速成功的抓取，连续调用两次 _fetch，
观察第二次点击后的状态变化与异常。
运行：python test_dna_repro.py
"""
import os
import sys
import tempfile
import time
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["QT_LOGGING_RULES"] = "*.debug=false"
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows 控制台 GBK 无法输出 emoji，改用 ASCII 安全输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
import pandas as pd
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import QApplication, QMessageBox

# 静默 QMessageBox（自动化测试）
QMessageBox.warning = staticmethod(lambda *a, **k: print("  [QMessageBox.warning]", a[1] if len(a) > 1 else ""))
QMessageBox.critical = staticmethod(lambda *a, **k: print("  [QMessageBox.critical]", a[1] if len(a) > 1 else ""))
QMessageBox.information = staticmethod(lambda *a, **k: print("  [QMessageBox.information]", a[1] if len(a) > 1 else ""))

import ui.dna_builder_dialog as m


class MockThread(QThread):
    """立即完成的 mock 线程，模拟抓取成功。"""
    done = pyqtSignal(object, str)

    def __init__(self, feed, code, market, count=300, parent=None):
        super().__init__(parent)
        self.feed, self.code, self.market, self.count = feed, code, market, count

    def run(self):
        # 用最近40个交易日，与对话框默认日期区间（今天-30天~今天）重叠
        dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=40).strftime("%Y-%m-%d")
        close = np.linspace(10, 15, 40)
        df = pd.DataFrame({
            "date": dates, "open": close, "high": close * 1.01,
            "low": close * 0.99, "close": close,
            "vol": np.random.rand(40) * 1e5, "amount": np.random.rand(40) * 1e7,
        })
        self.done.emit(df, "")


m.FetchBarsThread = MockThread


class FakeDB:
    def __init__(self):
        self.path = os.path.join(tempfile.gettempdir(), "fake_db.db")
        self._bars = None

    def get_bars_date_range(self):
        return None, None


def pump(app, n=10, dt=0.02):
    for _ in range(n):
        app.processEvents()
        time.sleep(dt)


def main():
    app = QApplication(sys.argv)
    dlg = m.DnaBuilderDialog(FakeDB())
    dlg._feed = None  # 避免真实 DataFeed

    def show(tag):
        print(f"  [{tag}] _fetching={getattr(dlg, '_fetching', 'MISSING')!r} "
              f"btn_enabled={dlg.fetch_btn.isEnabled()} btn_text={dlg.fetch_btn.text()!r} "
              f"save_enabled={dlg.save_btn.isEnabled()}")

    print("=== 初始状态 ===")
    show("init")

    print("=== 第一次抓取 ===")
    try:
        dlg._fetch()
        print("  _fetch() 调用正常")
    except Exception:
        traceback.print_exc()
    show("after call")
    pump(app)
    show("after done")

    print("=== 保存入黄金模具库 ===")
    try:
        dlg._save()
        print("  _save() 调用正常")
    except Exception:
        traceback.print_exc()
    show("after save")

    print("=== 第二次抓取（保存后再次点击） ===")
    try:
        dlg._fetch()
        print("  _fetch() 调用正常")
    except Exception:
        traceback.print_exc()
    show("after call")
    pump(app)
    show("after done")

    print("=== 第三次抓取（再点一次） ===")
    try:
        dlg._fetch()
        print("  _fetch() 调用正常")
    except Exception:
        traceback.print_exc()
    show("after call")
    pump(app)
    show("after done")

    print("REPRO DONE")
    app.quit()


if __name__ == "__main__":
    main()
