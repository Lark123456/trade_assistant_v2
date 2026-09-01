# -*- coding: utf-8 -*-
"""GUI 冒烟测试：offscreen 模式下构建主窗口、启动工作线程，运行数秒后退出。

运行：python test_gui_smoke.py
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["QT_LOGGING_RULES"] = "*.debug=false"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

from config import DB_PATH
from database import Database
from data_feed import DataFeed
from ui.main_window import MainWindow
from workers import Worker

db = Database(os.path.join(tempfile.gettempdir(), "ta_gui_test.db"))
feed = DataFeed()
worker = Worker(feed, db)
app = QApplication(sys.argv)
win = MainWindow(worker, db)
win.show()

state = {"errors": []}


def on_status(msg):
    print("STATUS:", msg)


def on_quotes(df):
    print("QUOTES_READY rows:", len(df))


def on_list(df):
    print("LIST_READY rows:", len(df))
    # 请求一次全市场行情 + 观察池行情
    worker.request_market_quotes([(r.market, r.code) for r in df.head(200).itertuples()])
    worker.request_quotes([(1, "600000"), (0, "000001")])


def on_alerts(events):
    print("ALERTS:", events)


def finish():
    print("SMOKE OK")
    worker.stop()
    worker.wait(3000)
    db.close()
    app.quit()


app.setQuitOnLastWindowClosed(True)
worker.status.connect(on_status)
worker.list_ready.connect(on_list)
worker.quotes_ready.connect(on_quotes)
worker.alerts_fired.connect(on_alerts)

worker.start()
worker.request_stock_list()
QTimer.singleShot(20000, finish)
sys.exit(app.exec_())
