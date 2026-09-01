# -*- coding: utf-8 -*-
"""程序入口：python main.py"""
import logging
import multiprocessing
import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from config import DB_PATH
from database import Database
from data_feed import DataFeed
from ui.main_window import MainWindow
from workers import Worker


def _start_monitor_widget():
    """后台启动 monitor_widget 子进程 (v0.8.16: 快速加监控浮动小窗口)

    子进程独立运行, 关主 app 时随主进程终止.
    失败不阻塞主 app 启动.
    """
    try:
        import monitor_widget
        proc = multiprocessing.Process(
            target=monitor_widget.main,
            name="monitor_widget",
            daemon=True)  # 守护进程, 主 app 退出时自动终止
        proc.start()
        logging.getLogger().info("monitor_widget 子进程已启动 pid=%d", proc.pid)
        return proc
    except Exception as e:
        logging.getLogger().warning("monitor_widget 启动失败: %s", e)
        return None


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

    # v0.8.16: 后台启动"快速加监控"小窗口
    monitor_proc = _start_monitor_widget()

    win = MainWindow(worker, db)
    win.show()

    worker.start()
    worker.request_stock_list()

    code = app.exec_()

    # 收尾: 关 monitor 子进程
    if monitor_proc and monitor_proc.is_alive():
        monitor_proc.terminate()
        monitor_proc.join(timeout=2)

    sys.exit(code)


if __name__ == "__main__":
    main()
