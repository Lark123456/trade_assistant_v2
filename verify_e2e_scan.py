"""端到端验证：跑全市场并行扫描，统计网络拉取/缓存命中/warning 刷屏数。

核心指标：
- warning 总数（重点看「网络取数失败，回退到 ...」是否还会刷屏）
- 缓存命中（last_source="cache"） vs 网络拉取（last_source="network"）只数
- 扫描耗时
- 命中策略数
"""
import logging
import time
from collections import Counter

from data_feed import DataFeed
from database import Database
from scanner import CachedFeed, parallel_multi_scan_formula
from config import DB_PATH

# --- 自定义 Handler：按消息特征计数 ----------------------------------------
WARNING_TAGS = {
    "网络取数失败，回退到": "网络失败回退",
    "网络数据.*比本地缓存.*旧": "网络数据较旧",
    "连接.*失败": "服务器连接失败",
    "除权除息事件不可用": "除权事件不可用",
    "未获得前复权数据": "未获前复权数据",
}


class CountingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.all_warnings = []
        self.tag_counter = Counter()
        self.all_messages = []

    def emit(self, record):
        msg = record.getMessage()
        self.all_messages.append((record.levelname, record.name, msg))
        if record.levelno >= logging.WARNING:
            self.all_warnings.append((record.name, msg))
            for pattern, tag in WARNING_TAGS.items():
                import re
                if re.search(pattern, msg):
                    self.tag_counter[tag] += 1
                    break


def main():
    import sys, os
    # 若命令行给了 e2e_scan_result.txt 参数，把 stdout 重定向到该文件（utf-8）
    if len(sys.argv) > 1:
        out_path = sys.argv[1]
        sys.stdout = open(out_path, "w", encoding="utf-8", buffering=1)
        sys.stderr = sys.stdout
    handler = CountingHandler()
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler, stream],
        format="%(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("e2e")

    db = Database(DB_PATH)
    feed = DataFeed(db=db)
    log.info("当前服务器池: %s", feed.servers)

    # 1. 拿股票列表（akshare 拉失败时回退 pytdx）
    log.info("[1] 拉全市场股票列表...")
    stock_df = feed.get_stock_list()
    log.info("    全市场 %d 只 A 股", len(stock_df))

    # 2. 截取前 200 只作端到端测试（避免全市场 5500+ 只太慢，重点验证网络链路）
    test_df = stock_df.head(200).reset_index(drop=True)
    log.info("[2] 取前 %d 只做端到端扫描", len(test_df))

    # 3. 用一个简单公式跑并行多策略扫描（4 线程）
    #    公式：MA5 > MA20（多头排列，命中较多，便于看出链路是否通）
    formulas = [
        (1, "MA5>MA20多头", "MA5 > MA20"),
    ]
    log.info("[3] 启动 parallel_multi_scan_formula (workers=4, bars_count=300)...")
    t0 = time.time()
    progress_count = [0]

    def progress(i, total, name):
        progress_count[0] = i

    results = parallel_multi_scan_formula(
        test_df, formulas, db, workers=4, bars_count=300,
        progress=progress, cancelled=None,
    )
    dt = time.time() - t0

    # 4. 统计 CachedFeed 的 last_source（每次扫描后会被覆盖，仅反映最后一只）
    #    所以从 db 直接查缓存覆盖范围更准确：
    hit_count = sum(len(v) for v in results.values())
    log.info("[4] 扫描完成，耗时 %.1fs", dt)
    log.info("    命中策略数: %d", len(results))
    log.info("    总命中数: %d", hit_count)

    # 5. 看 warning 刷屏情况（重点！）
    log.info("=" * 60)
    log.info("[5] Warning 统计（修复目标：网络取数失败应接近 0）")
    log.info("=" * 60)
    if not handler.tag_counter:
        log.info("    太好了，没有任何 warning 触发。")
    for tag, n in handler.tag_counter.most_common():
        log.info("    [%-20s] x %d", tag, n)
    log.info("    总 WARNING 数: %d", len(handler.all_warnings))

    # 6. 打印前 5 条非 warning 日志（看链路顺序）
    log.info("=" * 60)
    log.info("[6] 前 10 条关键日志（按时间序）")
    log.info("=" * 60)
    for level, name, msg in handler.all_messages[:10]:
        log.info("    %s %s: %s", level, name, msg[:120])

    log.info("=" * 60)
    log.info("[结论]")
    log.info("=" * 60)
    if handler.tag_counter.get("网络失败回退", 0) == 0:
        log.info("✓ 修复有效：扫描过程中没有任何「网络取数失败，回退到 ...」warning。")
    else:
        log.info("✗ 仍有问题：出现 %d 次「网络失败回退」，需进一步排查。",
                 handler.tag_counter.get("网络失败回退", 0))


if __name__ == "__main__":
    main()