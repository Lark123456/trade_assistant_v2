---
# 机器可读区块（YAML frontmatter）
# 可用 Python 读取（注意不要用 split 直接切，因为正文也有水平分隔线）：
#   import re, yaml
#   text = open('PROJECT_LOG.md', encoding='utf-8').read()
#   fm = re.search(r'^---\n(.*?)\n---', text, re.DOTALL).group(1)
#   data = yaml.safe_load(fm)
project:
  name: 个人股票交易决策辅助系统
  slug: trade_assistant
  path: "G:\\Deepseek workplace\\trade_assistant"
  entry: main.py
  test_command: python test_headless.py
  test_status: 158 passed / 0 failed
  version: "0.8.11"
  last_updated: "2026-08-30"
  purpose: 围绕个人交易策略的完整闭环本地桌面应用：盘前批量筛票 → 盘中重点范围自动扫描 + 观察池跟踪 + 提醒 → 盘后记录与复盘；v0.7.0 起为「粗筛 → DNA精筛」两阶段选股流水线；v0.7.4 起二层精筛自动串联（DNA骨架分+微观规则综合分）；v0.7.5 起行情日历观察池显示较加入日收盘价涨跌与盘中 5 秒波动；v0.7.6 起三 Tab 统一展示较加入价%/当日涨跌%/振幅%
  docs:
    - docs/数据源与复权策略.md: 前复权为高优先级硬性要求；pytdx 自算引擎/算法/三层监测/切换影响（2026-08-16，v0.7.0）
    - docs/策略管理功能框架.md: 策略管理现状盘点 + 5 域建议框架 + 待拍板问题（2026-08-15）
    - docs/功能盘点与用户故事地图.md: 全量功能清单 + 用户故事地图 + 追溯矩阵 + 埋点方案（2026-08-15/16）
    - docs/界面功能方向.md: 六页签功能方向定义（行情日历/提醒联动/统一组件）+ 策略编号与版本归档（2026-08-15/16）
  tech_stack:
    - Python 3.x
    - PyQt5
    - pytdx
    - SQLite
    - fastdtw 已移除（v0.7.2 起 DNA 精筛改用 numpy 窗口化 DTW）
  core_files:
    main.py: 应用入口
    data_feed.py: pytdx 行情连接、实时行情、日K/指数K线获取（v0.7.0 默认前复权）
    core/adjust.py: 前复权引擎（pytdx xdxr 自算因子，三层监测）
    core/dna_engine.py: DNA 特征工程 + 黄金模具库（npz/json）+ 多维加权 FastDTW 匹配
    core/micro_rules.py: 微观特征加减分 + 买点定级（SB1核心买点/B1底仓买点，v0.7.4）
    indicators.py: 指标计算 + 通达信公式解析引擎
    formula_explain.py: 公式 → 中文逐条释义
    strategy_presets.py: 内置策略库/指标库
    strategy_archive.py: 策略版本归档（按编号归集到 archive/strategies/）
    scanner.py: 策略扫描、日K本地缓存、并行扫描、layer2_screen 二层精筛编排（DTW+微观综合分）
    database.py: SQLite 持久化（xdxr_events 缓存 / scan_history 层与DNA列+综合分/买点/in_watchlist / strategies 从属关系）
    ui/calendar_tab.py: 行情日历（月历 → 当日三子菜单：粗筛/精筛综合分排行榜/观察池表现+5秒刷新 + 加入观察池联动）
    ui/hit_list.py: 统一命中列表组件（多选批量加入观察池）
    ui/dna_builder_dialog.py: DNA 黄金模具制作器（三维特征曲线核验）
    ui/scan_center.py: 选股模块（上粗筛下精筛，全市场DNA直测）
    ui/diagnose_dialog.py: 单票条件级诊断器
    workers.py: 后台线程（一层扫描后自动串联二层精筛、自动进池标注来源层与综合分）
    alert_engine.py: 提醒判定
    push.py: 微信推送
    test_adjust.py: 前复权三层监测自检（akshare 对照）
    test_calendar_ui.py: 行情日历双层筛选 UI 冒烟（offscreen）
    backfill_layer2.py: 二层精筛手动回填工具（对指定交易日粗筛结果执行二层精筛并落库）

