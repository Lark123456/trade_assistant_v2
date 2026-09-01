# -*- coding: utf-8 -*-
"""前复权引擎自检（三层监测体系，v0.7.0）。

监测层级：
1. 除权参考价自检：ref=(pre_close-fenhong+peigujia*peigu)/(1+songzhuangu+peigu)
   vs 除权日实际开盘价，偏差 >5% 告警（1~5% 为市场高开/低开，属正常）
2. 除权日连续性：复权后除权日相邻交易日涨跌幅应接近自然涨跌（无跳空）
3. 最新价锚定：前复权最新收盘 == 原始最新收盘
4. 外部对照：与 akshare 权威 qfq 源抽样对比（腾讯源 stock_zh_a_daily，
   归一化后最大相对误差 <1% 通过；akshare 不可用时标记 SKIP 不阻塞）

用法：python test_adjust.py
退出码：0=全部通过（或外部对照 SKIP），1=存在失败项。
"""
import sys

SAMPLES = [
    (0, "002940", "昂利康"),     # 送转+派息混合
    (1, "600519", "贵州茅台"),   # 大额派息
    (1, "600036", "招商银行"),   # 常规派息
    (0, "000001", "平安银行"),   # 历史送转+配股
    (0, "300750", "宁德时代"),   # 派息
    (0, "002594", "比亚迪"),     # 派息
    (1, "601318", "中国平安"),   # 派息
]

CONTINUITY_TOL = 0.05   # 除权日复权后相邻涨跌容差 5%
AK_MAX_ERR = 0.01       # akshare 对照归一化最大相对误差容差 1%


def check_stock(market, code, name):
    from data_feed import DataFeed
    from core.adjust import compute_qfq_prices

    feed = DataFeed()
    try:
        raw = feed.get_daily_bars(code, market=market, count=800, adjust="none")
        events = feed._get_qfq_events(code, market)
    finally:
        feed.disconnect()

    if raw is None or raw.empty or len(raw) < 60:
        return {"ok": False, "reason": "pytdx 无数据"}

    ours, issues = compute_qfq_prices(raw, events)
    fails = []
    warns = []

    # 1. 除权参考价自检（issues 即告警）
    for msg in issues:
        (warns if "偏差" in msg else fails).append(msg)

    # 2. 除权日连续性：找除权事件日，检查复权后相邻涨跌
    from core.adjust import filter_price_events
    for e in filter_price_events(events):
        idxs = ours.index[ours["date"] == e["date"]]
        if len(idxs) == 0 or idxs[0] == 0:
            continue
        idx = idxs[0]
        prev_close = ours["close"].iloc[idx - 1]
        if prev_close <= 0:
            continue
        chg = abs(ours["close"].iloc[idx] / prev_close - 1)
        if chg > CONTINUITY_TOL:
            fails.append(f"{e['date']} 除权日复权后跳空 {chg * 100:.2f}%")

    # 3. 最新价锚定
    last_raw = float(raw["close"].iloc[-1])
    last_ours = float(ours["close"].iloc[-1])
    if last_raw > 0 and abs(last_ours - last_raw) / last_raw > 1e-6:
        fails.append(f"最新价锚定失败 {last_ours} vs {last_raw}")

    # 4. akshare 外部对照
    ak_status = "SKIP"
    try:
        import akshare as ak
        prefix = "sh" if int(market) == 1 else "sz"
        ak_df = ak.stock_zh_a_daily(symbol=prefix + code, adjust="qfq")
        ak_df["date"] = ak_df["date"].astype(str)
        m = ours[["date", "close"]].merge(
            ak_df[["date", "close"]], on="date", suffixes=("_ours", "_ak"))
        if len(m) < 60:
            ak_status = "SKIP(共同天数不足)"
        else:
            no = m["close_ours"] / m["close_ours"].iloc[-1]
            na = m["close_ak"] / m["close_ak"].iloc[-1]
            rel = (no - na).abs() / na
            max_err = float(rel.max())
            if max_err > AK_MAX_ERR:
                fails.append(f"akshare对照最大误差 {max_err * 100:.3f}% > 1%")
            ak_status = f"max {max_err * 100:.3f}%"
    except Exception as e:  # noqa: BLE001
        ak_status = f"SKIP({type(e).__name__})"

    return {"ok": not fails, "fails": fails, "warns": warns,
            "ak": ak_status, "events": len(filter_price_events(events))}


def main():
    print("=" * 60)
    print("前复权引擎自检（三层监测）")
    print("=" * 60)
    all_ok = True
    for market, code, name in SAMPLES:
        r = check_stock(market, code, name)
        tag = "✅ PASS" if r["ok"] else "❌ FAIL"
        if not r["ok"]:
            all_ok = False
        print(f"\n{tag} {name}({code}) 除权事件{r.get('events', '?')}个 | akshare对照: {r.get('ak')}")
        for w in r.get("warns", []):
            print(f"  ⚠️  {w}")
        for f in r.get("fails", []):
            print(f"  ❌ {f}")
    print("\n" + "=" * 60)
    print("结论:", "全部通过" if all_ok else "存在失败项")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
