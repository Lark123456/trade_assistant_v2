# -*- coding: utf-8 -*-
"""程序入口：python main.py"""
import logging
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from config import DB_PATH
from database import Database
from data_feed import DataFeed
from ui.main_window import MainWindow
from workers import Worker


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler()])

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName("个人股票交易决策辅助系统")

    db = Database(DB_PATH)
    feed = DataFeed(db=db)   # v0.7.0：db 用于除权除息事件缓存（前复权自算）
    worker = Worker(feed, db)

    win = MainWindow(worker, db)
    win.show()

    worker.start()
    worker.request_stock_list()

    code = app.exec_()
    sys.exit(code)


if __name__ == "__main__":
    main()