current_status:
  phase: 选股逻辑层级重构（v0.8.0）
  short_summary: |
    v0.8.5 完成（任务：通达信导入目标分流——日历二级观察池与一级观察池分离）：
    1) 用户需求：从行情日历二级菜单「观察池」选择通达信导入时，不应进一级
       「观察池」大模块。原实现两个视图读同一张 watch_pool 表（日历按
       add_date=所选日期过滤，一级模块显示全部），导入必然同时出现在两处。
    2) 方案：新增 tdx_imports 表（code/name/market/add_date/block_name/reason/
       created_at），作为「日历通达信导入」独立存储，按 add_date 归属到某日期。
       database.py 新增 add_tdx_import / list_tdx_imports / delete_tdx_import。
    3) 对话框 TdxSyncDialog 新增 dest 参数（默认 watch_pool；calendar:YYYY-MM-DD
       走日历路径）：界面显示导入目标标签；_do_import 分流到
       _do_import_watch_pool（原行为）/ _do_import_calendar（写 tdx_imports，
       去重 = 当日 watch_pool∪tdx_imports 可见集合）。
    4) 行情日历三个子 Tab 的「🔄 通达信」按钮均以 dest=calendar:当前日期 打开；
       日历「观察池」子页在 watch_pool 行之外追加 tdx_imports 当日条目
       （来源=通达信导入，层级=L1·通达信）；删除操作同时删 tdx_imports 记录。
       一级观察池模块入口显式 dest=watch_pool（行为不变）。
    5) 顺手修复：_do_import 的"未预览→全量导入"兜底原按表格行数为 0 判断，
       预览后筛选成 0 行会误触发全量导入 → 改为显式 _previewed 标志。
    6) 测试：test_tdx_select.py 新增日历分流用例（导入只进 tdx_imports、
       不污染一级观察池、当日去重）；test_calendar_ui.py 新增「通达信导入显示
       在日历二级观察池且不进一级观察池」断言（28→30 项）；test_headless 158 项全过。
    ----

    
    
    
        v0.8.11 完成（任务：选股扫描性能优化——全市场扫一次跑多策略）
    1) 现状：盘前/盘后全市场扫描走 workers.auto_scan 任务, 逐个策略串行
       (或并行) 调 scan_formula 扫全市场。N 个策略 → 每票 拉K线/算指标 N 次,
       K线拉取与指标计算占 80%+ 耗时, N 越大越慢。
    2) 优化：scanner.py 新增 multi_scan_formula + parallel_multi_scan_formula——
       每只票只拉一次K线 + 算一次指标 (compute_indicators),
       然后对该票的指标数据逐公式求值 (formula_signal 纯读),
       K线拉取次数 = 股票数 × 1 (而不是 × 策略数)。
    3) workers.auto_scan 改造：公式类策略合并到一次 parallel_multi_scan_formula,
       按 strategy_id 分发结果 → _finalize_strategy_hits 复用单策略的
       落库/计算新信号/自动进池/scan_done.emit 流程。
       DNA 策略保留原路径 (_run_dna_strategy 独立执行)。
       单策略扫描 (request_strategy_scan) 行为不变。
    4) 测试：test_multi_scan.py 11 项覆盖——
       - 单策略结果与原 scan_formula 完全一致
       - 2 策略一次扫描时 K线拉取次数 = 股票数
       - 同票多策略（恒真公式 vs 上升公式）
       - 历史日期扫描（逐日命中 + signal_date 在范围内）
       - parallel_multi_scan_formula 分块合并一致性
       - test_headless/calendar_ui/tdx_sync/auto_tdx_sync 全部无回归。
    ----
    v0.8.10 完成（任务：选股 → 通达信自动同步）
    1) 需求：盘前/盘后全市场扫描（batch）完成后，把每个策略的命中代码自动同步到
       通达信自定义板块，板块名统一 YYMMDD·策略名（例 260831·B1十张图），
       日期前缀让通达信里开头相似的板块自动归为子板块。
    2) 新增 core/auto_tdx_sync.py：auto_sync_codes_to_tdx(db, strategy_name,
       codes, date) → (ok, msg)；make_block_name/yymmdd 组装板块名；
       settings tdx.auto_sync(默认开)/tdx.path(自定义根目录)；
       校验根目录含 T0002/blocknew 否则不写盘（防止 makedirs 静默建目录）；
       TDX 不可用/路径错/开关关 → 返回 ok=False 静默失败, 不阻塞选股流程。
    3) ui/scan_center.py：_on_done 里 mode==batch 且命中非空时自动调用；
       状态栏提示同步结果。
    4) tdx_sync.py：export_block 不再截断板块名为 8 字节——完整名进 .blk 文件名
       与 cfg 的 70 字节 filename 字段；cfg 8 字节 name 字段保留截断(TDX 硬限制)。
    5) 测试：test_auto_tdx_sync.py 30 项(板块名/开关/覆盖/多策略/空列表/路径校验/路径探测)；
       test_headless 158/0、calendar_ui 30/0、tdx_sync 33/0、tdx_select 全过, 无回归。
    ----
    v0.8.9 完成（任务：选股模块三子页拆分——修复 B1十张图 误入一层粗筛的 bug）：
    1) Bug：scan_center 顶部表 _load_latest 未过滤 layer, 导致 layer=2 结果
       在挂着「一层粗筛」标题的区域显示, 与用户预期(精筛仅出现在行情日历/二层精筛)
       不一致；底部表 _load_history 硬过滤 layer==1, 反向正确。
    2) 架构改动：选股模块 ScanCenter 改为 QTabWidget 三子页:
         ① 本次一层(◆ 本次扫描·一层粗筛命中) = 今日 layer=1
         ② 本次二层(◆ 本次扫描·二层DNA精筛打分榜) = 今日 layer=2, 按综合分降序
         ③ 命中历史(全部层 + 策略/日期/层筛选下拉)
       Tab 标题带行数; 二层子页含精筛策略下拉 + 对最近粗筛精筛/全市场直测按钮。
    3) 长期按日期归档职责完全保留在「行情日历」(已有逻辑不变), 选股模块只
       负责「本次运行的临时呈现」。
    4) 测试：smoke 验证 B1十张图(000010) 只出现在 tab2/tab3, 不再进入 tab1;
       tab2 按综合分降序(88.5>75.0); tab3 层筛选正确; test_headless 158/0、
       calendar_ui 30/0、tdx_sync 33/0 全部无回归。
    ----
    v0.8.8 完成（任务：择时极简化——只剩两个手动按钮）：
    1) 删除全部数据/指标/缓存层：core/market_metrics.py（指标纯函数库）、
       core/index_data.py（指数数据源）、core/timing.py（judge 判定）、
       test_market_metrics.py、test_timing.py 全部删除。
    2) ui/timing_tab.py 重写为极简 UI：两个 BigRegimeButton（多头/空头）各占一半，
       点击点亮互斥切换；状态持久化到 settings 表 key=timing.regime；
       启动自动恢复上次选择；底部有「清除选择」按钮。
    3) 配色按 A 股惯例：多头=红（涨 #e64340）/ 空头=绿（跌 #089981）/ 未选=灰；
       无数据获取, 无指标计算, 纯个人判断留痕工具。
    4) test_headless 158/0、calendar_ui 30/0、tdx_sync 33/0、tdx_select 全过,
       无回归。
    ----
        v0.8.7 完成（任务：择时板块重做——剔除活跃市值，改为 3 大指数监控）：
    1) 剔除：活跃市值卡片 + CSV 数据源框架（core/timing_data.py 删除、
       timing/ 目录删除）；指南针 SMF 私有格式确认无法直连（无 SDK/进程）。
       core/timing.judge 判定函数保留（备用通用工具）。
    2) core/market_metrics.py（新）：择时指标纯函数库——均线/趋势状态/
       均线偏离/多周期涨跌幅/区间百分位/量比/MACD/RSI/布林带/相对强弱/
       区间高低点；手写实现不依赖 pandas/numpy；test_market_metrics 32 项全过。
    3) core/index_data.py（新）：指数日线数据源——优先 daily_bars_cache，
       过期调 akshare.stock_zh_index_daily_tx（腾讯源，含成交额）拉取并落盘；
       INDEX_SPECS 定义 上证000001/创业板399006/科创板000688 三指数；
       另含涨停池/北向资金实时拉取函数。
    4) ui/timing_tab.py（重写）：3 张 IndexCard（点位/涨跌幅/1/5/20/60日%/
       近30日柱状走势/趋势/MA20偏离/量比/MACD/RSI/20日位置/布林/vs上证强弱）
       + SharedRowBar 共享横条（三指数横向对比 HTML 表 + 北向资金 + 股债比预留）。
    5) 测试：test_timing.py 精简为 7 项（judge 判定保留）；test_market_metrics.py
       32 项新增；test_headless 158/0、calendar_ui 30/0、tdx 33/0 全部无回归。
    ----
        v0.8.6 完成（任务：新增「择时」大板块——第一个 Tab、启动默认显示）：
    1) 指南针"活跃市值"是私有 SMF 格式数据，本机无 smf_api.dll SDK 进程可调；
       改为**可插拔数据源框架**：
       - 用户导入 (date,value) CSV 到 <BASE_DIR>/timing/active_cap.csv
         （卡片上的「📥 导入CSV」按钮支持粘贴或选文件）
       - 「🧪 用示例数据」按钮：内置 34 个交易日合成序列（仅演示判定逻辑）
       - 数据源选择持久化在 settings 表（timing.active_cap.provider）
    2) core/timing.py：judge(pct_today, pct_yesterday) 纯函数判定，
       1 日优先 → 2 日阈值。阈值常量 BULL=4.0 / BEAR=-2.35；
       用户原话 4 个场景全部通过单元测试（test_timing 32 项）。
    3) core/timing_data.py：ActiveCapDataSource（CSV 解析、save_csv_text、
       use_demo/use_csv 切换、settings 持久化）。
    4) ui/timing_tab.py：DataCard 基类 + ActiveCapCard（首张卡片：最新值、
       今日/昨日涨跌幅、判定大徽章、阈值 spinbox、近 30 日迷你柱状走势）+
       仪表盘网格布局（可扩展）。
    5) main_window.py：择时作为第一个 Tab + tabs.setCurrentWidget 默认显示。
    6) 测试：test_timing.py 32/0；test_headless.py 158/0 无回归。
       期间修复 timing_data._get_setting 未迭代 @contextmanager 的潜在 bug
       （改用 Database.get_setting / set_setting 公开 API）。
    ----
        v0.8.4 完成（接续 2026-08-30，任务：收尾通达信同步 + 回归全绿）：
    1) 重建丢失的 test_tdx_sync.py（核心模块 33 项全过，含真实 E:\TDX 只读验证）；
       test_tdx_select.py 导入勾选逻辑全过。
    2) 回归修复：test_calendar_ui.py 25/3 → 28/0（3 个 FAIL 是测试数据 bug——
       today 非交易日时 bdates 不含 today，加入日收盘取不到；补 today 日K 行）；
       test_headless.py 157/1 → 158/0（1 个 FAIL 是 scan_formula 的 quote_filter
       会剔除 |pct|>9.9 的近涨停票属设计行为，测试改为按同口径预估候选数；
       strategy_archive 兼容 str/Path 的 ARCHIVE_DIR，消除测试 WARNING）。
    3) 清理：calendar_tab.py 死代码（on_imported 内 if False else None）；
       删除遗留 _tmp_*.py/csv 临时分析文件。
    4) README 补充 v0.8.1-8.4 文档（通达信同步章节、10/30日最高涨幅、表头筛选、
       批量删除、fallback 修复说明）。
    ----
    v0.8.3 完成（任务：观察池批量删除 + 日历三 Tab 通达信同步入口）：
    1) 观察池 WatchTab 新增「删除」按钮：选中行 → 删选中；无选中 → 删当前过滤全部；
       确认弹窗防误删；删除后把 scan_history.in_watchlist 回写 0（避免历史命中仍标记在池）。
    2) 行情日历三个子 Tab（一层粗筛/二层精筛/观察池）各加「🔄 通达信」按钮，
       导出范围 = 当前 Tab 筛选后的可见行；导入后只刷新观察池 Tab。
    ----
    v0.8.2 完成（任务：打通通达信自选股板块双向同步）：
    1) tdx_sync.py：核心同步器——读取/写入 .blk（纯文本，每行 [市场位][6位代码]+CRLF，
       市场位 0=深/1=沪/2=北）；板块注册 blocknew.cfg（120 字节/条，GBK：名称8+类型2+
       保留40+文件名70）；板块名 8 字节 GBK 截断；导出覆盖/追加；导入解析。
    2) ui/tdx_sync_dialog.py：观察池下独立子界面——TDX 路径自动探测（E:\TDX 等）+手动浏览；
       导出：目标板块名 + 覆盖/追加；导入：板块下拉 + 预览表（代码/名称/市场/在池?）+
       全选/全不选/反选/仅未在池 + 按勾选导入观察池（待审核，来源标注「通达信导入」）。
    3) 观察池顶部「🔄 通达信」按钮入口。
    4) 实测 E:\TDX\T0002\blocknew 格式验证通过；test_tdx_sync.py 29 项核心测试全过。
    ----
    v0.8.1 完成（任务：所有股票列表加 10/30 日最高涨幅 + 修复现价=加入价 bug + 层级方向细化）：
    1) hit_list.py / calendar_tab.py 三 Tab 新增「10日最高涨幅%」「30日最高涨幅%」：
       加入日起往后 N 个交易日内最高收盘 / 加入日收盘 − 1（含当日；无数据回退 nan）。
    2) Bug 修复（楚天龙 12.28）：粗筛/精筛 fallback_to_base=True 让历史票「现价」永远=
       加入价 → 较加入价% 恒 0%、10/30 最高涨幅被隐藏。改为无实时行情时一律回退
       最新日K（db.get_latest_close），fallback_to_base 参数保留但废弃。
    3) 新增策略对话框细化：level=1 不选「选股方向」直接填分类标签（tags）；
       level≥2 通过 parent_strategy_ids 多选父策略（3 级选 2 级、4 级选 3 级链式）；
       二层精筛「买点定级」列可点击筛选（多选）；底部统计只统计当前筛选后的标的。
    4) 表头任意列点击弹筛选菜单（多选+搜索+全选/清空）——策略/来源/层级/买点定级等。
    5) 测试：test_calendar_ui.py 25/3（原始态），test_headless.py 157/1（原始态，网络项）。
    ----
    v0.8.0 完成（任务：选股逻辑层级化 + 业务层级与实现解耦）：
    1) 数据库迁移：strategies 表新增 level（业务层级：1=方向/2=精细/3+=预留）
       和 direction（选股方向：B1/B2/B3/超短/自定义）；scan_history 新增 direction；
       watch_pool 新增 level（生效层级）。
    2) 通用调度骨架：worker._chain_layerN(level, layer_hits, payload) 通
       用处理任意层级——level=N 的候选 = level=N-1 的命中按 direction 分组。
       level=2 的二层精筛接收同 direction 的一层命中。
    3) 同 direction 同 level 可重叠（多策略命中同一票均落库）；
       跨层级不叠加（观察池 update_watch_dna 时 level 只升不降）。
    4) UI：StrategyDialog 新增「业务层级」「选股方向」控件；策略管理
       移除「📦 导入内置」入口——所有策略一视同仁（用户策略与项目自带策略 UI 一致）；
       strategy_presets.py 降级为「策略模板」（参考库，不在主 UI 暴露）；
       行情日历顶部加 direction 筛选器 + 三 Tab「层级」列。
    5) level=3+ 字段预留但调度代码留 TODO（按你要求「通用调度 = level=2 通用化」）。
    6) 测试：test_headless.py 156 → 158 项全过；test_calendar_ui.py 28 项全过。
    ----
    v0.7.6 完成（任务：一层粗筛/二层精筛 Tab 加上较加入价%/当日涨跌%/振幅%）：
    三个子菜单（一层粗筛 / 二层精筛 / 观察池）统一使用 PerformanceListWidget 组件，
    现价 / 较加入价% / 当日涨跌% / 振幅% 表现列三 Tab 一致：盘中每 5 秒刷新（监听
    worker.quotes_ready 合并 + 各自定时器仅在前台时请求），非交易时段回退最近日K。
    加入价语义：观察池 = add_date 当日 K 线收盘；粗筛/精筛 = 命中日 close（scan_history.price）。
    refresh 时保留选中行；返回月历停止三个定时器。HitListWidget / DnaHitListWidget /
    WatchPerfListWidget 合并为统一组件。
    测试 test_calendar_ui.py 19 → 28 项全部通过；test_headless.py 143 项全部通过；
    真实库 2026-08-17 三 Tab 表现列均正常显示（一层粗筛 169 / 二层精筛 15 / 观察池 156）。
    ----
    v0.7.5 完成（任务：行情日历观察池显示较加入日收盘价涨跌 + 盘中波动）：
    观察池 Tab 新增表现列：来源|代码|名称|加入价|现价|较加入价%|当日涨跌%|振幅%|原因。
    加入价 = 加入交易日（add_date）日K收盘价（本地缓存，无缓存回退 entry_price）；
    较加入价% = (现价/加入价-1)×100（核心指标，红涨绿跌着色）；当日涨跌%/振幅% 盘中为实时
    行情（复用观察池页签每 5 秒的全池行情信号 + 日历自身盘中 5 秒请求兜底，非交易时段回退
    最近一根日K的涨跌与振幅）。刷新保持选中行不丢失；返回月历停止定时器。
    测试：test_calendar_ui.py 11 → 19 项全部通过；test_headless.py 143 项全部通过；
    真实库 2026-08-17 观察池 156 只全部显示 加入价/较加入价%，盘中 5 秒请求链路验证通过。
    ----
    v0.7.4 完成（任务：修复并贯通「行情日历-双层筛选与观察池流转引擎」）：
    1) 二层精筛自动串联：全市场扫描（单策略 batch / 多策略 auto_scan，含定时盘中扫描）
       一层粗筛完成后自动执行二层精筛——DTW 骨架匹配（golden_templates.npz 模具库）
       + 微观规则评分（core/micro_rules.py），final_score = clip(dtw_score + 修正, 0, 100)，
       过滤综合分 >= 75（或 Top15 兜底），持久化 layer=2 与
       dtw_score/final_score/matched_template/buy_level/signals/trade_date/in_watchlist。
    2) 数据库：scan_history 迁移新增 7 列（dtw_score/final_score/matched_template/
       buy_level/signals/in_watchlist/trade_date）；list_scan_history/list_scan_dates
       按 COALESCE(trade_date, date(scan_time)) 过滤（旧数据兼容）；新增
       update_scan_in_watchlist / mark_session_in_watchlist。
    3) UI：行情日历二层精筛 Tab 重构为「序号|股票代码|股票名称|综合得分|骨架相似度|
       买点定级|最佳匹配模具|当日现价|涨跌幅%|微观特征诊断」，按综合得分降序，
       SB1核心买点 红 / B1底仓买点 蓝 高亮；底部【加入观察池】（多选）/【查看K线】；
       加入后 Toast 提示、watch_pool 落库、scan_history.in_watchlist 回写、
       顶部「观察池 X 只」计数实时 +1；选股模块 DNA 排行同步展示综合分/买点。
    4) 修复：原扫描工作流一层完成后不自动串联二层 → 二层永远为 0（B008）。
    5) 验证：2026-08-17 粗筛 169 只（去重 156）回填二层精筛 → 15 只通过
       （6 SB1 / 9 B1）；测试 127 → 143 项全部通过；新增 test_calendar_ui.py 冒烟 11 项通过。
  recent_bugs_fixed:
    - id: B008
      title: 一层粗筛完成后不自动串联二层精筛，二层数据永远为 0
      date: "2026-08-17"
      files: [workers.py, scanner.py, database.py, ui/calendar_tab.py]
      impact: 行情日历二层精筛 Tab 显示 0 只；扫描工作流（workers/scanner）在一层公式扫描后未自动执行 DTW 模具匹配与微观评分，二层数据缺失、未写入数据库（v0.7.4 修复：auto_scan/strategy_scan 完成后自动 _chain_layer2）
    - id: B001
      title: 日K时间排序错误导致 REF 引用方向反了
      date: "2026-08-15"
      files: [data_feed.py, scanner.py]
      impact: 最新K线显示为最旧日期，所有依赖 REF(...,1) 的策略结果错误
    - id: B002
      title: 强制刷新可能用更旧的服务器数据覆盖本地缓存
      date: "2026-08-15"
      files: [scanner.py]
      impact: 公开服务器数据滞后时会把更新的本地缓存覆盖掉
    - id: B003
      title: 提醒 params 落库 JSON 未解析导致提醒检查抛异常
      date: "2026-08-16"
      files: [database.py]
      impact: list_alerts 返回字符串 params，check_quote_alerts/check_bar_alerts/提醒设置页均按 dict 访问 → 提醒检查链路实际不可用（v0.6.5 修复）
  blockers: []

