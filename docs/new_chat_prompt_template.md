# 新对话开场 Prompt 模板

> 把下面这段内容复制到新对话框的首条消息里，
> 把「【在这里填写你的具体指令】」替换成本次想做的事。

---

## 复制以下内容

```text
我正在开发一个 PyQt5 桌面应用：个人股票交易决策辅助系统（trade_assistant）。

项目目录：G:\Deepseek workplace\trade_assistant
主要入口：main.py
技术栈：Python 3.x + PyQt5 + pytdx（通达信行情服务器直连）+ SQLite

项目进度、已完成的功能、待办清单、关键决策、已知问题都记录在：
G:\Deepseek workplace\trade_assistant\PROJECT_LOG.md

请你先读取该文件（YAML frontmatter + Markdown 正文），了解项目当前状态后，再执行我的具体指令。

【在这里填写你的具体指令】

执行完毕后，请同步更新 PROJECT_LOG.md：
- 如有新增功能/修复，在 YAML frontmatter 的 milestones 中追加一条记录
- 如有待办完成或新增，更新 YAML frontmatter 的 todo 列表
- 如有新的关键决策，追加到 key_decisions
- 如有新的已知问题，追加到 known_issues
- 同时更新 Markdown 正文对应章节
```

---

## 使用示例

### 示例 1：实现新功能

```text
我正在开发一个 PyQt5 桌面应用：个人股票交易决策辅助系统（trade_assistant）。

项目目录：G:\Deepseek workplace\trade_assistant
主要入口：main.py
技术栈：Python 3.x + PyQt5 + pytdx（通达信行情服务器直连）+ SQLite

项目进度、已完成的功能、待办清单、关键决策、已知问题都记录在：
G:\Deepseek workplace\trade_assistant\PROJECT_LOG.md

请你先读取该文件（YAML frontmatter + Markdown 正文），了解项目当前状态后，再执行我的具体指令。

请实现「K线信号回放」功能：在单票诊断界面中，显示过去 30 天内每一天是否出现该策略信号，以及当天的短期/长期/B2类型等关键指标值。

执行完毕后，请同步更新 PROJECT_LOG.md：
- 如有新增功能/修复，在 YAML frontmatter 的 milestones 中追加一条记录
- 如有待办完成或新增，更新 YAML frontmatter 的 todo 列表
- 如有新的关键决策，追加到 key_decisions
- 如有新的已知问题，追加到 known_issues
- 同时更新 Markdown 正文对应章节
```

### 示例 2：修复 Bug

```text
我正在开发一个 PyQt5 桌面应用：个人股票交易决策辅助系统（trade_assistant）。

项目目录：G:\Deepseek workplace\trade_assistant
主要入口：main.py
技术栈：Python 3.x + PyQt5 + pytdx（通达信行情服务器直连）+ SQLite

项目进度、已完成的功能、待办清单、关键决策、已知问题都记录在：
G:\Deepseek workplace\trade_assistant\PROJECT_LOG.md

请你先读取该文件（YAML frontmatter + Markdown 正文），了解项目当前状态后，再执行我的具体指令。

请修复成交量单位不一致的问题：daily_bars_cache 的 vol 单位是股，market_snapshot 的 vol 单位是手，导致量比计算和 K 线成交量柱显示错误。请统一为「手」并在数据库层面兼容旧缓存。

执行完毕后，请同步更新 PROJECT_LOG.md：
- 如有新增功能/修复，在 YAML frontmatter 的 milestones 中追加一条记录
- 如有待办完成或新增，更新 YAML frontmatter 的 todo 列表
- 如有新的关键决策，追加到 key_decisions
- 如有新的已知问题，追加到 known_issues
- 同时更新 Markdown 正文对应章节
```

---

## 进阶：只给最小上下文（进一步省 Token）

如果你确定任务很明确，不需要模型读整个日志，可以只给关键信息：

```text
项目：trade_assistant（PyQt5 + pytdx + SQLite）
目录：G:\Deepseek workplace\trade_assistant

【当前相关上下文】
- 已完成：...
- 相关文件：...
- 目标：...

【具体指令】
...

完成后请更新 G:\Deepseek workplace\trade_assistant\PROJECT_LOG.md 的相关章节。
```
