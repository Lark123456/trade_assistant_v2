"""验证修复后的 TDX_SERVERS 能否拉到 000636 等真实股票日K。"""
import logging
from datetime import datetime

from data_feed import DataFeed, infer_market
from database import Database
from config import DB_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("verify")


def main():
    db = Database(DB_PATH)
    feed = DataFeed(db=db)
    log.info("当前行情服务器池：%s", feed.servers)

    # 1) 拉单只股票日 K —— 000636 (风华高科，原错误日志里的标的)
    code = "000636"
    market = infer_market(code)
    log.info("尝试拉取 %s (market=%d) 日K...", code, market)
    bars = feed.get_daily_bars(code, market=market, count=10)
    if bars is None or bars.empty:
        log.error("[FAIL] %s 拉取失败，返回空数据", code)
    else:
        log.info("[OK]   %s 拉到 %d 根日K", code, len(bars))
        log.info("最新 3 根:\n%s", bars.tail(3).to_string(index=False))
        last_date = str(bars["date"].iloc[-1])
        last_close = float(bars["close"].iloc[-1])
        log.info("最后一根K线日期=%s 收盘=%.3f", last_date, last_close)

    # 2) 拉实时行情确认协议握手
    log.info("尝试拉取实时行情 600000...")
    quotes = feed.get_quotes(["600000", "000636"])
    if quotes is None or quotes.empty:
        log.error("[FAIL] 实时行情拉取失败")
    else:
        log.info("[OK] 实时行情 %d 只:\n%s",
                 len(quotes), quotes[["code", "name", "price", "pct"]].to_string(index=False))

    # 3) 拉除权除息事件，验证 cache_daily_bars 完整链路
    log.info("尝试拉取 000636 除权除息事件...")
    events = feed.get_xdxr_events(market=infer_market("000636"), code="000636")
    if events is None:
        log.error("[FAIL] 除权事件拉取失败")
    else:
        log.info("[OK] 除权事件 %d 条", len(events))
        if events:
            log.info("最新 3 条: %s", events[-3:])

    feed.disconnect()
    log.info("修复验证完毕。")


if __name__ == "__main__":
    main()