milestones:
  - version: "0.8.0"
    date: "2026-08-17"
    summary: 选股逻辑层级重构 —— 业务层级与实现解耦 + 通用调度骨架 + 移除内置策略概念
    added:
      - database.py：strategies 表新增 level/direction 列；scan_history 新增 direction；watch_pool 新增 level
      - scanner.py：ScanResult 新增 direction 字段；scan_formula / parallel_scan_formula 接受 direction 透传
      - workers.py：新增 _chain_layerN(level, ...) 通用调度骨架；_run_strategy_scan 与 _run_dna_strategy 接收 strategy.level/direction；_auto_add_to_watch / _dna_add_to_watch 接收 level 参数；_chain_layer2 保留为别名
      - ui/strategy_tab.py：StrategyDialog 新增「业务层级」「选股方向」控件；移除「📦 导入内置」入口
      - ui/calendar_tab.py：顶部加 direction 筛选器（轻量级）；三 Tab columns 加「层级」列
      - strategy_presets.py：从「内置策略库」降级为「策略模板」注释（不再在主 UI 暴露）
      - level=3+ 字段预留但调度代码留 TODO
    removed:
      - "📦 导入内置" 按钮 + PresetImportDialog 入口（类保留供将来模板库调用）
    files:
      - database.py
      - scanner.py
      - workers.py
      - ui/strategy_tab.py
      - ui/calendar_tab.py
      - strategy_presets.py
      - test_headless.py
      - test_calendar_ui.py
      - PROJECT_LOG.md
    validation: "test_headless.py 158 通过 / 0 失败（156→158，+2 列字段相关）；test_calendar_ui.py 28 通过 / 0 失败"

  - version: "0.7.7"
    date: "2026-08-17"
    summary: K线图显示Bug修复 —— 实体宽度自适应 + hover tooltip + 右键框选汇总
    added:
      - ui/chart_widget.py：CandlestickItem 重写（不再用 QPicture 缓存，paint() 实时按 viewPixelSize 计算实体宽度）
      - BODY_PX=8 / BODY_MIN_PX=1.0（屏幕像素恒定，避免 X 缩放下实体被压成不可见/十字星）
      - hoverEvent + indexAt + QToolTip：鼠标悬停 K 线显示 OHLC/涨跌/振幅/日期
      - 右键按住拖动：LinearRegionItem 黄色虚线框，松开弹 8 项汇总（区间最高最低/涨跌/振幅/阳阴数/平均振幅/最大单日涨跌/区间成交合计）
      - test_headless.py：143 → 156 项（新增 [13]CandlestickItem 12 项 + [14]框选统计 1 项）
    fixed:
      - id: B009
        title: K线图实体全部塌缩为十字星，无 hover tooltip，无框选分析
        date: "2026-08-17"
        files: [ui/chart_widget.py]
        impact: 用户开杭华股份日K线图看到全部十字星、悬停无信息、无法框选分析。原 QPicture 缓存 + 硬编码 w=0.35 在 X 轴缩放下实体宽度退化；无 hoverEvent；无右键 ROI
    files:
      - ui/chart_widget.py
      - test_headless.py
      - PROJECT_LOG.md
    validation: "test_headless.py 156 通过 / 0 失败（143+13）"

  - version: "0.7.6"
    date: "2026-08-17"
    summary: 行情日历三 Tab（粗筛/精筛/观察池）统一表现列（较加入价%/当日涨跌%/振幅%）
    added:
      - ui/calendar_tab.py：PerformanceListWidget 统一组件，替换 HitListWidget / DnaHitListWidget / WatchPerfListWidget；三 Tab 共用 5s 行情刷新（监听 worker.quotes_ready 合并 + 各自定时器仅前台请求），refresh 保留选中行
      - 表现列：现价 / 较加入价% / 当日涨跌% / 振幅%；加入价语义：观察池 = add_date 收盘，粗筛/精筛 = scan_history.price（命中日 close）
      - fallback_to_base 参数控制非交易时段回退（粗筛/精筛 = 用 base_price；观察池 = 用最近日K）
      - test_calendar_ui.py：19 → 28 项（粗筛/精筛表现列、加入价=命中日、行情刷新同步、三 Tab 共享 quotes_ready）
    fixed: []
    files:
      - ui/calendar_tab.py
      - test_calendar_ui.py
      - README.md
      - PROJECT_LOG.md
    validation: "test_headless.py 143 通过 / 0 失败；test_calendar_ui.py 28 通过；真实库 2026-08-17 一层粗筛 169 / 二层精筛 15 / 观察池 156 全部显示 较加入价%/当日涨跌%/振幅%"

  - version: "0.7.5"
    date: "2026-08-17"
    summary: 行情日历观察池「较加入日收盘价涨跌」+ 盘中 5 秒波动刷新
    added:
      - ui/calendar_tab.py：WatchPerfListWidget 观察池表现表——列 来源|代码|名称|加入价|现价|较加入价%|当日涨跌%|振幅%|原因；加入价=加入交易日日K收盘（缓存，回退 entry_price）；较加入价%=（现价/加入价-1）×100 红涨绿跌；盘中 5 秒刷新（复用观察池页签全池行情信号 + 日历自身盘中请求兜底）；非交易时段回退最近日K的涨跌/振幅；刷新保留选中行；返回月历停止定时器
      - database.py：get_close_on_date（加入日收盘）、get_latest_bar_snapshot（最近两根日K收盘/高低，非交易时段兜底）
      - test_calendar_ui.py：11 → 19 项（表现列/加入价/行情刷新/较加入价%/振幅%/定时器请求）
    fixed: []
    files:
      - ui/calendar_tab.py
      - database.py
      - test_calendar_ui.py
      - README.md
      - PROJECT_LOG.md
    validation: "test_headless.py 143 通过 / 0 失败；test_calendar_ui.py 19 通过；真实库 2026-08-17 观察池 156 只显示 加入价/较加入价%"

  - version: "0.7.4"
    date: "2026-08-17"
    summary: 修复并贯通「行情日历-双层筛选与观察池流转引擎」（一层粗筛 → 自动二层精筛 → 观察池流转）
    added:
      - workers.py：全市场/自动/定时扫描一层完成后自动串联二层精筛（_chain_layer2，跨策略去重、阈值75+Top15 兜底、自动进池标注综合分与买点）
      - core/micro_rules.py：MicroRuleEvaluator 微观特征加减分（11 条规则，clip ±20）+ 买点定级（SB1核心买点/B1底仓买点）
      - scanner.py：layer2_screen（DTW 骨架 + 微观规则 → final_score=clip(dtw+修正,0,100)，支持 threshold/top_n/模板库注入）
      - database.py：scan_history 迁移 7 列（dtw_score/final_score/matched_template/buy_level/signals/in_watchlist/trade_date）；list_scan_history/list_scan_dates 按 COALESCE(trade_date, date(scan_time))；update_scan_in_watchlist / mark_session_in_watchlist
      - ui/calendar_tab.py：二层精筛 Tab 重构（综合得分降序 + SB1红/B1蓝高亮 + 微观诊断列）+ DnaHitListWidget（多选加入观察池/查看K线）+ 计数联动刷新
      - ui/hit_list.py：多选批量加入观察池 + Toast + in_watchlist 回写
      - ui/models.py：PandasModel 文本着色规则（text_color_rules）
      - ui/scan_center.py：DNA 排行改展示综合得分/骨架相似度/买点定级/微观诊断
      - test_headless.py 127 → 143 项；新增 test_calendar_ui.py（offscreen 冒烟 11 项）
      - backfill_layer2.py：历史交易日二层精筛手动回填工具
    fixed:
      - B008 一层完成后不串联二层 → 二层永远 0
    files:
      - workers.py
      - scanner.py
      - database.py
      - core/micro_rules.py
      - ui/calendar_tab.py
      - ui/hit_list.py
      - ui/models.py
      - ui/scan_center.py
      - test_headless.py
      - test_calendar_ui.py
      - backfill_layer2.py
      - README.md
      - PROJECT_LOG.md
    validation: "test_headless.py 143 通过 / 0 失败；test_calendar_ui.py 11 通过；2026-08-17 粗筛169只回填二层精筛15只通过（6 SB1 / 9 B1），日历计数 169/15/156 精准同步"

  - version: "0.6.6"
    date: "2026-08-16"
    summary: 行情日历替换实时行情页签 + 行情快照服务化
    added:
      - ui/calendar_tab.py 行情日历：月历视图（有选股日期淡红背景+单元格命中数+悬停摘要）→ 点击日期进入当日选股清单（策略命中+手动入池，来源列区分，非交易日为空）
      - ui/hit_list.py 统一命中列表组件（来源/策略/代码/名称/价格/涨幅/时间/原因 + 查看K线/加入观察池/单票诊断）
      - 行情快照服务化：全市场行情由扫描中心定时维护（90 秒新鲜度，盘中候选排序不再依赖行情页）
      - database.list_scan_dates（月份命中日期聚合）
    removed:
      - ui/market_tab.py（实时行情页签，含量比/涨速/排序/过滤）
      - warm_vol5 / get_vol5 / vol5 相关数据库方法（vol5_cache 表保留兼容）
      - 行情页设置持久化（market_interval/market_auto）
    files:
      - ui/calendar_tab.py
      - ui/hit_list.py
      - ui/scan_center.py
      - ui/main_window.py
      - scanner.py
      - data_feed.py
      - database.py
      - workers.py
      - test_headless.py
      - README.md
      - docs/界面功能方向.md
      - docs/功能盘点与用户故事地图.md
      - PROJECT_LOG.md
    validation: "test_headless.py 109 通过 / 0 失败；offscreen GUI 冒烟通过（主窗口/日历/每日清单/快照）"

  - version: "0.6.5"
    date: "2026-08-16"
    summary: 策略编号+版本迭代归档 + 提醒策略联动（B003 修复）
    added:
      - 策略固定编号 S0001...（创建分配/删除不回收/改名不变）
      - strategy_versions 版本表 + archive/strategies/{编号}/ 文件夹归档（公式/说明书变更自动新版本，删除策略不删归档）
      - 策略管理页「📜 版本历史」：查看任意版本 + 双版本并排对比
      - 提醒设置移至第 3 位（观察池后）；新增「策略信号」提醒类型（选具体策略，公式快照+编号版本，信号命中即提醒）
      - strategy_archive.py 新模块
    fixed:
      - B003 提醒 params JSON 未解析导致提醒检查抛异常
    files:
      - database.py
      - strategy_archive.py
      - config.py
      - alert_engine.py
      - workers.py
      - ui/alert_tab.py
      - ui/strategy_tab.py
      - ui/main_window.py
      - test_headless.py
      - README.md
      - docs/界面功能方向.md
      - docs/策略管理功能框架.md
      - docs/功能盘点与用户故事地图.md
      - PROJECT_LOG.md
    validation: "test_headless.py 108 通过 / 0 失败（新增编号/版本/策略信号/B003 共 11 项测试）"

  - version: "0.6.4"
    date: "2026-08-15"
    summary: 界面功能方向定义（纯文档，无代码变更）
    added:
      - docs/界面功能方向.md：六页签功能方向（行情日历/提醒联动/统一组件）+ 指标库定位说明 + 3 个待确认问题
      - 新增待办 T015（行情日历改造）/ T016（提醒设置策略联动）
      - 新增关键决策 D010（行情页→行情日历）/ D011（提醒设置第3位+策略信号联动）
    fixed: []
    files:
      - docs/界面功能方向.md
      - PROJECT_LOG.md
    validation: "无代码变更，test_headless.py 结果不变（97 通过）"

  - version: "0.6.3"
    date: "2026-08-15"
    summary: 移除可视化条件编辑器（策略表达统一为通达信公式）
    added:
      - 策略对话框移除「可视化条件」类型与条件树编辑器，只剩公式/指标存档
      - 存量 visual 策略自动迁移为 formula（database.py _migrate）
    removed:
      - cond_builder.py / ui/cond_builder_widget.py（设计记录保留在 docs/策略管理功能框架.md P1 与 docs/功能盘点 F027/F056）
      - 可视化条件相关测试（8 项）
    files:
      - ui/strategy_tab.py
      - scanner.py
      - database.py
      - test_headless.py
      - README.md
      - docs/策略管理功能框架.md
      - docs/功能盘点与用户故事地图.md
      - PROJECT_LOG.md
    validation: "test_headless.py 97 通过 / 0 失败（原 105 项，移除 8 项可视化条件测试）"

  - version: "0.6.2"
    date: "2026-08-15"
    summary: 全量功能盘点 + 用户故事地图（纯文档，无代码变更）
    added:
      - docs/功能盘点与用户故事地图.md：57 项功能清单（F001-F057）+ 盘前/盘中/盘后故事地图 + 需求追溯矩阵 + 功能埋点方案（待确认）
      - 新增待办 T014（功能使用埋点）
    fixed: []
    files:
      - docs/功能盘点与用户故事地图.md
      - PROJECT_LOG.md
    validation: "无代码变更，test_headless.py 结果不变（105 通过）"

  - version: "0.6.1"
    date: "2026-08-15"
    summary: 策略管理功能框架梳理（纯文档，无代码变更）
    added:
      - docs/策略管理功能框架.md：现状盘点 + 错位分析（P1-P7）+ 5 域建议框架 + 4 个待拍板问题
      - 新增待办 T009-T013（参数化模板/命中走势跟踪/版本历史/方向确认/节奏解耦）
    fixed: []
    files:
      - docs/策略管理功能框架.md
      - PROJECT_LOG.md
    validation: "无代码变更，test_headless.py 结果不变（105 通过）"

  - version: "0.6"
    date: "2026-08-15"
    summary: 单针下30 数据对账 + 日K排序修复 + 项目日志建立
    added:
      - 单针下30 完整公式（中期/中长期/四买点/85/30 参考线）
      - 诊断界面数据来源显示与强制刷新缓存
      - 项目日志 PROJECT_LOG.md
    fixed:
      - 日K时间排序 Bug
      - 缓存刷新时旧数据覆盖新数据
    files:
      - data_feed.py
      - scanner.py
      - ui/diagnose_dialog.py
      - strategy_presets.py
      - test_headless.py
      - README.md
      - docs/内置策略说明.md
      - PROJECT_LOG.md
    validation: "000001 2026-08-14 短期=0.0 长期=42.86，与通达信一致"

  - version: "0.5"
    date: "2026-08-15 之前"
    summary: 策略管理重写 + 保险机制（中文释义/诊断）
    added:
      - strategy_tab.py 双区域重写
      - 内置策略库/指标库
      - formula_explain.py
      - diagnose_dialog.py
      - strategies.description 字段
      - 多输出公式 labels
    files:
      - strategy_presets.py
      - formula_explain.py
      - ui/strategy_tab.py
      - ui/diagnose_dialog.py
      - database.py
      - scanner.py
      - test_headless.py
      - README.md
      - docs/内置策略说明.md

  - version: "0.4"
    date: "2026-08-15 之前"
    summary: 公式引擎升级
    added:
      - 通达信脚本语法（:= 赋值、{} 注释、中文标识符、多输出）
      - REF(X, BARSLAST(...)) 序列参数
      - EXIST/EVERY bool 数组修复
    files:
      - indicators.py
      - test_headless.py

  - version: "0.3"
    date: "2026-08-15 之前"
    summary: 基础功能搭建
    added:
      - 实时行情、观察池、交易记录、提醒设置、扫描中心
      - pytdx 数据层、SQLite 持久化、可视化条件编辑器、K线图
    files: []

