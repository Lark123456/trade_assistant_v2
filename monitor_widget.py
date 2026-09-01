# -*- coding: utf-8 -*-
"""快速加监控小窗口（v0.8.16 独立进程版）

功能:
  - 浮动小窗口(always-on-top, 可拖动)
  - 手动输入股票代码 OR 监测剪贴板自动填入(TDX 复制代码后自动填)
  - 选预设模板(价格/涨跌幅/5日均线/放量)填阈值, 或写自定义公式
  - 一键写入 db.alerts 表, 主 app 监控页自动可见

启动方式:
  独立运行: python monitor_widget.py
  主 app 启动: main_window 在子进程拉起 (main.py 里调用)
"""
import sys
import os
import re
import json
import time
import sqlite3
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime

# 路径
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from config import DB_PATH

# === 预设监控模板 ===
# 字段含义: (显示名, alert_type, operator, threshold_unit_label)
PRESETS = [
    ("价格 ≥",       "price",   ">=", "元"),
    ("价格 ≤",       "price",   "<=", "元"),
    ("涨跌幅 ≥",     "percent", ">=", "%"),
    ("涨跌幅 ≤",     "percent", "<=", "%"),
    ("5日均线上穿",   "ma_cross", "cross_up", "(无)"),
    ("5日均线下穿",   "ma_cross", "cross_down", "(无)"),
    ("放量上涨(>1.5×5日均量)", "volume", "surge_up", "(无)"),
    ("跌破5日均线 %", "ma_pct",   "<=", "% (负数)"),
]

# === 股票名查询(简单缓存) ===
_NAME_CACHE = {}

def lookup_stock_name(code: str) -> str:
    """简单查股票名(优先用缓存, 没有则返回空)"""
    if not code or len(code) != 6 or not code.isdigit():
        return ""
    if code in _NAME_CACHE:
        return _NAME_CACHE[code]
    # 先从 db 查
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT name FROM stock_list WHERE code=? LIMIT 1",
                           (code,)).fetchone()
        conn.close()
        if row and row[0]:
            _NAME_CACHE[code] = row[0]
            return row[0]
    except Exception:
        pass
    return ""


def get_db_path() -> str:
    """从配置读 db 路径"""
    return DB_PATH


def add_alert_to_db(code: str, name: str, market: int, alert_type: str,
                    operator: str, threshold: float, params: dict = None) -> int:
    """写入 db.alerts 表, 返回新 id"""
    conn = sqlite3.connect(get_db_path(), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO alerts(code,name,market,alert_type,operator,threshold,"
        "params,enabled,triggered_count,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (str(code).zfill(6), name, market, alert_type, operator, threshold,
         json.dumps(params or {}, ensure_ascii=False), 1, 0, now))
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    return new_id


