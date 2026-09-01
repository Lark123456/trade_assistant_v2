# -*- coding: utf-8 -*-
"""pandas DataFrame 到 QTableView 的模型（含涨跌着色）。"""
import pandas as pd
from PyQt5.QtCore import QAbstractTableModel, QModelIndex, Qt
from PyQt5.QtGui import QColor

from config import COLOR_DOWN, COLOR_FLAT, COLOR_UP


class PandasModel(QAbstractTableModel):
    """只读 DataFrame 表格模型；color_cols 中的列按数值正负红涨绿跌着色。
    text_color_rules: {列名: [(子串, 颜色), ...]}——文本列按子串匹配着色
    （如 买点定级 列：SB1 红 / B1 蓝），用于关键词高亮。

    支持点击表头排序（QTableView.setSortingEnabled(True) 启用）：
    数值列按数值大小，文本列按字符串字典序，混合列智能识别。排序偏好
    在 setDataFrame 替换数据后自动保持（避免 5 秒行情刷新时丢失用户排序）。

    v0.8.0：表头筛选支持——按列名设置允许值集合（白名单），None/空=不过滤。
    视图展示按筛选过滤后的行，原始数据保留在 _df 中，可通过 dataframe(filtered=False)
    获取完整数据。setDataFrame 时保留筛选偏好。
    """

    def __init__(self, df: pd.DataFrame, color_cols=("pct",), text_color_rules=None):
        super().__init__()
        self._df = df.reset_index(drop=True) if df is not None and not df.empty \
            else pd.DataFrame()
        self._visible_idx = None  # None = 全部可见；list[int] = 按列表顺序显示的行索引
        self.color_cols = set(color_cols)
        self.text_color_rules = text_color_rules or {}
        self._sort_col = None
        self._sort_order = Qt.AscendingOrder
        # v0.8.0：每列筛选状态 {列名: set[str] | None}；None/空表示不过滤
        self._filters = {}

    def setDataFrame(self, df: pd.DataFrame):
        self.beginResetModel()
        df2 = df.reset_index(drop=True) if df is not None and not df.empty \
            else pd.DataFrame()
        # 在 reset 块内应用当前排序偏好，避免行情刷新时丢失用户排序
        if self._sort_col is not None and self._sort_col < len(df2.columns):
            df2 = self._apply_sort_to(df2, self._sort_col, self._sort_order)
        self._df = df2
        self._visible_idx = self._compute_visible_idx()
        self.endResetModel()

    def dataframe(self, filtered=True):
        """返回 DataFrame。filtered=True（默认）返回按筛选过滤后的视图；False 返回原始全量。
        索引用默认 0..N-1，方便 UI 直接用 iloc。
        """
        if not filtered or self._visible_idx is None:
            return self._df
        return self._df.iloc[self._visible_idx].reset_index(drop=True)

    def _compute_visible_idx(self):
        """根据 self._filters 计算可见行索引列表（保持原始 df 顺序）。"""
        if not self._filters or self._df.empty:
            return None  # 不过滤 = 全可见
        # 复合筛选（AND）：所有非空筛选都需满足
        mask = pd.Series(True, index=self._df.index)
        for col, allowed in self._filters.items():
            if not allowed:
                continue
            if col not in self._df.columns:
                continue
            # str() 兼容 int/float/None
            col_str = self._df[col].astype(str)
            mask &= col_str.isin({str(x) for x in allowed})
        if mask.all():
            return None  # 全部行都满足 = 无效果
        idx = mask[mask].index.tolist()
        return idx if idx else []  # 全过滤掉时返回空列表（rowCount=0）

    # v0.8.0：表头筛选 API
    def set_column_filter(self, col, allowed_values):
        """设置某列的筛选白名单（None 或 set/list 为空 = 不过滤）。

        allowed_values: set | list | tuple | None
        """
        if col not in self._df.columns:
            return
        if not allowed_values:
            self._filters.pop(col, None)
        else:
            self._filters[col] = set(allowed_values)
        self.beginResetModel()
        self._visible_idx = self._compute_visible_idx()
        self.endResetModel()

    def clear_all_filters(self):
        """清空所有筛选，恢复显示全部行。"""
        if not self._filters:
            return
        self._filters.clear()
        self.beginResetModel()
        self._visible_idx = self._compute_visible_idx()
        self.endResetModel()

    def get_column_filter(self, col):
        """返回某列当前筛选白名单（None = 不过滤）。"""
        return self._filters.get(col)

    def get_active_filters(self):
        """返回当前所有生效的筛选 {列名: set}。"""
        return {k: v for k, v in self._filters.items() if v}

    def get_unique_values(self, col):
        """返回某列在原始数据中的所有不重复值（用于构建筛选菜单）。

        NaN/None 统一显示为 ''。
        """
        if col not in self._df.columns or self._df.empty:
            return []
        vals = self._df[col].astype(str).fillna('')
        return sorted(vals.unique().tolist())

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        if self._visible_idx is None:
            return len(self._df)
        return len(self._visible_idx)

    def columnCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._df.columns)

    def _row_to_orig(self, row):
        """视图行号 → 原始 df 行号（用于 data() 访问）。"""
        if self._visible_idx is None:
            return row
        if 0 <= row < len(self._visible_idx):
            return self._visible_idx[row]
        return -1  # 越界

    def sort(self, column, order=Qt.AscendingOrder):
        """Qt 表头点击触发：按列排序，更新排序状态并刷新视图。"""
        if not (0 <= column < len(self._df.columns)):
            return
        self.beginResetModel()
        self._df = self._apply_sort_to(self._df, column, order)
        self._sort_col = column
        self._sort_order = order
        self._visible_idx = self._compute_visible_idx()
        self.endResetModel()

    @staticmethod
    def _apply_sort_to(df, column, order):
        """按列排序：优先尝试数值，失败回退字符串字典序（NaN/无法转换放末尾）。"""
        col = df.columns[column]
        ascending = (order == Qt.AscendingOrder)
        s = df[col]
        try:
            num = pd.to_numeric(s, errors='coerce')
            # 超过半数能解析为数值 → 按数值排序
            if num.notna().sum() >= len(s) * 0.5:
                sorted_df = df.assign(_sort_key=num).sort_values(
                    by="_sort_key", ascending=ascending, kind='stable',
                    na_position='last').drop(columns="_sort_key")
            else:
                sorted_df = df.assign(_sort_key=s.astype(str)).sort_values(
                    by="_sort_key", ascending=ascending, kind='stable',
                    key=lambda x: x.str.lower(), na_position='last'
                ).drop(columns="_sort_key")
        except Exception:
            sorted_df = df.sort_values(
                by=col, ascending=ascending, kind='stable',
                key=lambda x: x.astype(str).str.lower(), na_position='last')
        return sorted_df.reset_index(drop=True)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return str(self._df.columns[section])
        return section + 1

    def setData(self, index, value, role=Qt.EditRole):
        """Qt 默认实现要求；本 model 只读，所有 setData 返回 False。"""
        return False

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        col = self._df.columns[index.column()]
        orig_row = self._row_to_orig(index.row())
        if orig_row < 0:
            return None
        val = self._df.iloc[orig_row, index.column()]
        if role == Qt.DisplayRole:
            if pd.isna(val):
                return ""
            if isinstance(val, float):
                return f"{val:.4g}"
            return str(val)
        if role == Qt.ForegroundRole:
            if col in self.color_cols and isinstance(val, (int, float)) and not pd.isna(val):
                if val > 0:
                    return QColor(COLOR_UP)
                if val < 0:
                    return QColor(COLOR_DOWN)
                return QColor(COLOR_FLAT)
            rules = self.text_color_rules.get(col)
            if rules and val is not None and not pd.isna(val):
                s = str(val)
                for sub, color in rules:
                    if sub in s:
                        return QColor(color) if isinstance(color, str) else color
        if role == Qt.TextAlignmentRole and isinstance(val, (int, float)):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None