todo:
  - id: T001
    title: K线信号回放（保险机制第三层）
    priority: high
    status: pending
    related_files: [ui/diagnose_dialog.py, indicators.py]
    description: 在单票诊断中显示过去 N 天哪些 K 线出现信号、信号类型、关键条件数值
  - id: T002
    title: 成交量单位统一
    priority: high
    status: pending
    related_files: [data_feed.py, scanner.py, ui/chart_widget.py, database.py]
    description: daily_bars_cache 的 vol 是股，market_snapshot 的 vol 是手，需统一或显式标注；影响量比、K线成交量柱、因子快照 volume
  - id: T003
    title: 前复权支持
    priority: medium
    status: pending
    related_files: [data_feed.py, indicators.py]
    description: 当前使用原始价格，长期均线（MA114）可能受除权除息影响
  - id: T004
    title: 分钟线/周线/月线
    priority: medium
    status: pending
    related_files: [data_feed.py, indicators.py, ui/chart_widget.py]
    description: 扩展多周期数据与公式引擎支持
  - id: T005
    title: 自定义公式叠加到 K 线图
    priority: high
    status: pending
    related_files: [ui/chart_widget.py, indicators.py]
    description: 把单针下30、黄白线等指标绘制到 K 线图上（用户已拍板：指标库保留，尽快做 T005 让指标库有消费端）
  - id: T006
    title: 策略回测
    priority: medium
    status: pending
    related_files: [scanner.py, indicators.py, database.py]
    description: 基于历史日K对策略进行胜率/盈亏回测
  - id: T007
    title: 收盘自动总结推送
    priority: low
    status: pending
    related_files: [workers.py, push.py, scanner.py]
    description: 盘后自动扫描并推送当日命中、观察池变化、交易记录摘要
  - id: T008
    title: 多模型协作流程优化
    priority: low
    status: in_progress
    related_files: [PROJECT_LOG.md]
    description: 细化本日志更新规范，探索结构化 prompt 模板让新模型更快接手
  - id: T009
    title: 策略参数化模板
    priority: high
    status: pending
    related_files: [ui/strategy_tab.py, indicators.py, scanner.py, strategy_presets.py]
    description: 策略模板 + 参数面板（如 N:=14 声明语法或参数表格），调阈值不碰公式；当前参数（单针70/30、B2涨幅4%等）全部写死在公式里
  - id: T010
    title: 策略命中走势跟踪与效果统计
    priority: high
    status: pending
    related_files: [database.py, workers.py, ui/scan_center.py]
    description: 命中 N 日后涨跌幅统计并显示在命中历史，策略效果评估第一步（效果域），为 T006 回测铺路
  - id: T011
    title: 策略版本历史
    priority: medium
    status: completed
    related_files: [database.py, ui/strategy_tab.py, strategy_archive.py]
    description: v0.6.5 已落地：策略固定编号 S0001 + strategy_versions 版本表 + archive/strategies/ 文件夹归档 + 版本历史/对比 UI；公式/说明书变更自动新版本，删除策略不删归档
  - id: T012
    title: 策略管理方向确认（参数化/效果评估）
    priority: high
    status: pending
    related_files: [ui/strategy_tab.py, ui/chart_widget.py]
    description: 已拍板：移除可视化条件编辑器（v0.6.3）；指标库保留并尽快做 T005 画图（v0.6.4 确认）。剩余问题：参数化优先级、效果评估优先级（见 docs/策略管理功能框架.md 第五节）
  - id: T013
    title: 扫描节奏与策略解耦
    priority: low
    status: pending
    related_files: [database.py, scanner.py, ui/scan_center.py]
    description: 同一策略支持多节奏，或节奏改为「执行方案」对象（一组策略+节奏+候选范围）
  - id: T014
    title: 功能使用埋点
    priority: high
    status: pending
    related_files: [database.py, ui/main_window.py, usage_report.py]
    description: 新增 usage_log 表 + 页签切换/关键按钮埋点 + usage_report.py 报表脚本，跑 1-2 周后按 MoSCoW 拍板功能砍留（方案见 docs/功能盘点与用户故事地图.md 第五节，待用户确认实施）
  - id: T015
    title: 行情日历（替换实时行情页签）
    priority: high
    status: completed
    related_files: [ui/calendar_tab.py, ui/hit_list.py, ui/main_window.py, ui/scan_center.py, workers.py, scanner.py, data_feed.py, database.py]
    description: v0.6.6 已落地：月历（淡红+命中数+悬停摘要）→ 每日选股清单（统一命中列表组件）；行情快照服务化（扫描中心维护 90s 快照）；删除 market_tab.py；量比/vol5 代码清理
  - id: T016
    title: 提醒设置：移至第3位 + 策略信号联动
    priority: high
    status: completed
    related_files: [ui/alert_tab.py, ui/main_window.py, alert_engine.py, workers.py, database.py]
    description: v0.6.5 已落地：页签移至第 3 位；新增「策略信号」提醒类型（选具体策略，公式快照+编号版本，信号命中即提醒）；联动方式已拍板 = B 公式快照（改策略不影响旧提醒，新建提醒用新版）；顺带修复 B003

key_decisions:
  - id: D001
    topic: 单针下30 默认选股信号
    decision: 保留 长期>70 AND 短期<30，不改为四买点信号
    impact: 选股结果更贴近原默认约定；四买点仅作诊断/副图显示
  - id: D002
    topic: B2 普通上影线处理
    decision: 合并为一条策略，结果中标注「完美/稍欠」
    impact: 不拆成两个策略，扫描结果更简洁
  - id: D003
    topic: B2 第⑦条条件
    decision: 加入 C > 黄线
    impact: 灾后重建场景自动满足，普通 B2 必须站上成本线
  - id: D004
    topic: 砖型图版本
    decision: 只做带 白线>黄线 过滤的版本
    impact: 按用户要求不保留纯原版
  - id: D005
    topic: 日K缓存刷新策略
    decision: 服务器数据若比本地缓存旧，保留本地缓存
    impact: 避免公开服务器数据滞后时覆盖券商/本地更新数据
  - id: D006
    topic: 多模型上下文传递
    decision: 使用 PROJECT_LOG.md 作为项目进度与上下文载体
    impact: 新对话可快速了解全局，避免从头复述
  - id: D007
    topic: 策略管理功能框架
    decision: 按「资产定义/生命周期/验证/执行/效果」五域梳理（见 docs/策略管理功能框架.md）；判定效果域（命中走势跟踪+回测）为最大空白、参数化模板为最大缺口；可视化条件与指标库去留待用户拍板（T012）
    impact: 后续开发优先 T009 参数化与 T010 命中走势跟踪；拍板前不再向可视化条件/指标库投入
  - id: D008
    topic: 功能盘点方法体系
    decision: 采用「功能清单 + 用户故事地图 + 需求追溯矩阵 + 功能使用埋点 + MoSCoW」组合（见 docs/功能盘点与用户故事地图.md）；用 1-2 周埋点数据代替猜测来决定功能砍留
    impact: 盘点已完成（57 项功能）；埋点实施待用户确认（T014）
  - id: D009
    topic: 可视化条件编辑器去留
    decision: 移除可视化条件编辑器（用户确认：策略均为先想好再开发，无需图形化拼条件）；策略类型只保留「公式 / 指标存档」
    impact: 编辑对话框简化；存量 visual 策略自动迁移为 formula（数据库 _migrate）；cond_builder 模块与测试删除，设计记录保留在 docs（框架 P1 / 盘点 F027、F056）
  - id: D010
    topic: 实时行情页签 → 行情日历
    decision: 系统定位为「辅助大A监测系统」（跑策略/监控/提醒，非看盘软件），移除实时行情页签，改为以每日为单位归档策略命中结果的「行情日历」；行情数据改为后台快照服务（盘中候选排序仍需）
    impact: 界面从"看盘"转向"回溯+盯机会"；量比/涨速/vol5 预热随行情页一并清理；K线入口移至列表行操作
  - id: D011
    topic: 提醒设置位置与策略联动
    decision: 提醒设置页签移至第 3 位（观察池后、策略管理前）；新增「策略信号」提醒类型，可选择具体策略（如砖型图翻红/回踩黄线），信号命中即提醒
    impact: 提醒成为盘中核心哨兵；与策略管理联动（联动方式绑定ID还是快照待对齐）
  - id: D012
    topic: 指标库定位
    decision: 指标库保留，并尽快实施 T005（指标叠加K线图）让其有消费端（用户拍板，2026-08-15）
    impact: T005 优先级提升为 high；指标库的第三个作用（画图人工核对）落地后不再悬空
  - id: D013
    topic: 行情日历淡红规则
    decision: 当日有自动扫描命中 或 手动加入观察池 → 日期框淡红背景；每日清单同时显示自动命中与手动入池（来源列区分）（用户拍板，2026-08-15）
    impact: 日历 = 当日所有"选股行为"的归档视图，不限于自动命中
  - id: D014
    topic: 策略信号提醒联动方式
    decision: B 公式快照（用户拍板 2026-08-16）：提醒创建时冻结公式 + 记录策略编号/版本；改策略不影响旧提醒，新建提醒自然用新版
    impact: 提醒与策略解耦但有追溯依据（编号+版本可精确对应归档）；策略删除不影响已建提醒
  - id: D015
    topic: 策略编号与版本迭代归集
    decision: 每个策略固定编号（S0001...，创建分配/删除不回收/改名不变）；公式/说明书变更自动生成新版本，按编号归集到 archive/strategies/ 文件夹（用户拍板 2026-08-16）
    impact: 保险机制补上时间维度；提醒快照可追溯"基于哪个版本"；改名不破坏归集