# === 主窗口 ===
class MonitorWidget:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("快速加监控")
        self.root.geometry("420x320+1100+80")  # 默认右上角
        self.root.attributes("-topmost", True)   # 永远置顶
        self.root.attributes("-toolwindow", True)  # 不在任务栏显示
        # 拖动支持
        self._drag_x = self._drag_y = 0
        self.root.bind("<Button-1>", self._on_drag_start)
        self.root.bind("<B1-Motion>", self._on_drag_motion)
        # 状态
        self._clipboard_check_after_id = None
        self._last_clipboard = ""
        # 构建 UI
        self._build_ui()
        # 启动剪贴板监控
        self._start_clipboard_monitor()

    def _on_drag_start(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_drag_motion(self, event):
        x = self.root.winfo_x() + (event.x - self._drag_x)
        y = self.root.winfo_y() + (event.y - self._drag_y)
        self.root.geometry(f"+{x}+{y}")

    def _build_ui(self):
        # 标题栏
        title = ttk.Frame(self.root)
        title.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(title, text="📌 快速加监控",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        ttk.Label(title, text="(拖动标题栏移动)",
                  font=("Microsoft YaHei UI", 8), foreground="#888").pack(side="right")

        # 股票代码 + 剪贴板按钮
        code_row = ttk.Frame(self.root)
        code_row.pack(fill="x", padx=8, pady=4)
        ttk.Label(code_row, text="股票代码:").pack(side="left")
        self.code_var = tk.StringVar()
        self.code_var.trace_add("write", self._on_code_change)
        code_entry = ttk.Entry(code_row, textvariable=self.code_var, width=12,
                               font=("Consolas", 11))
        code_entry.pack(side="left", padx=4)
        ttk.Button(code_row, text="📋", width=3,
                   command=self._paste_from_clipboard).pack(side="left", padx=2)
        self.name_label = ttk.Label(code_row, text="", foreground="#0066cc")
        self.name_label.pack(side="left", padx=6)

        # 实时价格(占位, 后续接入行情 API)
        self.quote_label = ttk.Label(self.root, text="", foreground="#666",
                                     font=("Microsoft YaHei UI", 9))
        self.quote_label.pack(anchor="w", padx=8, pady=(0, 4))

        # 监控条件: 模板下拉
        cond_row = ttk.Frame(self.root)
        cond_row.pack(fill="x", padx=8, pady=6)
        ttk.Label(cond_row, text="监控条件:").pack(side="left")
        self.preset_var = tk.StringVar(value=PRESETS[0][0])
        preset_combo = ttk.Combobox(cond_row, textvariable=self.preset_var,
                                    values=[p[0] for p in PRESETS],
                                    state="readonly", width=18)
        preset_combo.pack(side="left", padx=4)
        preset_combo.bind("<<ComboboxSelected>>", self._on_preset_change)
        # 阈值输入
        self.threshold_var = tk.StringVar()
        self.threshold_entry = ttk.Entry(cond_row, textvariable=self.threshold_var,
                                        width=10, font=("Consolas", 10))
        self.threshold_entry.pack(side="left", padx=4)
        self.unit_label = ttk.Label(cond_row, text="元", foreground="#666")
        self.unit_label.pack(side="left")

        # 高级公式(默认隐藏)
        self.advanced_expanded = False
        self.advanced_btn = ttk.Button(self.root, text="▼ 高级公式 (可选)",
                                      command=self._toggle_advanced)
        self.advanced_btn.pack(anchor="w", padx=8, pady=(4, 0))
        self.advanced_frame = ttk.Frame(self.root)
        self.formula_var = tk.StringVar()
        self.formula_entry = ttk.Entry(self.advanced_frame,
                                      textvariable=self.formula_var,
                                      font=("Consolas", 9))
        self.formula_entry.pack(fill="x")

        # 按钮区
        btn_row = ttk.Frame(self.root)
        btn_row.pack(fill="x", padx=8, pady=10)
        ttk.Button(btn_row, text="✓ 添加监控", command=self._on_add).pack(side="left")
        ttk.Button(btn_row, text="✕ 清空", command=self._on_clear).pack(side="left", padx=4)

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        status = ttk.Label(self.root, textvariable=self.status_var,
                           relief="sunken", anchor="w",
                           font=("Microsoft YaHei UI", 8))
        status.pack(side="bottom", fill="x")

        # 初始化阈值
        self._on_preset_change()

    def _on_preset_change(self, *_):
        """模板切换时, 更新单位标签和清空阈值"""
        for pname, _atype, _op, unit in PRESETS:
            if pname == self.preset_var.get():
                self.unit_label.config(text=unit)
                if unit == "(无)":
                    self.threshold_var.set("")
                    self.threshold_entry.config(state="disabled")
                else:
                    self.threshold_entry.config(state="normal")
                break

    def _on_code_change(self, *_):
        """代码变化时, 异步查股票名 + 当前价"""
        code = self.code_var.get().strip()
        if len(code) == 6 and code.isdigit():
            name = lookup_stock_name(code)
            self.name_label.config(text=name if name else "未知")
            # TODO: 接入 worker 行情 API 拉现价 (子进程暂时不直接连 worker, 写 db 由主 app 触发)
        else:
            self.name_label.config(text="")

    def _paste_from_clipboard(self):
        """从剪贴板读股票代码"""
        try:
            import pyperclip
            content = pyperclip.paste().strip()
        except Exception:
            content = self.root.clipboard_get().strip()
        m = re.search(r"\b(\d{6})\b", content)
        if m:
            self.code_var.set(m.group(1))
        else:
            self.status_var.set(f"剪贴板没有股票代码: {content[:30]}")

    def _toggle_advanced(self):
        if not self.advanced_expanded:
            self.advanced_frame.pack(fill="x", padx=8, pady=4)
            self.advanced_btn.config(text="▲ 高级公式 (可选)")
            self.advanced_expanded = True
        else:
            self.advanced_frame.pack_forget()
            self.advanced_btn.config(text="▼ 高级公式 (可选)")
            self.advanced_expanded = False

    def _on_add(self):
        """添加监控"""
        code = self.code_var.get().strip()
        if not (len(code) == 6 and code.isdigit()):
            self.status_var.set("❌ 股票代码无效, 应为6位数字")
            return
        # 确定 alert_type / operator / threshold
        if self.advanced_expanded and self.formula_var.get().strip():
            # 高级模式: 整个公式直接存到 params
            alert_type = "custom_formula"
            operator = "custom"
            threshold = 0.0
            params = {"formula": self.formula_var.get().strip()}
        else:
            # 模板模式
            for pname, atype, op, unit in PRESETS:
                if pname == self.preset_var.get():
                    alert_type = atype
                    operator = op
                    break
            else:
                self.status_var.set("❌ 未选择监控条件")
                return
            threshold_str = self.threshold_var.get().strip()
            if unit != "(无)":
                try:
                    threshold = float(threshold_str)
                except ValueError:
                    self.status_var.set(f"❌ 阈值需为数字, 您输入了: '{threshold_str}'")
                    return
            else:
                threshold = 0.0
            params = {}
        # 推断 market (6=沪, 0/3=深, 4/8=北交所)
        if code.startswith(("6", "9")) and not code.startswith("92"):
            market = 1  # 沪
        elif code.startswith(("4", "8", "92")):
            market = 2  # 北交所
        else:
            market = 0  # 深
        # 写入 db
        name = self.name_label.cget("text")
        if name == "未知" or not name:
            name = ""
        try:
            new_id = add_alert_to_db(code, name, market, alert_type,
                                     operator, threshold, params)
            self.status_var.set(f"✅ 已添加监控 id={new_id}: {code} {alert_type}{operator}{threshold}")
            # 闪烁主 app 提示 (通过 db 通知是另一个话题, MVP 不做)
        except Exception as e:
            self.status_var.set(f"❌ 写入失败: {e}")

    def _on_clear(self):
        self.code_var.set("")
        self.threshold_var.set("")
        self.formula_var.set("")
        self.name_label.config(text="")
        self.quote_label.config(text="")
        self.status_var.set("已清空")

    def _start_clipboard_monitor(self):
        """每 0.5 秒检查剪贴板, 6位数字自动填入(剪贴板内容变化时)"""
        def _check():
            try:
                try:
                    import pyperclip
                    current = pyperclip.paste().strip()
                except Exception:
                    try:
                        current = self.root.clipboard_get().strip()
                    except Exception:
                        current = ""
                if current and current != self._last_clipboard:
                    self._last_clipboard = current
                    m = re.search(r"^(\d{6})$", current)
                    if m and m.group(1) != self.code_var.get():
                        self.code_var.set(m.group(1))
                        self.status_var.set(f"📋 自动填入剪贴板: {m.group(1)}")
            except Exception:
                pass
            self._clipboard_check_after_id = self.root.after(500, _check)
        _check()

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        if self._clipboard_check_after_id:
            self.root.after_cancel(self._clipboard_check_after_id)
        self.root.destroy()


def main():
    widget = MonitorWidget()
    widget.run()


if __name__ == "__main__":
    main()