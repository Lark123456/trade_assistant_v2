# -*- coding: utf-8 -*-
"""策略管理页签：多策略（公式）增删改启停、扫描节奏配置、
内置策略/指标库一键导入、中文逻辑说明书、单票条件级诊断。
（v0.6.3 起移除可视化条件编辑器，策略表达统一为通达信公式）"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
                             QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                             QListWidget, QMessageBox, QPlainTextEdit, QPushButton,
                             QScrollArea, QStackedWidget, QTabWidget, QTableView,
                             QVBoxLayout, QWidget)

from formula_explain import format_explain_lines
from indicators import check_formula
from strategy_presets import PRESET_INDICATORS, PRESET_STRATEGIES
from ui.models import PandasModel

MODE_LABELS = {"intraday": "盘中重点", "batch": "盘前盘后全市场", "manual": "仅手动"}
KIND_LABELS = {"formula": "通达信公式", "indicator": "指标存档", "dna": "DNA精筛"}

STRATEGY_COLUMNS = ["id", "编号", "name", "TDX简称", "类型", "描述", "节奏", "状态", "updated_at"]
INDICATOR_COLUMNS = ["id", "编号", "name", "分类", "描述", "updated_at"]


class StrategyDialog(QDialog):
    """新增/编辑策略对话框。prefill 用于「指标→策略」转换时预填内容。"""

    def __init__(self, db, strategy=None, prefill=None, parent=None):
        super().__init__(parent)
        self.db = db
        self.strategy = strategy
        self._keep_kind = None  # 编辑指标存档时保持 kind 不变
        if strategy and strategy.get("kind") == "indicator":
            self._keep_kind = "indicator"
        self.setWindowTitle("编辑策略" if strategy else "新增策略")
        self.setMinimumWidth(720)
        form = QFormLayout(self)

        self.name_edit = QLineEdit()
        form.addRow("策略名称", self.name_edit)

        # v0.8.15：TDX cfg name 字段别名（≤8 字节 GBK，通达信板块管理显示）。
        # 自动加 MMDD 日期前缀，您只需填"策略简称"部分（如"海选"/"B1HX"/"B1海"）。
        self.tdx_alias_edit = QLineEdit()
        self.tdx_alias_edit.setMaxLength(16)  # 最多 16 个 ASCII 或 8 个汉字
        self.tdx_alias_edit.setPlaceholderText(
            "TDX 板块管理显示的简称（≤8字节 GBK，自动加日期前缀）\n"
            "例：填『海选』 → 9月1号 cfg name = 『91海选』\n"
            "    填『B1HX』 → 9月1号 cfg name = 『91B1HX』\n"
            "留空 → 用策略名截断（可能丢字）")
        self.tdx_alias_edit.setToolTip(
            "TDX cfg name 字段是 8 字节 GBK 硬限制，无法装完整长策略名。\n"
            "这里填简称，代码会自动加日期前缀（MMDD，3字节）组装成完整 cfg name。\n"
            "例：填『海选』+ 9月1号 → 『91海选』（5字节，TDX 完整显示）")
        form.addRow("TDX 简称", self.tdx_alias_edit)

        self.kind_combo = QComboBox()
        self.kind_combo.addItem("通达信公式", "formula")
        self.kind_combo.addItem("指标存档", "indicator")
        self.kind_combo.addItem("DNA精筛", "dna")
        self.kind_combo.currentIndexChanged.connect(self._sync_kind_ui)
        if self._keep_kind:
            self.kind_combo.setEnabled(False)
        form.addRow("策略类型", self.kind_combo)

        # v0.8.0：业务层级（与 kind 技术类型解耦；同 direction 同 level 可重叠）
        self.level_combo = QComboBox()
        for lv, label in ((1, "1层（方向层·粗筛）"),
                          (2, "2层（精细层·精筛）"),
                          (3, "3层（预留）"),
                          (4, "4层（预留）")):
            self.level_combo.addItem(label, lv)
        self.level_combo.setToolTip(
            "1=方向层（一层粗筛）；2=精细层（二层精筛）；3+=预留。\n"
            "跨层级候选按 parent_strategy_ids 多选链式流转。")
        form.addRow("业务层级", self.level_combo)
        self.level_combo.currentIndexChanged.connect(self._sync_level_ui)

        # v0.8.1：level=1 → 显示「分类标签」输入框（自由文本，不参与流转）
        # v0.8.1：level≥2 → 显示「父策略」多选列表（多选 level=N-1 的策略）
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText(
            "B1 / B2 / 超短 / 自定义分类（仅用于展示/搜索，不影响候选流转）")
        form.addRow("分类标签 (level=1)", self.tags_edit)

        self.parent_list = QListWidget()
        self.parent_list.setMaximumHeight(120)
        self.parent_list.setSelectionMode(QListWidget.MultiSelection)
        self.parent_list.setToolTip(
            "level≥2 策略的候选来源（多选）：\n"
            "  选中的父策略命中 → 本策略的精筛候选\n"
            "  level=1 时隐藏此列表")
        form.addRow("父策略 (level≥2)", self.parent_list)

        self.mode_combo = QComboBox()
        for key, label in MODE_LABELS.items():
            self.mode_combo.addItem(label, key)
        form.addRow("扫描节奏", self.mode_combo)

        self.enabled_check = QCheckBox("启用该策略")
        self.enabled_check.setChecked(True)
        form.addRow("", self.enabled_check)

        # 公式编辑 + 自动中文释义预览
        self.formula_edit = QPlainTextEdit()
        self.formula_edit.setPlaceholderText(
            "通达信风格公式，支持多语句与中文变量名，例如：\n"
            "白线:=EMA(EMA(C,10),10);\n"
            "黄线:=(MA(C,14)+MA(C,28)+MA(C,57)+MA(C,114))/4;\n"
            "信号:白线>黄线 AND KDJ_J<13;\n"
            "支持 :=赋值、{}注释、多输出「名称:表达式;」（最后一个输出为信号）；\n"
            "函数：MA/EMA/SMA/WMA/DMA/SLOPE/REF/HHV/LLV/CROSS/COUNT/BARSLAST/"
            "EXIST/EVERY/FILTER/IF/BETWEEN/ABS/MAX/MIN/SUM/STD")
        self.formula_edit.setFixedHeight(120)
        self.formula_edit.textChanged.connect(self._refresh_explain)
        self.explain_preview = QPlainTextEdit()
        self.explain_preview.setReadOnly(True)
        self.explain_preview.setFixedHeight(110)
        self.explain_preview.setPlaceholderText("公式的中文释义会实时显示在这里，"
                                                "用于核对「代码实现 == 你的本意」")
        formula_box = QWidget()
        fv = QVBoxLayout(formula_box)
        fv.setContentsMargins(0, 0, 0, 0)
        fv.addWidget(self.formula_edit)
        fv.addWidget(QLabel("◆ 公式中文释义（自动生成）:"))
        fv.addWidget(self.explain_preview)

        # 策略内容：公式编辑（v0.6.3 起统一为公式表达，无可视化条件）
        # 或 DNA 精筛配置（v0.7.0）
        self.dna_widget = QWidget()
        dv = QVBoxLayout(self.dna_widget)
        dv.addWidget(QLabel("◆ 黄金模具（可多选；候选匹配任一模具即计入打分）："))
        self.tpl_list = QListWidget()
        self.tpl_list.setMaximumHeight(110)
        dv.addWidget(self.tpl_list)
        trow = QHBoxLayout()
        trow.addWidget(QLabel("分数阈值"))
        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(0, 100)
        self.threshold_spin.setDecimals(1)
        self.threshold_spin.setValue(80.0)
        self.threshold_spin.setSuffix(" 分")
        self.threshold_spin.setToolTip("低于该分数的候选视为不通过精筛")
        trow.addWidget(self.threshold_spin)
        trow.addWidget(QLabel("窗口模式"))
        self.window_combo = QComboBox()
        self.window_combo.addItem("按模具长度对齐", "template_len")
        self.window_combo.addItem("固定25天", "fixed")
        self.window_combo.setToolTip(
            "候选特征窗口末端固定锚定最新交易日；\n"
            "「按模具长度对齐」= 模具29天则候选看最近29天")
        trow.addWidget(self.window_combo)
        dv.addLayout(trow)
        lrow = QHBoxLayout()
        self.length_scan_check = QCheckBox("长度弹性扫描(15~40天)")
        self.length_scan_check.setChecked(True)
        self.length_scan_check.setToolTip(
            "在各长度档位中取最高分，避免真实形态周期≠模具长度时漏选")
        lrow.addWidget(self.length_scan_check)
        lrow.addWidget(QLabel("从属粗筛策略"))
        self.parent_combo = QComboBox()
        self.parent_combo.addItem("无（一级独立，可全市场直测）", None)
        for s in db.list_strategies(enabled_only=True):
            if s.get("kind") not in ("indicator", "dna"):
                self.parent_combo.addItem(s["name"], s["id"])
        self.parent_combo.setToolTip(
            "选择后运行顺序为：先跑粗筛策略，再对其命中结果做DNA精筛；\n"
            "选「无」则作为一级策略独立运行（可全市场直测）")
        lrow.addWidget(self.parent_combo, 1)
        dv.addLayout(lrow)
        dv.addStretch(1)

        stack = QStackedWidget()
        stack.addWidget(formula_box)   # index 0: 通达信公式
        stack.addWidget(self.dna_widget)  # index 1: DNA精筛
        form.addRow("策略内容", stack)
        self._content_stack = stack

        # 逻辑说明书（保险机制：记录策略意图）
        self.desc_edit = QPlainTextEdit()
        self.desc_edit.setPlaceholderText(
            "策略逻辑说明书（中文）：这个策略想做什么、每条条件的含义、"
            "参数的含义。写清楚，方便日后核对代码是否实现了你的本意。")
        self.desc_edit.setFixedHeight(90)
        form.addRow("逻辑说明书", self.desc_edit)

        btns = QHBoxLayout()
        ok_btn = QPushButton("保存")
        ok_btn.clicked.connect(self._save)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(ok_btn)
        btns.addWidget(cancel_btn)
        form.addRow(btns)

        if strategy:
            self.name_edit.setText(strategy.get("name", ""))
            kind_idx = 0 if strategy.get("kind") != "indicator" else 1
            if strategy.get("kind") == "dna":
                kind_idx = 2
            self.kind_combo.setCurrentIndex(kind_idx)
            # v0.8.1：编辑时回填 level / tags / parent_strategy_ids
            lv = int(strategy.get("level", 1) or 1)
            lv_idx = max(0, self.level_combo.findData(lv))
            self.level_combo.setCurrentIndex(lv_idx)
            self.tags_edit.setText(strategy.get("tags", "") or "")
            # parent_strategy_ids：level≥2 时回填勾选
            self._sync_level_ui()
            if lv >= 2:
                try:
                    import json as _json
                    psids = _json.loads(strategy.get("parent_strategy_ids") or "[]")
                except (ValueError, TypeError):
                    psids = []
                # 兼容旧字段
                legacy = strategy.get("parent_strategy_id")
                if not psids and legacy:
                    psids = [int(legacy)]
                pid_set = {int(x) for x in psids if str(x).isdigit()}
                for i in range(self.parent_list.count()):
                    item = self.parent_list.item(i)
                    if item.data(Qt.UserRole) in pid_set:
                        item.setSelected(True)
            self.mode_combo.setCurrentIndex(
                max(0, self.mode_combo.findData(strategy.get("scan_mode", "batch"))))
            self.enabled_check.setChecked(bool(strategy.get("enabled", 1)))
            self.desc_edit.setPlainText(strategy.get("description") or "")
            # v0.8.15：TDX 简称回填
            self.tdx_alias_edit.setText(strategy.get("tdx_alias") or "")
            if strategy.get("kind") == "dna":
                self._load_dna_config(strategy.get("formula") or "")
            else:
                self.formula_edit.setPlainText(strategy.get("formula") or "")
        elif prefill:
            self.name_edit.setText(prefill.get("name", ""))
            self.mode_combo.setCurrentIndex(
                max(0, self.mode_combo.findData(prefill.get("scan_mode", "batch"))))
            self.desc_edit.setPlainText(prefill.get("description", ""))
            self.formula_edit.setPlainText(prefill.get("formula", ""))
        self._refresh_explain()
        self._sync_kind_ui()

    def _refresh_explain(self):
        text = self.formula_edit.toPlainText().strip()
        if not text:
            self.explain_preview.clear()
            return
        try:
            self.explain_preview.setPlainText(format_explain_lines(text))
        except ValueError as e:
            self.explain_preview.setPlainText(f"（释义暂不可用：{e}）")

    # ------------------------------------------------------------ DNA 配置
    def _load_templates(self):
        """刷新模具多选列表。返回 {name: checkbox item}。"""
        from PyQt5.QtWidgets import QListWidgetItem
        from core.dna_engine import TemplateManager
        tm = TemplateManager()
        self.tpl_list.clear()
        self._tpl_checks = {}
        for name, meta in tm.list_templates():
            item = QListWidgetItem(
                f"{name}　[{meta.get('code', '')} {meta.get('strategy', '')} "
                f"{meta.get('start', '')}~{meta.get('end', '')} {meta.get('len_days', '')}天]")
            self.tpl_list.addItem(item)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self._tpl_checks[name] = item
        return self._tpl_checks

    def _load_dna_config(self, formula):
        import json
        try:
            cfg = json.loads(formula or "{}")
        except (ValueError, TypeError):
            cfg = {}
        checks = self._load_templates()
        for name in cfg.get("templates", []):
            if name in checks:
                checks[name].setCheckState(Qt.Checked)
        self.threshold_spin.setValue(float(cfg.get("threshold", 80.0)))
        self.window_combo.setCurrentIndex(
            max(0, self.window_combo.findData(cfg.get("window_mode", "template_len"))))
        self.length_scan_check.setChecked(bool(cfg.get("length_scan", True)))
        pid = cfg.get("parent")
        idx = self.parent_combo.findData(pid)
        if idx >= 0:
            self.parent_combo.setCurrentIndex(idx)

    def _sync_kind_ui(self):
        """按策略类型切换内容编辑区：公式 / DNA 配置。"""
        kind = self.kind_combo.currentData()
        self._content_stack.setCurrentIndex(1 if kind == "dna" else 0)
        if kind == "dna" and not hasattr(self, "_tpl_checks"):
            self._load_templates()

    def _sync_level_ui(self):
        """v0.8.1：按业务层级切换内容区——level=1 显示分类标签，level≥2 显示父策略多选。"""
        level = self.level_combo.currentData() or 1
        if level == 1:
            # level=1：分类标签可填，父策略不可选
            self.tags_edit.setVisible(True)
            self.parent_list.setVisible(False)
        else:
            # level≥2：父策略必选（多选 level=level-1 策略）
            self.tags_edit.setVisible(False)
            self.parent_list.setVisible(True)
            self._load_parent_strategies(level - 1)

    def _load_parent_strategies(self, parent_level):
        """v0.8.1：加载 level=parent_level 的所有启用策略到父策略多选列表。"""
        from PyQt5.QtWidgets import QListWidgetItem
        self.parent_list.clear()
        current_id = 0
        if self.strategy:
            current_id = int(self.strategy.get("id") or 0)
        for s in self.db.list_strategies(enabled_only=True, level=parent_level):
            if current_id and int(s.get("id") or 0) == current_id:
                continue
            text = f"{s.get('code', '?')} {s.get('name', '')} (L{s.get('level', '?')})"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, int(s.get("id") or 0))
            self.parent_list.addItem(item)

    def _save(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "提示", "请填写策略名称")
            return
        kind = self.kind_combo.currentData()
        if self._keep_kind:
            kind = self._keep_kind
        if kind == "dna":
            checked = [n for n, it in getattr(self, "_tpl_checks", {}).items()
                       if it.checkState() == Qt.Checked]
            if not checked:
                QMessageBox.warning(self, "提示", "请至少勾选一个黄金模具（可在策略管理页「🧬 DNA模具」中制作）")
                return
            import json
            formula = json.dumps({
                "templates": checked,
                "threshold": self.threshold_spin.value(),
                "window_mode": self.window_combo.currentData(),
                "length_scan": self.length_scan_check.isChecked(),
                "parent": self.parent_combo.currentData(),
            }, ensure_ascii=False)
        else:
            formula = self.formula_edit.toPlainText().strip()
            ok, err = check_formula(formula)
            if not ok:
                QMessageBox.warning(self, "公式错误", err)
                return
        # v0.8.1：业务层级 + 分类标签(level=1) / 父策略(level≥2) 写入
        level = int(self.level_combo.currentData() or 1)
        tags = self.tags_edit.text().strip() if level == 1 else ""
        parent_strategy_ids = []
        if level >= 2:
            for i in range(self.parent_list.count()):
                item = self.parent_list.item(i)
                if item.isSelected():
                    pid = item.data(Qt.UserRole)
                    if pid:
                        parent_strategy_ids.append(int(pid))
            if not parent_strategy_ids:
                QMessageBox.warning(self, "提示",
                    f"level={level} 策略必须至少勾选一个父策略（level=1 的策略）")
                return
        # 兼容旧字段（DNA 精筛的 cfg.parent）
        legacy_parent_id = self.parent_combo.currentData() if kind == "dna" else None
        self._result = {
            "name": name, "kind": kind, "formula": formula,
            "scan_mode": self.mode_combo.currentData(),
            "enabled": 1 if self.enabled_check.isChecked() else 0,
            "description": self.desc_edit.toPlainText().strip(),
            "parent_strategy_id": legacy_parent_id,  # 兼容
            "level": level, "tags": tags,
            "parent_strategy_ids": parent_strategy_ids,
            "tdx_alias": self.tdx_alias_edit.text().strip(),
        }
        self.accept()

    def result(self):
        return getattr(self, "_result", None)


class FormulaDocDialog(QDialog):
    """策略/指标说明书：意图描述 + 逐条中文释义。"""

    def __init__(self, title, description, formula, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"逻辑说明书：{title}")
        self.resize(680, 560)
        root = QVBoxLayout(self)
        root.addWidget(QLabel("◆ 策略意图说明（人工编写）"))
        self.desc_view = QPlainTextEdit()
        self.desc_view.setReadOnly(True)
        self.desc_view.setPlainText(description or "（未填写说明书）")
        root.addWidget(self.desc_view, stretch=2)
        root.addWidget(QLabel("◆ 公式逐条中文释义（自动生成，用于核对代码实现）"))
        self.explain_view = QPlainTextEdit()
        self.explain_view.setReadOnly(True)
        try:
            self.explain_view.setPlainText(format_explain_lines(formula))
        except ValueError as e:
            self.explain_view.setPlainText(f"（释义暂不可用：{e}）")
        root.addWidget(self.explain_view, stretch=2)
        root.addWidget(QLabel("◆ 公式原文"))
        self.formula_view = QPlainTextEdit()
        self.formula_view.setReadOnly(True)
        self.formula_view.setPlainText(formula)
        root.addWidget(self.formula_view, stretch=2)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        root.addWidget(close_btn)


class VersionCompareDialog(QDialog):
    """两个版本并排对比（公式 + 说明书）。"""

    def __init__(self, v1, v2, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"版本对比：{v1.get('code', '')} v{v1['version']} vs v{v2['version']}")
        self.resize(1000, 560)
        root = QHBoxLayout(self)
        for v in (v1, v2):
            box = QWidget()
            bv = QVBoxLayout(box)
            bv.addWidget(QLabel(f"◆ v{v['version']}（{v.get('created_at', '')[:16]}）"))
            view = QPlainTextEdit()
            view.setReadOnly(True)
            view.setPlainText(_version_text(v))
            bv.addWidget(view)
            root.addWidget(box)
        bar = QHBoxLayout()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        bar.addStretch(1)
        bar.addWidget(close_btn)
        outer = QVBoxLayout(self)
        outer.addLayout(root, stretch=1)
        outer.addLayout(bar)


def _version_text(v):
    return (f"【公式】\n{v.get('formula') or ''}\n\n"
            f"【说明书】\n{v.get('description') or '（未填写）'}\n\n"
            f"【名称】{v.get('name', '')}　【节奏】{v.get('scan_mode', '')}")


class VersionHistoryDialog(QDialog):
    """策略版本历史（v0.6.5）：按编号归集的迭代版本，可查看/对比。"""

    def __init__(self, db, strategy, parent=None):
        super().__init__(parent)
        self.db = db
        self.versions = db.list_strategy_versions(strategy["id"])
        self.setWindowTitle(
            f"版本历史：{strategy.get('name', '')}（{strategy.get('code', '')}）")
        self.resize(880, 580)
        root = QVBoxLayout(self)
        body = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel(f"◆ 迭代版本（编号 {strategy.get('code', '')} 归集）"))
        self.listw = QListWidget()
        self.listw.setMinimumWidth(240)
        for v in self.versions:
            self.listw.addItem(f"v{v['version']}　{v.get('created_at', '')[:16]}")
        self.listw.currentRowChanged.connect(self._show)
        left.addWidget(self.listw)
        right = QVBoxLayout()
        right.addWidget(QLabel("◆ 版本内容（公式 + 说明书）"))
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        right.addWidget(self.detail)
        body.addLayout(left, 1)
        body.addLayout(right, 2)
        root.addLayout(body, stretch=1)
        if self.versions:
            self.listw.setCurrentRow(0)
        bar = QHBoxLayout()
        note = QLabel("版本归档目录：archive/strategies/ 下按编号归集")
        note.setStyleSheet("color:#666;")
        bar.addWidget(note)
        bar.addStretch(1)
        cmp_btn = QPushButton("对比两个版本")
        cmp_btn.setToolTip("先按住 Ctrl 在列表中选择两个版本，再点此按钮")
        cmp_btn.clicked.connect(self._compare)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        bar.addWidget(cmp_btn)
        bar.addWidget(close_btn)
        root.addLayout(bar)

    def _show(self, row):
        if not (0 <= row < len(self.versions)):
            return
        v = self.versions[row]
        self.detail.setPlainText(_version_text(v))

    def _compare(self):
        rows = [i.row() for i in self.listw.selectedIndexes()]
        if len(rows) != 2:
            QMessageBox.information(self, "提示", "请先按住 Ctrl 选择两个版本，再点「对比两个版本」")
            return
        v1 = self.versions[rows[0]]
        v2 = self.versions[rows[1]]
        if v1["version"] == v2["version"]:
            return
        if v1["version"] > v2["version"]:
            v1, v2 = v2, v1
        VersionCompareDialog(v1, v2, self).exec_()


class PresetImportDialog(QDialog):
    """内置策略/指标导入对话框（v0.7.2：策略库已删除，仅保留导入功能）。"""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("导入内置策略 / 指标")
        self.resize(720, 500)
        root = QVBoxLayout(self)
        tabs = QTabWidget()

        existing = {r["name"] for r in db.list_strategies()}
        self.strategy_checks = {}
        self.indicator_checks = {}
        for presets, store, label in (
                (PRESET_STRATEGIES, self.strategy_checks, "选股策略（导入后参与扫描）"),
                (PRESET_INDICATORS, self.indicator_checks, "指标（主图/副图，仅存档，可转策略）")):
            page = QWidget()
            pv = QVBoxLayout(page)
            pv.addWidget(QLabel(label))
            for p in presets:
                cb = QCheckBox(f"{p['name']}　[{p.get('category', '')}]")
                cb.setToolTip((p.get("description") or "")[:500])
                if p["name"] in existing:
                    cb.setChecked(True)
                    cb.setEnabled(False)
                    cb.setText(cb.text() + "　（已导入）")
                store[p["name"]] = cb
                pv.addWidget(cb)
            pv.addStretch(1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
            tabs.addTab(scroll, "选股策略" if presets is PRESET_STRATEGIES else "指标库")
        root.addWidget(tabs)

        bar = QHBoxLayout()
        import_btn = QPushButton("导入所选")
        import_btn.clicked.connect(self._import)
        cancel_btn = QPushButton("关闭")
        cancel_btn.clicked.connect(self.reject)
        bar.addStretch(1)
        bar.addWidget(import_btn)
        bar.addWidget(cancel_btn)
        root.addLayout(bar)

    def _import(self):
        count = 0
        for p in PRESET_STRATEGIES + PRESET_INDICATORS:
            cb = (self.strategy_checks if p["kind"] == "formula"
                  else self.indicator_checks).get(p["name"])
            if cb is None or not cb.isChecked() or not cb.isEnabled():
                continue
            self.db.add_strategy(
                name=p["name"], kind=p["kind"], formula=p["formula"],
                conditions=None,
                scan_mode=p.get("scan_mode", "manual"),
                enabled=1, description=p.get("description", ""))
            count += 1
        QMessageBox.information(self, "导入完成", f"已导入 {count} 项到策略管理。")
        self.accept()


class ScanDateDialog(QDialog):
    """立即扫描设置（v0.7.3）：选择信号日期范围。

    - 默认 = 最新交易日（判断最新K线，命中自动进池）
    - 可选历史日期范围（判断范围内每个交易日的K线，用于对照验证，不自动进池）
    - 日期范围限制在日K缓存覆盖区间内（无数据的日期置灰不可选）
    """

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("立即扫描 · 信号日期")
        self.setMinimumWidth(420)
        form = QFormLayout(self)
        dmin, dmax = db.get_bars_date_range()
        self.start_edit = QDateEdit()
        self.end_edit = QDateEdit()
        for e in (self.start_edit, self.end_edit):
            e.setCalendarPopup(True)
            e.setDisplayFormat("yyyy-MM-dd")
            if dmin and dmax:
                e.setMinimumDate(QDate.fromString(dmin, "yyyy-MM-dd"))
                e.setMaximumDate(QDate.fromString(dmax, "yyyy-MM-dd"))
            else:
                e.setMaximumDate(QDate.currentDate())
        default = QDate.fromString(dmax, "yyyy-MM-dd") if dmax else QDate.currentDate()
        self.start_edit.setDate(default)
        self.end_edit.setDate(default)
        form.addRow("开始日期", self.start_edit)
        form.addRow("结束日期", self.end_edit)
        note = QLabel(
            "默认=最新交易日（判断最新K线，命中自动进池）；\n"
            "选历史日期范围将逐日判断该范围内每个交易日的K线，"
            "用于对照验证策略（不自动进池）；\n"
            "可选范围 = 日K缓存覆盖区间，区间外日期置灰。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#666;")
        form.addRow(note)
        btns = QHBoxLayout()
        ok_btn = QPushButton("开始扫描")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(ok_btn)
        btns.addWidget(cancel_btn)
        form.addRow(btns)

    def date_range(self):
        return (self.start_edit.date().toString("yyyy-MM-dd"),
                self.end_edit.date().toString("yyyy-MM-dd"))


class StrategyTab(QWidget):
    def __init__(self, db, on_run_scan=None, on_changed=None, parent=None):
        super().__init__(parent)
        self.db = db
        self.on_run_scan = on_run_scan
        self.on_changed = on_changed
        self._build_ui()
        self._load()

    def _build_ui(self):
        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        add_btn = QPushButton("＋ 新增策略")
        add_btn.clicked.connect(self._add)
        edit_btn = QPushButton("✏️ 编辑")
        edit_btn.clicked.connect(self._edit)
        toggle_btn = QPushButton("启用/停用")
        toggle_btn.clicked.connect(self._toggle)
        del_btn = QPushButton("删除")
        del_btn.clicked.connect(self._delete)
        dna_btn = QPushButton("🧬 DNA模具")
        dna_btn.setToolTip("制作/管理股票形态黄金模具（二层DNA精筛用）：\n"
                           "输入代码+起止日期 → 三维特征曲线核验 → 保存入库")
        dna_btn.clicked.connect(self._open_dna_builder)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._load)
        diag_btn = QPushButton("🔍 单票诊断")
        diag_btn.setToolTip("选一只股票逐条核对公式每个条件的数值与通过/不通过")
        diag_btn.clicked.connect(self._diagnose)
        doc_btn = QPushButton("📖 说明书")
        doc_btn.setToolTip("查看所选策略/指标的逻辑说明书与公式中文释义")
        doc_btn.clicked.connect(self._show_doc)
        ver_btn = QPushButton("📜 历史版本")
        ver_btn.setToolTip("查看所选策略的迭代版本（按编号归集，可对比）")
        ver_btn.clicked.connect(self._show_versions)
        run_btn = QPushButton("▶ 立即扫描所选")
        run_btn.setToolTip(
            "仅对当前选中的【单个策略】跑全市场深度扫描（4线程并行，约1分钟）。\n"
            "区别于选股模块的「盘前/盘后全市场扫描」：后者批量运行所有启用的"
            "「盘前盘后全市场」节奏策略")
        run_btn.clicked.connect(self._run)
        for b in (add_btn, edit_btn, toggle_btn, del_btn, dna_btn, refresh_btn,
                  diag_btn, doc_btn, ver_btn, run_btn):
            bar.addWidget(b)
        bar.addStretch(1)
        root.addLayout(bar)

        # 策略列表标题行（导入内置入口）
        title_bar = QHBoxLayout()
        # v0.8.0：移除「📦 导入内置」入口——所有策略（含 B1/B2/砖型图等）一视同仁，
        # 用户自定义添加的策略与项目自带的策略在 UI 上完全一致。
        title_bar.addWidget(QLabel("◆ 策略列表（业务层级/选股方向/公式/说明书均可编辑）"))
        title_bar.addStretch(1)
        root.addLayout(title_bar)

        self.table = QTableView()
        self.table.setModel(PandasModel(None))
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.doubleClicked.connect(lambda _i: self._edit())  # 双击行编辑
        root.addWidget(self.table, stretch=3)

        root.addWidget(QLabel("◆ 指标库（主图/副图存档，不参与扫描，可一键转换为策略）"))
        ibar = QHBoxLayout()
        ind_doc_btn = QPushButton("📖 查看说明书")
        ind_doc_btn.clicked.connect(self._show_doc_ind)
        convert_btn = QPushButton("⚙ 转换为策略")
        convert_btn.clicked.connect(self._convert_ind)
        ind_edit_btn = QPushButton("编辑公式")
        ind_edit_btn.clicked.connect(self._edit_ind)
        ind_del_btn = QPushButton("删除")
        ind_del_btn.clicked.connect(self._delete_ind)
        for b in (ind_doc_btn, convert_btn, ind_edit_btn, ind_del_btn):
            ibar.addWidget(b)
        ibar.addStretch(1)
        root.addLayout(ibar)
        self.ind_table = QTableView()
        self.ind_table.setModel(PandasModel(None))
        self.ind_table.setAlternatingRowColors(True)
        self.ind_table.setSelectionBehavior(QTableView.SelectRows)
        root.addWidget(self.ind_table, stretch=2)

    # ---------------------------------------------------------------- 数据
    def _load(self):
        import pandas as pd
        rows = self.db.list_strategies()
        strats, inds = [], []
        for r in rows:
            desc = r.get("formula") or ""
            if r.get("kind") == "indicator":
                inds.append({
                    "id": r["id"], "编号": r.get("code", ""), "name": r["name"],
                    "分类": (r.get("description") or "").splitlines()[0] if r.get("description") else "",
                    "描述": desc,
                    "updated_at": r.get("updated_at", ""),
                })
            else:
                strats.append({
                    "id": r["id"], "编号": r.get("code", ""), "name": r["name"],
                    "TDX简称": r.get("tdx_alias") or "",
                    "类型": KIND_LABELS.get(r.get("kind"), r.get("kind")),
                    "描述": desc,
                    "节奏": MODE_LABELS.get(r.get("scan_mode"), r.get("scan_mode")),
                    "状态": "启用" if r.get("enabled") else "停用",
                    "updated_at": r.get("updated_at", ""),
                })
        sdf = pd.DataFrame(strats)
        idf = pd.DataFrame(inds)
        self.table.model().setDataFrame(sdf if not sdf.empty else sdf)
        self.ind_table.model().setDataFrame(idf if not idf.empty else idf)

    def _selected(self, table=None):
        table = table or self.table
        idx = table.currentIndex()
        df = table.model().dataframe()
        if idx.isValid() and 0 <= idx.row() < len(df):
            return self.db.get_strategy(int(df.iloc[idx.row()]["id"]))
        return None

    def _refresh_all(self):
        self._load()
        if self.on_changed:
            self.on_changed()

    # ------------------------------------------------------------ 策略 CRUD
    def _open_dna_builder(self):
        from ui.dna_builder_dialog import DnaBuilderDialog
        dlg = DnaBuilderDialog(self.db, self)
        dlg.exec_()

    def _add(self):
        dlg = StrategyDialog(self.db, None, None, self)
        if dlg.exec_() == QDialog.Accepted:
            info = dlg.result()
            if info:
                self.db.add_strategy(info["name"], kind=info["kind"],
                                     formula=info["formula"],
                                     scan_mode=info["scan_mode"],
                                     enabled=info["enabled"],
                                     description=info.get("description", ""),
                                     parent_strategy_id=info.get("parent_strategy_id"),
                                     level=info.get("level", 1),
                                     tags=info.get("tags", ""),
                                     parent_strategy_ids=info.get("parent_strategy_ids"),
                                     tdx_alias=info.get("tdx_alias", ""))
                self._refresh_all()

    def _edit(self):
        st = self._selected()
        if st is None:
            QMessageBox.information(self, "提示", "请先选择策略")
            return
        dlg = StrategyDialog(self.db, st, None, self)
        if dlg.exec_() == QDialog.Accepted:
            info = dlg.result()
            if info:
                self.db.update_strategy(st["id"], **info)
                self._refresh_all()

    def _toggle(self):
        st = self._selected()
        if st is None:
            return
        self.db.update_strategy(st["id"], enabled=0 if st["enabled"] else 1)
        self._refresh_all()

    def _delete(self):
        st = self._selected()
        if st is None:
            return
        if QMessageBox.question(self, "确认", f"删除策略「{st['name']}」？") == QMessageBox.Yes:
            self.db.delete_strategy(st["id"])
            self._refresh_all()

    def _run(self):
        st = self._selected()
        if st is None:
            QMessageBox.information(self, "提示", "请先选择策略")
            return
        if not st.get("enabled"):
            QMessageBox.information(self, "提示", "该策略已停用，请先启用")
            return
        # v0.7.3：选择信号日期范围（默认最新交易日，可历史对照验证）
        dlg = ScanDateDialog(self.db, self)
        if dlg.exec_() != QDialog.Accepted:
            return
        start_date, end_date = dlg.date_range()
        if self.on_run_scan:
            self.on_run_scan(st, start_date, end_date)

    # ------------------------------------------------------------ 内置导入
    # v0.8.0：移除「导入内置」UI 入口——所有策略（含原内置策略与用户新建策略）
    # 在主 UI 上完全一致。strategy_presets.py 仍保留为「策略模板」源，
    # 可在后续通过隐藏菜单项或导入脚本调用。

    # ------------------------------------------------------------ 说明书
    def _show_doc(self, st=None):
        st = st or self._selected()
        if st is None:
            QMessageBox.information(self, "提示", "请先选择策略或指标")
            return
        dlg = FormulaDocDialog(st.get("name", ""), st.get("description", ""),
                               st.get("formula", ""), self)
        dlg.exec_()

    def _show_doc_ind(self):
        self._show_doc(self._selected(self.ind_table))

    # ------------------------------------------------------------ 指标库操作
    def _convert_ind(self):
        ind = self._selected(self.ind_table)
        if ind is None:
            QMessageBox.information(self, "提示", "请先选择指标")
            return
        signal_tpl = ""
        note = ""
        for p in PRESET_INDICATORS:
            if p["name"] == ind.get("name"):
                signal_tpl = p.get("convert_signal", "")
                break
        if not signal_tpl:
            signal_tpl = "{ ↓ 请把下面这行替换成你的信号条件 }\n信号:;\n"
            note = "请补充信号条件后再保存。"
        prefill = {
            "name": f"{ind.get('name', '指标')}选股",
            "kind": "formula",
            "formula": f"{ind.get('formula', '')}\n{signal_tpl}",
            "scan_mode": "batch",
            "description": (f"{ind.get('description', '')}\n"
                            f"【转换说明】本策略由指标「{ind.get('name', '')}」转换而来。"
                            f"{note}"),
        }
        dlg = StrategyDialog(self.db, None, prefill, self)
        if dlg.exec_() == QDialog.Accepted:
            info = dlg.result()
            if info:
                self.db.add_strategy(info["name"], kind=info["kind"],
                                     formula=info["formula"],
                                     scan_mode=info["scan_mode"],
                                     enabled=info["enabled"],
                                     description=info.get("description", ""),
                                     level=info.get("level", 1),
                                     tags=info.get("tags", ""),
                                     parent_strategy_ids=info.get("parent_strategy_ids"))
                self._refresh_all()

    def _edit_ind(self):
        ind = self._selected(self.ind_table)
        if ind is None:
            QMessageBox.information(self, "提示", "请先选择指标")
            return
        dlg = StrategyDialog(self.db, ind, None, self)
        if dlg.exec_() == QDialog.Accepted:
            info = dlg.result()
            if info:
                info["kind"] = "indicator"
                self.db.update_strategy(ind["id"], **info)
                self._refresh_all()

    def _delete_ind(self):
        ind = self._selected(self.ind_table)
        if ind is None:
            return
        if QMessageBox.question(self, "确认", f"删除指标「{ind['name']}」？") == QMessageBox.Yes:
            self.db.delete_strategy(ind["id"])
            self._refresh_all()

    # ------------------------------------------------------------ 版本历史
    def _show_versions(self):
        st = self._selected()
        if st is None:
            QMessageBox.information(self, "提示", "请先选择策略或指标")
            return
        dlg = VersionHistoryDialog(self.db, st, self)
        dlg.exec_()

    # ------------------------------------------------------------ 单票诊断
    def _diagnose(self):
        from ui.diagnose_dialog import DiagnoseDialog
        st = self._selected() or self._selected(self.ind_table)
        dlg = DiagnoseDialog(self.db, current=st, parent=self)
        dlg.exec_()