known_issues:
  - id: I001
    title: pytdx 公开服务器数据滞后
    severity: medium
    workaround: 使用本地缓存保护机制；当前系统日期 2026-08-15，部分服务器日K只到 2026-08-03/07
  - id: I002
    title: akshare 获取股票列表偶发 SSL 错误
    severity: low
    workaround: 有 3 次重试，失败时回退 pytdx
  - id: I003
    title: 成交量单位不一致
    severity: high
    workaround: 见待办 T002
  - id: I004
    title: 公式引擎暂不支持自定义函数注册与绘图指令
    severity: low
    workaround: COLOR/LINETHICK 等指令作注释处理，不报错也不生效
  - id: I005
    title: 指标库（indicator 类型）暂无消费端
    severity: medium
    workaround: 指标不参与扫描且 K 线叠加（T005）未实现，当前仅可看说明书或转策略；待 T005 落地或用户拍板去留（T012）
---

# 项目进度日志（Project Log）

> 本文档用于在多个对话/多个大模型之间传递项目上下文，降低重复沟通成本。
> 每次重大修改后，由开发者或 AI 助手更新上方的 YAML 区块与本 Markdown 正文。
> 开启新对话时，可把本文件内容（或按需节选）作为首条 system prompt / 上下文喂给新模型。

---

## 一、项目概览

- **项目名称**：个人股票交易决策辅助系统（trade_assistant）
- **定位**：围绕个人交易策略的完整闭环本地桌面应用
  - 盘前批量筛票 → 盘中重点范围自动扫描 + 观察池跟踪 + 提醒 → 盘后记录与复盘
- **技术栈**：Python 3.x + PyQt5 + pytdx（通达信行情服务器直连）+ SQLite
- **工作目录**：`G:\Deepseek workplace\trade_assistant`
- **主要入口**：`main.py`
- **自检命令**：`python test_headless.py`（当前 108 项全部通过）

### 核心模块

| 文件 | 职责 |
|------|------|
| `main.py` | 应用入口 |
| `data_feed.py` | pytdx 行情连接、实时行情、日K/指数K线获取 |
| `indicators.py` | 指标计算 + 通达信公式解析引擎 |
| `formula_explain.py` | 公式 → 中文逐条释义 |
| `strategy_presets.py` | 内置策略库/指标库（B1/B2/砖型图/单针下30等） |
| `strategy_archive.py` | 策略版本归档（按编号归集到 `archive/strategies/`） |
| `scanner.py` | 策略扫描、日K本地缓存、并行扫描 |
| `database.py` | SQLite 持久化（含策略编号/版本表） |
| `ui/` | PyQt5 界面（6 个页签 + K线图 + 诊断/说明书/内置库/版本历史对话框） |
| `ui/calendar_tab.py` | 行情日历（月历 → 每日选股清单） |
| `ui/hit_list.py` | 统一命中列表组件（K线/入池/诊断行操作） |
| `ui/diagnose_dialog.py` | 单票条件级诊断器 |
| `workers.py` | 后台线程 |
| `alert_engine.py` / `push.py` | 提醒判定（含策略信号）与微信推送 |

### 关键文档

| 文件 | 说明 |
|------|------|
| `docs/策略管理功能框架.md` | 策略管理现状盘点（v0.6）+ 错位分析 P1-P7 + 5 域建议框架 + 待拍板问题（2026-08-15） |
| `docs/功能盘点与用户故事地图.md` | 全量功能清单（57 项 F001-F057）+ 用户故事地图 + 需求追溯矩阵 + 功能埋点方案（2026-08-15） |
| `docs/界面功能方向.md` | 六页签功能方向定义：行情日历（替换实时行情）+ 提醒策略联动 + 统一组件 + 指标库定位说明（2026-08-15） |

---

## 二、当前状态

见 YAML frontmatter 中的 `current_status` 与 `todo` 区块。简要版：

- **当前阶段**：界面功能方向重构（行情日历 + 提醒联动）
- **已完成**：行情数据层、公式引擎、策略管理、保险机制、扫描中心、观察池/交易/提醒等；框架梳理（v0.6.1）；功能盘点（v0.6.2）；移除可视化条件编辑器（v0.6.3）；界面功能方向定义（v0.6.4）；策略编号+版本归档+提醒策略联动（v0.6.5）；行情日历+快照服务化（v0.6.6）
- **最近修复**：日K时间排序 Bug、缓存刷新旧数据覆盖新数据、B003 提醒 params JSON 未解析
- **最近变更**：实时行情页签 → 行情日历（月历淡红+每日选股清单）；行情快照由扫描中心维护；量比/vol5 清理；页签顺序：行情日历→观察池→提醒→策略→扫描→交易
- **下一步重点**：T009 参数化模板 / T010 命中走势跟踪 / T005 指标画图 / T014 埋点（优先级由用户安排）

---

## 三、版本迭代记录

详细记录见 YAML frontmatter 中的 `milestones` 区块。按时间顺序的要点：

1. **v0.6.6（2026-08-16）**：行情日历替换实时行情页签 + 行情快照服务化 + 量比清理（109 项测试通过）
2. **v0.6.5（2026-08-16）**：策略编号+版本迭代归档 + 提醒策略联动 + B003 修复（108 项测试通过）
3. **v0.6.4（2026-08-15）**：界面功能方向定义（行情日历/提醒联动/统一组件，纯文档）
4. **v0.6.3（2026-08-15）**：移除可视化条件编辑器（策略表达统一为公式；存量 visual 策略自动迁移；97 项测试通过）
5. **v0.6.2（2026-08-15）**：全量功能盘点 + 用户故事地图（纯文档）
6. **v0.6.1（2026-08-15）**：策略管理功能框架梳理（纯文档：docs/策略管理功能框架.md）
7. **v0.6（2026-08-15）**：单针下30 数据对账 + 日K排序修复 + 项目日志建立
8. **v0.5**：策略管理重写 + 保险机制（中文释义/诊断/内置库）
9. **v0.4**：公式引擎升级（脚本语法/中文变量/多输出/序列REF）
10. **v0.3 及更早**：基础功能搭建（行情/扫描/观察池/提醒/K线图）

---

## 四、关键决策

详细记录见 YAML frontmatter 中的 `key_decisions` 区块。核心决策包括：

- 单针下30 默认信号保留 `长期>70 AND 短期<30`
- B2 普通合并为一条策略，结果标注「完美/稍欠」
- B2 加入第⑦条 `C > 黄线`
- 砖型图只保留带 `白线>黄线` 过滤的版本
- 缓存刷新时服务器数据若比本地旧则保留本地缓存
- 使用 `PROJECT_LOG.md` 在多模型间传递上下文
- 移除可视化条件编辑器（策略表达统一为公式，D009）
- 实时行情页签 → 行情日历；提醒设置移至第 3 位并联动策略（D010/D011，见 `docs/界面功能方向.md`）
- 策略信号提醒 = 公式快照 + 编号版本追溯（D014）
- 策略固定编号 S0001 + 版本迭代归集到 `archive/strategies/`（D015）

---

## 五、待办清单

详细清单见 YAML frontmatter 中的 `todo` 区块。当前高优先级：

- **T009** 策略参数化模板（调阈值不碰公式）
- **T010** 策略命中走势跟踪与效果统计（效果域，T006 回测前置）
- **T005** 自定义公式叠加到 K 线图（指标库消费端，已定保留）
- **T012** 策略管理方向确认（参数化/效果评估优先级，见 `docs/策略管理功能框架.md`）
- **T014** 功能使用埋点（用数据决定功能砍留，见 `docs/功能盘点与用户故事地图.md`）

> ✅ 已完成：T011 策略版本历史、T016 提醒策略联动（v0.6.5）、T015 行情日历（v0.6.6）

---

## 六、已知问题 / 技术债务

详细记录见 YAML frontmatter 中的 `known_issues` 区块。主要包括：

- pytdx 公开服务器数据滞后
- akshare 获取股票列表偶发 SSL 错误
- 成交量单位不一致（股 vs 手）
- 公式引擎暂不支持自定义函数注册与绘图指令生效
- 指标库（indicator 类型）暂无消费端：不扫描、K线叠加未做，只能看说明书或转策略

---

## 七、如何把这份日志喂给新的大模型对话

### 推荐做法

1. **不要从头聊起**。新对话首条消息直接给：
   - 本文件完整内容（如果模型上下文够长）
   - 或按需求节选 YAML 中的相关字段

2. **按任务类型节选**：

   - 想做新功能 → 给 `project` + `current_status.short_summary` + `todo`
   - 想修 Bug → 给 `project` + `current_status.recent_bugs_fixed` + `known_issues`
   - 想 review 代码 → 给相关文件路径 + 对应 `milestones` 条目

3. **Prompt 模板示例**：

   ```text
   请基于以下项目状态，帮我实现 trade_assistant 的「K线信号回放」功能。
   工作目录：G:\Deepseek workplace\trade_assistant
   
   [粘贴 YAML 中 project / current_status / todo 的相关内容]
   ```

4. **让 AI 每次修改后更新本文件**。任务结尾加一句：

   > 修改完成后，请同步更新 PROJECT_LOG.md 的 YAML frontmatter（milestones / todo / current_status）以及 Markdown 正文。

---

## 八、本次更新记录

- **更新日期**：2026-11-24（v0.10.5）
- **更新原因**：用户确认「不希望策略被上传到 GitHub，但确实有迁移需求」，要求做一键导入/导出；
  排查中发现 `archive/strategies/**.md`（含逻辑说明书 + 公式原文）**从仓库初始导入起就在
  公开仓库里**，属于泄露面
- **更新内容**：
  - 新增 `strategy_export.py`：一键**导出为单个 JSON**（`strategies` + `strategy_versions` +
    `dna_templates`（npz→base64）+ 计数与导出时间），`load_bundle()` 做格式/版本校验；
    `import_templates()` 恢复模具库时把旧文件备份成 `.bak-时间戳`。
  - `database.py` 新增 `export_strategies()` / `export_strategy_versions()` /
    `import_strategy_bundle(bundle, mode)`：按 `code`（优先）/`name` 匹配做
    **合并**或**覆盖**；先落库再按 `旧id→新id` 映射回填 `parent_strategy_ids`
    与 legacy `parent_strategy_id`（旧库单值字段为空时用唯一父策略补齐），
    保证跨机器迁移后层级串联不断；孤儿版本（所属策略已删）计入 `versions_skipped`。
  - `ui/strategy_tab.py` 新增「📤 导出策略」「📥 导入策略」两个按钮：
    导出弹保存对话框并回报数量；导入先展示备份摘要，再让用户选合并/覆盖，
    覆盖需二次确认，完成后刷新列表并联动其它页签。
  - **仓库卫生**：`.gitignore` 增加 `archive/`、`策略备份-*.json`；
    执行 `git rm -r --cached archive`（本地文件保留），策略归档不再随提交上传。
  - 新增 `docs/策略迁移与导出导入.md`：策略存放位置、导入导出、换机器完整步骤与注意事项。
- **验证**：新增 `test_strategy_export.py` **30 项全部通过**（导出结构、全新库导入、
  公式/别名/层级/节奏/启用状态一致、父策略 id 重映射、重复导入为更新、覆盖导入清空、
  非法格式与超前版本被拒、DNA 模具往返含旧文件备份）。
  真实库实测：13 个策略（3 个 DNA + 4 个指标存档）、33 条历史版本、10 个模具，
  导出 83.3 KB；导入全新库后名称集合/公式/别名/父策略引用全部一致。

- **更新日期**：2026-11-24（v0.10.4）
- **更新原因**：用户希望「把自动扫描的数据交给 AI 处理，生成基于自己策略的综合日报再推送」
- **更新内容**：
  - 新增 `ai_summary.py`：任意 **OpenAI 兼容**端点（方舟 / DeepSeek / Moonshot / 本地 Ollama，
    改 Base URL 即可）生成日报点评。核心三点：
    - `SYSTEM_PROMPT` 注入用户策略语义（B1海选=白线>黄线且 J<13 的海选网、B2=次日确认、
      砖型/单针=形态辅助、B1十张图=DNA骨架+微观综合分≥80、买点定级 SB1>B1底仓），
      并强约束「纯文本 ≤500 字、只用给定数据、禁止编造、不给买卖点位」；
    - `build_brief()` 只喂汇总（日期/市场宽度/各策略命中数/每策略前 N 只/二层评分），
      并自动生成 **【跨策略共振】名单**（同一只票被 ≥2 个策略命中的 top10，
      是上下文里信息密度最高的一段），上限 4000 字，省 token；
      二层行若没有综合分（策略执行失败仍入库的情况）退回普通写法，避免出现「综合- 骨架-」；
    - `_post_completion()` 直连 `/chat/completions`，401/403/404/429 等翻成中文可操作提示；
      `_tidy()` 清掉模型偶尔带出的 Markdown 记号，保证飞书纯文本观感一致。
  - `ui/scan_center.py`：「自动日报」页新增 AI 配置两行（启用 / Base URL / 模型 /
    API Key / 额外要求 / **测试 AI**）；`_run_daily_report` 拆出 `_compose_and_push()`，
    把「AI 点评 → 落盘 → 推送」整体放工作线程，经 `daily_log_line` / `daily_doc_ready`
    / `report_sent` 信号回主线程，界面不卡；AI 失败自动退回纯数据版继续推送。
  - 配置键：`ai_enabled` / `ai_base_url` / `ai_api_key` / `ai_model` / `ai_max_tokens` /
    `ai_timeout` / `ai_prompt_extra`（`AIConfig.load/save`）。
  - 文档新增「五、AI 点评」章节（端点对照表、喂什么、怎么写、失败处理、密钥存放提醒）。
- **验证**：`test_daily_report.py` 85 → **112 项全部通过**，新增 27 项：endpoint 拼接、
  配置校验、`build_brief` 分层与截断、跨策略共振名单、二层无分数回退、
  请求端点/鉴权头/模型与消息体、System 提示含策略语义与防编造约束、Markdown 清理、
  token 用量解析、401 问候语、异常结构不崩、`test_ai`、AI 配置回填与保存。
  真实库 2026-11-23：上下文 1862 字 / 系统提示 805 字，共振名单识别出
  「300654 世纪天鸿：砖型图翻红、B2普通、B1十张图（二层）」等 10 组。

- **更新日期**：2026-11-24（v0.10.3）
- **更新原因**：用户提问「自动日报是只跑当天吗」——确认原实现 `start=end=今天` 后，暴露出
  三个空跑场景：① 法定节假日无当日K线 → 0 命中空报告；② 收盘数据未落地 → 每只票都因
  「当日无数据」被静默跳过；③ 断档不补。用户选择「自动回退到最近有数据的交易日」
- **更新内容**：
  - `daily_report.py` 新增 `resolve_scan_date(db, probe=None, today=None)` + `PROBE_CODES`：
    缓存有今天 → 用今天；否则探测 600519/000001/601318 的最新K线日期；探测到今天 → 用今天；
    只探测到更早日期 → **回退到该交易日**并在日志说明；探测全失败 → 用缓存最近日。
  - `database.py` 新增 `count_cached_bars_on(day)`：某日有多少只股票已有日K（判断数据落地）。
  - `ui/scan_center.py`：新增 `scan_date_ready` 信号与 `_probe_scan_date()`（**工作线程探测**，
    自带独立 `DataFeed` 连接，避免与 worker 线程抢 pytdx 连接）；`_start_daily_job` 改为
    「先定信号日再扫描」；`_on_scan_date_ready()` 落定 `_daily_day` 并提交扫描；
    `_on_auto_scan_done()` 回报当日日K覆盖度（<1000 只给「结果会偏少」提示）；
    `_run_daily_report(auto, day=None)` 支持显式日期（手动日报按钮仍取今天）。
  - 文档补充「信号日怎么定」小节。
- **验证**：`test_daily_report.py` 77 → **85 项全部通过**（新增：探测到上一交易日回退、
  探测到今天用今天、探测不可用按缓存最近日、探测抛异常不炸、缓存含今天走捷径、
  `count_cached_bars_on` 统计）。

- **更新日期**：2026-11-24（v0.10.2）
- **更新原因**：用户指出「我之前跑通的方式是 app_id + app_secret + receive_id」，说明他现在用的是
  **自建应用机器人**（Open API），而不是自定义机器人 webhook；`oc_...` 是群 chat_id 而非 hook token，
  v0.10.1 的「补全 Webhook」对其无效
- **更新内容**：
  - `feishu.py` 重构为双通道：
    - **应用机器人**：`get_tenant_token()`（`/open-apis/auth/v3/tenant_access_token/internal`，
      进程内缓存 + 提前 2 分钟过期 + token 失效自动重试一次）→
      `send_app_message()`（`/open-apis/im/v1/messages?receive_id_type=`，
      `content` 按 IM API 要求序列化为 **JSON 字符串**；支持 chat_id/open_id/user_id/email）。
    - **自定义机器人**：原 webhook 通道保留（含签名与地址补全）。
    - 统一入口 `send_message(creds, ...)`、`is_configured()`、`describe()`（脱敏）、`test_message()`；
      旧 `send_feishu`/`test_feishu` 保留兼容。
    - 错误码提示扩充：token 侧 10003/10014/40004；IM 侧 230001/230002/230006/230013/
      99991663‑72 等，均给中文可操作建议。
  - `daily_report.py`：配置新增 `mode`(app/webhook) / `app_id` / `app_secret` /
    `receive_id` / `receive_id_type`，settings 键 `feishu_mode` / `feishu_app_id` /
    `feishu_app_secret` / `feishu_receive_id` / `feishu_receive_id_type`；
    新增 `credentials()`；**老配置无 feishu_mode 时按是否有 webhook 推断**。
  - `ui/scan_center.py`：「自动日报」页新增「推送方式」下拉 + 应用机器人三行输入，
    按方式自动启用/置灰对应输入框；推送/测试改走 `send_message` / `test_message`。
  - `docs/自动日报与飞书推送.md`：重写配置章节为 A（应用机器人）/ B（Webhook）两套流程，
    补充权限开通、发布版本、拉群、chat_id 获取，以及新错误码排查表。
- **验证**：`test_daily_report.py` 51 → **77 项全部通过**，新增用例：应用机器人发送成功、
  先换 token 再发消息、token 请求参数、IM 地址与 Authorization 头、receive_id 透传、
  IM content 为 JSON 字符串、token 缓存不重复换取、纯文本走 text、230002 提示、
  凭证校验、describe 不泄露 secret、界面推送方式回填与启用关系、老配置推断 webhook。

- **更新日期**：2026-11-23（v0.10.1 修复）
- **更新原因**：用户实测「测试发送」报 `✗ 网络异常: Invalid URL 'oc_b41562840b49ca979df302e9a5acff5a':
  No scheme supplied` —— 只填了 webhook 末尾的 token，没有完整地址
- **更新内容**：
  - `feishu.py` 新增 `normalize_webhook()`：完整 URL / `open-apis/bot/v2/hook/xxx` /
    裸 token（`oc_xxx`）/ 缺协议域名 四种形态统一补全为
    `https://open.feishu.cn/open-apis/bot/v2/hook/...`；`_post` 入口统一调用。
  - `feishu.py` 新增飞书错误码 → 中文可操作提示（19001 地址不对 / 19003 机器人被移出群 /
    19021 签名校验失败 / 19022 IP 白名单 / 19024 关键词不匹配）。
  - `ui/scan_center.py`：保存配置时自动补全并回填输入框；输入框提示语改为
    「只粘最后那串 oc_xxx 也能自动补全」；`mask_webhook` 日志按补全后的地址打码。
  - `docs/自动日报与飞书推送.md` 补充该故障的排查表。
- **验证**：`test_daily_report.py` 44 → **51 项全部通过**（新增：裸 token 补全 / 只粘路径补全 /
  完整 URL 原样 / 缺协议补全 / 空白视为未配置 / 裸 token 直接发送走补全地址 / 错误码提示）。

- **更新日期**：2026-11-23
- **更新原因**：用户要求「可以设置定时每天自动进行全市场扫描，然后把所有扫描结果自动汇总成一个文档，
  通过飞书机器人发到飞书」，且功能放在选股模块下
- **更新内容**：
  - **新增「自动日报」子页（v0.10.0）**：`ui/scan_center.py` 由三子页扩为四子页；新页签含
    启用开关 / 执行时间 / 仅工作日 / 启动补跑 / 飞书 Webhook / 签名密钥 / 每策略条数 /
    内容开关 / 保存配置 / 立即扫描并推送 / 只生成日报并推送 / 运行日志。
  - **`daily_report.py`（新）**：`DailyReportConfig`（配置读写到 settings，键前缀 `daily_report_*`
    / `feishu_*`）、`build_daily_report`（按天汇总 scan_history → Markdown + 飞书行）、
    `should_fire`（定时判定，含 2 小时宽限与「启动补跑」）、`save_report`（写 `reports/日报-YYYY-MM-DD.md`）。
    同一策略当天多次扫描**只取最近一次会话**，避免重复计数。
  - **`feishu.py`（新）**：飞书自定义机器人推送，支持 `msg_type=post` 富文本 +
    可选签名校验（HMAC-SHA256 → base64），超 6000 字截断、失败自动降级纯文本重试；
    配置 `feishu_webhook` / `feishu_secret`。
  - **`workers.py`**：新增 `auto_scan_done` 信号，在 `auto_scan` 的 `finally` 中发出
    （含 mode/hits/日期区间/是否取消），供日报在「全部策略 + 二层串联」真正结束后收尾；
    `all_hits`/`mode` 提到 `try` 之前，异常时也能正常发信号。
  - **`database.py`**：新增 `get_market_breadth(day)`（市场宽度：涨跌家数/平均涨幅）；
    迁移新增表达式索引 `idx_scan_history_day(COALESCE(trade_date, date(scan_time)))`。
  - **`ui/scan_center.py` 配套改造**：`_run_auto` 支持 `start_date/end_date/silent`
    （定时任务固定信号日=当天、不弹提示框、不抢页签）；`_on_error` 在定时任务失败时复位状态；
    配置改为内存缓存，避免每 30 秒 tick 反复读库；飞书推送在后台线程执行，
    经 `report_sent` 信号回主线程。
  - **文档**：新增 `docs/自动日报与飞书推送.md`（配置步骤、定时规则、报告结构、常见问题）。
- **验证**：新增 `test_daily_report.py` **44 项全部通过**（定时判定 9 项 / 飞书消息体与签名 9 项 /
  日报汇总 11 项 / 落盘 2 项 / 配置读写 4 项 / 页签 offscreen 7 项）；
  `test_multi_scan.py` 11 项通过；`_smoke_v900/test_db_migration.py` 通过（旧库升级 + 真实库加载）；
  真实库 2026-11-23 生成日报：8 个策略、命中 953 只（一层 925 / 二层 28）、
  市场宽度 涨 4306 / 跌 780 / 均 +2.79%，文档 6084 B，耗时 0.6s；
  索引后按信号日查询 7.16s → 0.02s。
  注：`test_headless.py` 在 `test_scan` 处因**公开行情服务器不可用**（`get_quotes` 返回无 `pct` 列）
  中断，与本次改动无关（既有环境问题）。

- **更新日期**：2026-08-17
- **更新原因**：用户反馈观察池 Tab 有「较加入价%」，但一层粗筛和二层精筛 Tab 没有，要求统一展示
- **更新内容**：
  - **三 Tab 表现列统一（v0.7.6）**：抽出 `PerformanceListWidget` 通用组件替换原 HitListWidget /
    DnaHitListWidget / WatchPerfListWidget。三个子菜单均展示
    `现价 / 较加入价% / 当日涨跌% / 振幅%` 四列（红涨绿跌着色）
  - **加入价语义**：
    - 观察池 = 加入日 `add_date` 的日K收盘价（缓存，回退 `entry_price`）
    - 一层粗筛 / 二层精筛 = 命中日 `scan_history.price`（即信号当根 K 线收盘价）
    - fallback_to_base=True（粗筛/精筛命中日即当前最新价）/ False（观察池用最近日K）
  - **盘中 5 秒刷新统一**：监听 worker.quotes_ready 合并（全局信号无额外网络）+
    各自定时器仅在前台可见时 request_quotes 兜底；非交易时段不刷网络；refresh 时保留选中行
  - **验证**：test_calendar_ui.py 19 → **28 项全部通过**；test_headless.py 143 项全部通过；
    真实库 2026-08-17 一层粗筛 169 / 二层精筛 15 / 观察池 156 三 Tab 表现列均正常显示
    （粗筛现价 12.45 周大生/较加入价%0/当日涨跌%1.68/振幅%2.28；观察池现价 9.14 招商公路/较加入价%0/当日涨跌%-0.65/振幅%1.09）

- **更新日期**：2026-08-17
- **更新原因**：用户要求行情日历观察池显示「加入股票相比加入当天收盘价的涨跌幅度」，盘中打开还要显示波动（非实时，5 秒刷新）
- **更新内容**：
  - **观察池表现列（v0.7.5）**：行情日历 → 当日清单 → 观察池 Tab 重构为
    `来源|代码|名称|加入价|现价|较加入价%|当日涨跌%|振幅%|原因`（`ui/calendar_tab.py::WatchPerfListWidget`）
  - **较加入价%**：加入价 = 加入交易日（add_date）的日K收盘价（`database.get_close_on_date`，本地缓存；
    无缓存回退 `entry_price`）；较加入价% = (现价/加入价 − 1) × 100，红涨绿跌着色
  - **盘中波动 5 秒刷新**：复用观察池页签每 5 秒请求的全池行情信号（`worker.quotes_ready` 合并过滤），
    日历自身盘中再对当日股票请求兜底（覆盖已删除出池的历史票）；现价/当日涨跌%/振幅% 盘中实时跳动；
    **非交易时段不刷网络**，现价回退最近日K收盘、当日涨跌%/振幅% 回退最近一根日K（
    `database.get_latest_bar_snapshot`）
  - **细节**：刷新时保留选中行（按代码恢复选中）；返回月历停止定时器；非交易日/无K线时列留空不报错
  - **验证**：`test_calendar_ui.py` 11 → **19 项全部通过**（表现列/加入价=缓存收盘/初始较加入价%=0/
    行情到达后 现价10.5·较加入价%5·当日涨跌%5·振幅%7/盘中定时请求）；`test_headless.py` **143 项全部通过**；
    真实库 2026-08-17 观察池 **156 只**全部显示 加入价/较加入价%（现价=最新交易日收盘，较加入价%=0），
    当日涨跌%/振幅% 显示当日实盘数据（如 招商公路 -0.65% / 振幅1.09%）

- **更新日期**：2026-08-17
- **更新原因**：用户任务「修复并贯通行情日历-双层筛选与观察池流转引擎」——一层粗筛 169 只但二层精筛 0 只
- **更新内容**：
  - **修复 B008（核心问题）**：扫描工作流一层公式扫描完成后未自动串联执行二层 DTW 模具匹配与微观评分，
    二层数据缺失、未入库、UI 无法呈现。`workers.py` 在 `auto_scan`（全市场/定时盘中扫描）全部策略完成、
    `strategy_scan`（单策略 batch）完成后自动调用 `_chain_layer2`：
    跨策略去重候选 → 拉日K → DNA 三维特征 → 与 `golden_templates.npz` 模具库比对得骨架分 →
    `core/micro_rules.py::MicroRuleEvaluator` 微观加减分 + 买点定级 →
    `final_score = np.clip(dtw_score + score_modifier, 0, 100)` → 过滤 `>=75` 或综合分 Top15 →
    持久化 layer=2（`dtw_score/final_score/matched_template/buy_level/signals`）→ 自动进池并回写
    `scan_history.in_watchlist`。DNA 精筛策略流同步改用 `layer2_screen`（综合分口径统一）。
  - **数据库对齐（database.py）**：`scan_history` 迁移新增 7 列
    （`dtw_score/final_score/matched_template/buy_level/signals/in_watchlist/trade_date`）；
    `list_scan_history`/`list_scan_dates` 按 `COALESCE(trade_date, date(scan_time))` 过滤（旧数据兼容）；
    新增 `update_scan_in_watchlist` / `mark_session_in_watchlist`。
  - **行情日历三 Tab（ui/calendar_tab.py）**：① 一层粗筛 保持原列；② 二层精筛 重构为
    `序号|股票代码|股票名称|综合得分|骨架相似度|买点定级|最佳匹配模具|当日现价|涨跌幅%|微观特征诊断`，
    按综合得分降序，SB1核心买点 红色 / B1底仓买点 蓝色 高亮（`PandasModel.text_color_rules`）；
    ③ 观察池 当日进入清单。底部【加入观察池】（多选，Toast「已成功加入观察池」，watch_pool 落库 +
    in_watchlist 回写 + 顶部「观察池 X 只」计数实时 +1）/【查看K线】。
  - **选股模块**（ui/scan_center.py）：DNA 排行改展示综合得分/骨架相似度/买点定级/微观诊断。
  - **验证**：`golden_templates.npz` 正常加载（2 模具，feature_version=2 与引擎一致）；
    `backfill_layer2.py` 对 2026-08-17 粗筛 169 只（去重 156）执行二层精筛 → **15 只通过
    （6 SB1核心买点 / 9 B1底仓买点）**，耗时约 1.4s；日历计数「粗筛 169 / 精筛 15 / 观察池 156」
    与 Tab 数据、标题文字精准同步；加入观察池后计数 +1 实时生效。
  - 测试：`test_headless.py` 127 → **143 项全部通过**；新增 `test_calendar_ui.py`（offscreen 冒烟 11 项通过）。

- **更新日期**：2026-08-16
- **更新原因**：用户反馈双击结果表不弹 K 线图
- **更新内容**：
  - 修复 B007：K线图数据线程误传 `adjust="qfq"` 给 `CachedFeed.get_daily_bars`（该方法无此参数，
    v0.7.0 起内部即前复权）→ TypeError 被吞 → 对话框显示"获取失败"。已去掉该参数，
    实测真实库加载 800 根前复权日K正常
  - 补齐双击入口：观察池表格、选股模块命中历史表、日历清单（HitListWidget）均支持双击看K线
  - 回归 127 项全部通过；GUI 冒烟通过

- **更新日期**：2026-08-16
- **更新原因**：用户要求 K 线图副图增加内置指标「单针下30」与「砖型图」
- **更新内容**：
  - K线图副图新增「单针下30」（短期/中期/中长期/长期四线 + 85/30 参考线 + 四买点标记，
    与通达信副图一致；中期/中长期为 `:=` 赋值，从公式环境变量取值绘制）与
    「砖型图」（数值>0 红砖 / =0 绿砖柱状，与通达信副图一致）
  - 指标计算复用公式引擎 `evaluate_script` + 内置指标库定义（`strategy_presets.PRESET_INDICATORS`），
    任何周期（日/周/月/分钟）均可显示
  - 回归 127 项全部通过；GUI 冒烟通过

- **更新日期**：2026-08-16
- **更新原因**：用户要求开发并集成 K 线图功能（确认范围：周期切换+多指标叠加+全入口）
- **更新内容**：
  - **K线图升级**（`ui/chart_widget.py` 重写）：
    - 周期切换：日K（前复权）/ 周K / 月K / 5分 / 15分 / 30分 / 60分钟
    - 周/月K由前复权日K重采样（`resample_daily_bars`，周K=周五、月K=月末；复权口径与日K一致）
    - 分钟线用 pytdx 原生数据（`DataFeed.get_kline_bars`，category 0/1/2/3/7），UI 标注"未复权"
    - 主图指标开关：均线MA(5/10/20/60)、布林BOLL、黄白线YL/WL（仅日K）
    - 副图切换：MACD / KDJ / RSI / 无（成交量固定）
    - 数据异步拉取（QThread），标题栏显示最新价与涨跌幅，数据源标注（前复权/未复权）
  - **入口全铺**：观察池/命中列表「查看K线」直开新对话框；选股模块粗筛结果表、DNA排行榜、
    行情日历二层精筛表新增**双击行看K线**
  - 回归 127 项全部通过；GUI 冒烟通过；T005（指标画图）部分完成（周期+指标叠加，
    自定义公式叠加待后续）

- **更新日期**：2026-08-16
- **更新原因**：用户反馈①重复点击「立即扫描所选」导致后台任务堆积；②需要历史日期扫描做策略对照验证
- **更新内容**：
  - **防重复提交（v0.7.3）**：Worker 增加 `_scanning` 状态（strategy_scan/auto_scan/dna_scan 执行期间置位，
    try/finally 复位）；所有 `request_*` 在扫描运行中直接拒绝并返回 False；策略管理/选股模块
    提示「已有扫描在运行，请等待其完成后再试」——手误连点不再堆积任务
  - **历史日期扫描**：`scan_formula`/`parallel_scan_formula` 支持 `start_date/end_date`（对范围内
    每个交易日逐日判断，指标在全序列计算后逐日截断，结果按 `signal_date` 记录）；
    `dna_screen` 支持 `as_of_date`（窗口末端锚定指定日）；`scan_history` 新增 `signal_date` 列
  - **日期选择 UI**：策略管理「立即扫描所选」弹出信号日期对话框；选股模块工具栏加「信号日期」
    起止选择——默认=最新交易日，可选范围限制在日K缓存覆盖区间（**区间外日期置灰不可选**）
  - **历史对照扫描不自动进池**（信号已错过，进池无意义且污染观察池）；结果表新增「信号日期」列
  - 回归 127 项全部通过；GUI 冒烟通过

- **更新日期**：2026-08-16
- **更新原因**：用户实际使用反馈——策略库对话框多余、策略管理页与选股模块扫描功能理解疑问
- **更新内容**：
  - **删除「策略库」对话框**（`StrategyLibraryDialog`），所有策略/指标直接在主界面
    两个区域（策略列表 + 指标库）管理；内置导入简化为「📦 导入内置」按钮
    （`PresetImportDialog`，仅保留导入功能），放在策略列表标题行
  - 主栏按钮：恢复「✏️ 编辑」；新增**双击策略行直接编辑**
  - 主栏顺序：＋新增策略 | ✏️编辑 | 启用/停用 | 删除 | 🧬DNA模具 | 刷新 |
    🔍单票诊断 | 📖说明书 | 📜历史版本 | ▶立即扫描所选（用户确认保留，tooltip 说明
    与选股模块批量扫描的区别：单策略即时验证 vs 批量跑所有启用策略）
  - 回归 127 项全部通过；GUI 冒烟通过

- **更新日期**：2026-08-16
- **更新原因**：用户新增策略时 DNA 模具只显示一个且无法勾选；策略管理页按钮重新组织
- **更新内容**：
  - **修复 B006**：`_load_templates` 误用 `QListWidget.addItem()` 的返回值（Qt 该 API 返回
    None）→ 第一个模具 addItem 后 `setFlags` 抛 AttributeError 被信号槽静默吞掉，
    导致第二个模具未添加、第一个无勾选框。改为先创建 `QListWidgetItem` 再 addItem。
    实测两个模具（昂利康/澄天伟业）均正常显示可勾选
  - **策略管理页按钮重组**（用户指定顺序）：＋新增策略 | 启用/停用 | 删除 | 📚策略库 |
    🧬DNA模具 | 刷新 | 🔍单票诊断 | 📖说明书 | 📜历史版本 | ▶立即扫描所选；
    移除主栏「编辑」按钮
  - 「📦内置库」→「📚策略库」：`PresetLibraryDialog` 升级为 `StrategyLibraryDialog`，
    新增「我的策略」页签（策略表格 + 编辑/新增），编辑功能移入策略库
  - 「立即扫描所选」tooltip 明确与选股模块「盘前/盘后全市场扫描」的区别：
    前者=单个策略全市场深扫（约1分钟），后者=批量运行所有启用策略
  - 回归 127 项全部通过；GUI 冒烟通过

- **更新日期**：2026-08-16
- **更新原因**：用户重启后全市场扫描可运行但速度仍慢，且大量「复权告警」刷屏
- **更新内容**：
  - **修复 B005（扫描慢的真正根因）**：日K缓存新鲜度判定 bug——非交易时间 `days <= 1`
    把周五收盘的缓存误判过期（周末 days=2、周一盘前 days=3），导致**全市场每票重拉网络**。
    修复：非交易时间改为「缓存最后日期 >= 最近交易日（跳过周末）」。
    实测：全市场 5543 只 B1海选 从 ~15 分钟/策略 → **62 秒/策略**（10ms/只 缓存命中）
  - **复权告警阈值按板块涨跌停幅度动态化**：除权参考价 vs 开盘价偏差 ≤ 涨跌停幅度
    （主板10%/创业板科创板20%/北交所30% +2% 舍入缓冲）属除权日市场高开/低开（一字板
    可精确到涨跌停价），不再告警刷屏；超过幅度才告警。实测 000788/000880 等告警消除，
    且与 akshare 对照确认复权引擎本身正确（max 误差 <0.12%）
  - `core/adjust.py::compute_qfq_prices` 增加 `limit_pct` 参数；`data_feed.price_limit_pct(code)`
    按代码前缀返回涨跌停幅度
  - 回归 127 项全部通过；里程碑 v0.7.2

- **更新日期**：2026-08-16
- **更新原因**：用户点「选股模块 → 盘前/盘后全市场扫描」弹窗报错 `'bars_count'`
- **更新内容**：
  - 修复 B004：`request_auto_scan` 的队列 payload 缺少 `bars_count` 键，`_run_strategy_scan` 中
    `p["bars_count"]` 抛 KeyError → scan_error 弹窗。存量 bug（策略管理页「立即扫描所选」路径
    有该键所以未暴露）；`request_auto_scan` 增加 `bars_count=300` 参数并透传，
    `auto_scan` 分支补 `payload.get("bars_count", 300)` 兜底
  - 验证：auto_scan 多策略（B2普通/B1海选）全流程无报错；回归 127 项全部通过

- **更新日期**：2026-08-16
- **更新原因**：用户反馈全市场扫描/选股特别慢（十几分钟未完成）。排查发现三大性能问题：① 前复权切换后日K缓存全量重建（一次性）；② 并行扫描线程 `DataFeed()` 未传 db 导致除权事件每票重复拉取且不缓存；③ DNA 精筛为单线程且 fastdtw 纯 Python 受 GIL 限制
- **更新内容**：
  - 修复 `parallel_scan_formula` 线程内 `DataFeed(db=db)`（除权事件共享缓存，避免重复拉取）
  - 修复缓存口径污染：日K未拿到前复权（降级）时不写缓存，避免不复权数据混入 qfq 缓存
  - SQLite 开启 **WAL + busy_timeout=5000**（多线程扫描并发读写）
  - **numpy 窗口化 DTW** 替代 fastdtw 库（`core/dna_engine.py::windowed_dtw`）：语义一致（累积加权欧氏距离 Σd + 路径回溯，Sakoe-Chiba ±radius+长度差窗口），14x 加速；21 组真实数据分数与 fastdtw 版 **0.00 分差异**；同形100分/1%噪声96.6分校准不变；requirements 移除 fastdtw
  - **dna_screen 多线程并行**（workers 参数，每线程独立行情连接+共享缓存）；全市场DNA直测改 4 线程（`dna_fast` 模式）
  - 性能实测（240 只缓存票）：粗筛后精筛 1 线程 38.8s→19.2s；4 线程 32.9s→12.0s；全市场直测预计 15-20 分钟 → **~4-5 分钟**
  - 首次前复权缓存重建为一次性成本：全市场 5500 只日K拉取+复权约 15-20 分钟，完成后秒级
  - 里程碑 v0.7.2；测试 127 项全部通过；GUI 冒烟通过

- **更新日期**：2026-08-16
- **更新原因**：用户测试 DNA 模具制作器发现绿线（第3维）问题——强势股（昂利康 3 倍涨幅）偏离黄线 +30~50%，旧算法 ±10% 映射导致绿线长期贴 1.0 天花板，回踩白线动作完全丢失
- **更新内容**：
  - 维度3 特征算法升级 v2（`core/dna_engine.py`）：**动态支撑线**——股价在白线 WL 上方运行时以白线为支撑（灵敏反映回踩动作），跌破白线切换黄线 YL（破位看中期支撑）；映射范围 ±10% → **±25%**（`BIAS_SPAN=0.25`，0.5=踩线语义不变）
  - 实测对比（昂利康模具 29 天）：旧算法回踩日全贴顶 1.0（区分度 0.000），新算法回踩日 0.74~0.96（区分度 0.039~0.18），贴顶天数 26→18（剩余贴顶为"价格真实远离所有均线"的语义表达）
  - **模具口径版本管理**：`_feature_version` 字段（v1=黄线±10% / v2=动态支撑±25%），保存时自动标记；匹配时版本不符的旧模具自动跳过并提示重建，防止新旧特征混用比对（`DNAPatternMatcher.current_templates` / `scanner.dna_screen` 双重校验）
  - 演示模具「昂利康_经典B1缩量回踩」已用 v2 口径重建
  - UI：模具制作器绿线命名改为「支撑线贴合度」，提示注明语义（白线优先/破位切黄线）
  - 测试 124 → 127 项全部通过（新增：v2 回踩灵敏度、值域、横盘踩线≈0.5）；GUI 冒烟通过；里程碑 v0.7.1

- **更新日期**：2026-08-16
- **更新原因**：用户确认实施二层 DNA 精筛（多维 FastDTW 形态匹配）+ 前复权全量切换 + 选股模块改造
- **更新内容**：
  - 前复权引擎 `core/adjust.py`：pytdx `get_xdxr_info` 自算复权因子（实测确认每10股口径：fenhong/songzhuangu/peigu ÷10、peigujia 不除；因子方向=除以 rate）；全量切换 `bars_adjust=qfq`（首次迁移清空旧日K缓存）；`test_adjust.py` 三层监测 7 样本全过（akshare 对照 max 误差 0.006%~0.159%）
  - DNA 引擎 `core/dna_engine.py`：黄白线 YL/WL + 三维无量纲特征 + 加权 FastDTW（weights 0.5/0.3/0.2、radius=4、Score=100·e^(−3.5·dist/len)）；模具库 `dna_storage/`（golden_templates.npz + templates_meta.json）；候选窗口按模具长度对齐 + 末端锚定最新交易日 + 长度弹性扫描 15~40 天取最高分；全市场直测自动快速模式（单档）
  - 数据库迁移 v0.7.0：`xdxr_events` 表、`scan_history` 加 layer/dna_score/dna_template/dna_detail、`strategies` 加 parent_strategy_id（精筛从属粗筛）、`watch_pool` 加 dna_score/dna_template
  - UI：策略管理页「🧬 DNA模具」制作器（`ui/dna_builder_dialog.py`，pyqtgraph 三维曲线核验）；「DNA精筛」策略类型（模具多选/阈值/窗口模式/长度弹性/从属粗筛策略）；扫描中心→「🔍 选股模块」（上粗筛下精筛，对最近粗筛结果精筛/全市场DNA直测）；行情日历当日清单三子菜单（粗筛/精筛含DNA分数/观察池）；观察池与命中历史展示 DNA 列
  - 自动进池标注来源层（粗筛/精筛）与DNA分数；精筛命中更新已有观察池记录的 DNA 标注（`update_watch_dna`）
  - 新增依赖 fastdtw；测试 109 → 124 项全部通过；里程碑 v0.7.0；offscreen GUI 冒烟通过
  - 新增关键决策 D016（前复权全量切换/自算引擎+三层监测）、D017（候选窗口=模具长度+末端锚定+长度弹性）、D018（精筛策略从属粗筛策略+可独立全市场直测）
  - 新增文档 `docs/数据源与复权策略.md`（前复权=高优先级硬性要求，后续 Agent 开发必读）

- **更新日期**：2026-08-16
- **更新原因**：用户确认继续实施 T015 行情日历（替换实时行情页签）
- **更新内容**：
  - 新增 `ui/calendar_tab.py`（月历：淡红背景+命中数+悬停摘要 → 每日选股清单）与 `ui/hit_list.py`（统一命中列表组件）
  - 行情快照服务化：扫描中心维护全市场快照（90s 新鲜度），盘中候选排序不再依赖行情页
  - 删除 `ui/market_tab.py`；清理 warm_vol5/get_vol5/vol5 数据库方法（vol5_cache 表保留兼容）
  - 测试 108 → 109 项全部通过；里程碑 v0.6.6；T015 完成；offscreen GUI 冒烟通过

- **更新日期**：2026-08-16
- **更新原因**：用户拍板 Q3（提醒联动 = B 公式快照），并追加「策略编号 + 迭代版本归集」需求
- **更新内容**：
  - 策略固定编号 S0001...（创建分配/删除不回收/改名不变），存量策略迁移时补号
  - `strategy_versions` 版本表 + `archive/strategies/{编号}/` 文件夹归档 + 版本历史/对比 UI（T011 完成）
  - 提醒设置移至第 3 位 + 「策略信号」提醒类型（公式快照+编号版本）（T016 完成）
  - 修复 B003（提醒 params JSON 未解析导致提醒检查抛异常）
  - 测试 97 → 108 项全部通过；里程碑 v0.6.5；决策 D014/D015
  - README 与 docs 三篇文档同步更新

- **更新日期**：2026-08-15
- **更新原因**：用户明确系统定位（辅助大A监测系统，非看盘软件），提出界面功能方向调整：实时行情→行情日历、提醒设置移至第3位并联动策略、询问指标库作用
- **更新内容**：
  - 新增 `docs/界面功能方向.md`：六页签功能方向定义 + 行情日历详细设计 + 提醒策略联动设计 + 指标库定位说明 + 3 个待确认问题
  - 新增待办 T015（行情日历）/ T016（提醒设置联动+位置）
  - 新增关键决策 D010（行情页→行情日历）/ D011（提醒设置第3位+策略信号联动）
  - 里程碑 v0.6.4（纯文档，无代码变更）

- **更新日期**：2026-08-15
- **更新原因**：用户拍板移除可视化条件编辑器（策略均为先想好再开发，无需图形化拼条件）
- **更新内容**：
  - 移除 `cond_builder.py` / `ui/cond_builder_widget.py`，策略对话框只剩「公式 / 指标存档」类型
  - 存量 visual 策略自动迁移为 formula（database.py `_migrate`），scanner/strategy_tab 简化
  - 测试更新：105 → 97 项全部通过
  - 新增关键决策 D009；里程碑 v0.6.3；T012 更新（可视化条件问题已解决，剩 3 个问题）
  - README 与 docs 两篇框架文档同步更新

- **更新日期**：2026-08-15
- **更新原因**：用户提出需要一种直观全面的方法看清现有全部功能、判断哪些真正需要
- **更新内容**：
  - 新增 `docs/功能盘点与用户故事地图.md`：57 项功能清单（F001-F057）+ 盘前/盘中/盘后用户故事地图 + 需求追溯矩阵 + 功能埋点方案
  - 新增待办 T014（功能使用埋点，待用户确认实施）
  - 新增关键决策 D008（功能盘点方法体系：清单+故事地图+追溯矩阵+埋点+MoSCoW）
  - 里程碑 v0.6.2（纯文档，无代码变更）

- **更新日期**：2026-08-15
- **更新原因**：用户提出梳理「策略管理」功能框架，确认现有功能是否真实需要
- **更新内容**：
  - 新增 `docs/策略管理功能框架.md`：现状盘点 + 错位分析（P1-P7）+ 5 域建议框架 + 4 个待拍板问题
  - 新增待办 T009（参数化模板）/ T010（命中走势跟踪）/ T011（版本历史）/ T012（方向确认）/ T013（节奏解耦）
  - 新增关键决策 D007（策略管理五域框架）
  - 新增已知问题 I005（指标库无消费端）
  - 里程碑 v0.6.1（纯文档，无代码变更）

- **更新日期**：2026-08-15
- **更新原因**：用户提出需要在多模型/多对话间传递项目进度，降低 Token 成本
- **更新内容**：
  - 创建 `PROJECT_LOG.md`
  - 加入 YAML frontmatter，使项目状态、待办、里程碑、关键决策、已知问题均可被脚本解析
  - 保留 Markdown 正文作为人类可读视图
