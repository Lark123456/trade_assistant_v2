# -*- coding: utf-8 -*-
"""东财板块同步（v0.11.0）：行业 / 概念 / 地域 → board / stock_board。

数据源：东方财富数据中心「核心题材-板块归属」报表 RPT_F10_CORETHEME_BOARDTYPE。
        该报表按全市场分页，一次就能拿到全部「股票-板块」归属，无需逐板块抓成分。

为什么不用 push2 行情接口：
    push2*.eastmoney.com 的 clist 接口在部分网络下会被服务端直接断连
    （TCP/TLS 握手成功，请求发出后无任何响应）。数据中心接口走独立域名，
    且分页总量可控（约 10 万条 / 106 页），更适合作为稳定主源。

字段说明：
    NEW_BOARD_CODE = BKxxxx          板块代码
    BOARD_NAME     = 名称
    BOARD_TYPE     = 行业 / 板块 / 空  空值表示概念、指数、风格等，东财归入概念
    BOARD_LEVEL    = 行业层级 1/2/3

用法：
    python board_sync.py                    # 增量：7 天内同步过则整体跳过
    python board_sync.py --full             # 全量重拉
    python board_sync.py --type concept     # 只同步概念
    python board_sync.py --type industry    # 只同步行业
    python board_sync.py --limit 5 --dry-run  # 调试：只看前 5 个板块，不写库
    python board_sync.py --proxy off        # 不走系统代理（默认 auto）

写入策略：
    - 全量数据先拉到内存，校验通过后在一个事务里写入（失败整体回滚）；
    - 单个板块成分异常（旧数据 >=20 只、本次返回 0 只）时保留旧数据；
    - 板块从接口消失时只置 is_active=0，不删除映射；
    - 全量同步时成分总数暴跌超过 30% 则中止写入；
    - 每次同步写一条 board_sync_log。
"""
from __future__ import annotations

import argparse
import datetime
import logging
import re
import sys
import time

import requests

from config import DB_PATH
from database import Database

log = logging.getLogger("board_sync")

SOURCE = "em"
REPORT = "RPT_F10_CORETHEME_BOARDTYPE"
COLUMNS = "SECURITY_CODE,NEW_BOARD_CODE,BOARD_NAME,BOARD_TYPE,BOARD_LEVEL"
DC_HOSTS = (
    "datacenter-web.eastmoney.com",
    "datacenter.eastmoney.com",
)
PAGE_SIZE = 1000
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BOARD_CODE_RE = re.compile(r"^BK\d+$")
A_SHARE_PREFIX = ("60", "68", "00", "30", "4", "8", "92")
# 东财 BOARD_TYPE → 本地 board_type
TYPE_MAP = {"行业": "industry", "板块": "region"}
# 东财行业为三级（1 宽 / 2 中 / 3 细），每只票各属一个；
# 本地「一票一行业」只保留二级，避免同一只票被打上三条重复行业标签
INDUSTRY_LEVEL = "2"


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _is_a_share(code):
    return (len(code) == 6 and code.isdigit()
            and code.startswith(A_SHARE_PREFIX))


def _fresh(ts, days):
    if not ts:
        return False
    try:
        dt = datetime.datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return False
    return (datetime.datetime.now() - dt).total_seconds() < max(0, days) * 86400


def _classify(em_type, em_level):
    """东财 BOARD_TYPE 归一化为 industry / region / concept。

    行业只取二级（INDUSTRY_LEVEL），一三级行返回 None 表示丢弃；
    空 BOARD_TYPE 是概念、指数、风格等，东财统一归入概念板块。
    """
    t = (em_type or "").strip()
    if t == "行业":
        return ("industry" if str(em_level or "").strip() == INDUSTRY_LEVEL
                else None)
    return TYPE_MAP.get(t, "concept")


class EastmoneyClient:
    """东财数据中心报表客户端：主机轮询 + 代理模式切换 + 重试。"""

    def __init__(self, proxy="auto", timeout=20, retries=4, rate=0.15):
        self.timeout = max(5, float(timeout))
        self.retries = max(1, int(retries))
        self.rate = max(0.0, float(rate))
        self._sessions = {}
        self._mode_order = []
        if proxy in ("on", "auto"):
            s = requests.Session()
            s.trust_env = True
            self._sessions["proxy"] = s
            self._mode_order.append("proxy")
        if proxy in ("off", "auto"):
            s = requests.Session()
            s.trust_env = False
            self._sessions["direct"] = s
            self._mode_order.append("direct")
        for s in self._sessions.values():
            s.headers.update({"User-Agent": USER_AGENT,
                              "Referer": "https://data.eastmoney.com/"})
        self._preferred = None
        self._host_idx = 0

    def _request(self, params):
        errors = []
        for attempt in range(self.retries):
            modes = []
            if self._preferred:
                modes.append(self._preferred)
            modes += [m for m in self._mode_order if m != self._preferred]
            mode = modes[attempt % len(modes)]
            host = DC_HOSTS[(self._host_idx + attempt) % len(DC_HOSTS)]
            try:
                r = self._sessions[mode].get(
                    f"https://{host}/api/data/v1/get",
                    params=params, timeout=self.timeout)
                r.raise_for_status()
                j = r.json()
                if not isinstance(j, dict):
                    raise ValueError("返回不是 JSON 对象")
                if j.get("success") is False:
                    msg = str(j.get("message") or "")
                    if "报表配置不存在" in msg:
                        raise RuntimeError(msg)
                    # 「返回数据为空」等属于正常空页，交给调用方处理
                    return j
                self._preferred = mode
                self._host_idx = (self._host_idx + attempt) % len(DC_HOSTS)
                return j
            except Exception as e:  # noqa: BLE001
                errors.append(f"{mode}/{host}: {type(e).__name__}: {str(e)[:80]}")
                time.sleep(min(6.0, 0.8 * (attempt + 1)))
        raise RuntimeError("东财数据中心请求失败；" + " | ".join(errors[-4:]))

    def fetch_pairs(self, progress=None):
        """拉取全市场「股票-板块」归属。

        返回 (rows, api_count)。rows 为接口原始 dict 列表。
        """
        params = {
            "reportName": REPORT,
            "columns": COLUMNS,
            "pageSize": PAGE_SIZE,
            "pageNumber": 1,
            # 必须联合排序：只按 SECURITY_CODE 排序时，同一只股票的多行会在
            # 分页边界漂移，实测造成 459 条重复并漏行
            "sortColumns": "SECURITY_CODE,NEW_BOARD_CODE",
            "sortTypes": "1,1",
        }
        j = self._request(params)
        result = j.get("result") or {}
        pages = int(result.get("pages") or 0)
        api_count = int(result.get("count") or 0)
        rows = list(result.get("data") or [])
        if progress:
            progress(f"分页 1/{max(1, pages)}，累计 {len(rows)} 条")
        for pn in range(2, pages + 1):
            params["pageNumber"] = pn
            j = self._request(params)
            rows.extend((j.get("result") or {}).get("data") or [])
            if progress and (pn % 10 == 0 or pn == pages):
                progress(f"分页 {pn}/{pages}，累计 {len(rows)} 条")
            time.sleep(self.rate)
        return rows, api_count


def _load_stock_codes(db):
    try:
        return {r["code"] for r in db.load_stock_list()}
    except Exception as e:  # noqa: BLE001
        log.debug("读取 stock_list 失败，退回代码前缀过滤: %s", e)
        return set()


def _last_synced_by_type(db):
    """各类板块的最近一次同步时间。"""
    out = {}
    for b in db.list_boards(source=SOURCE):
        ts = b.get("last_synced_at")
        if ts and ts > out.get(b["board_type"], ""):
            out[b["board_type"]] = ts
    return out


def sync_boards(db, board_types=("industry", "concept", "region"), full=False,
                days=7, proxy="auto", timeout=20, retries=4, rate=0.15,
                limit=0, dry_run=False, progress=print):
    """同步东财板块。返回统计 dict；异常时写 failed 日志并抛出。"""
    started_at = _now()
    wanted = set(board_types)

    # 增量：本次要求的类型都在 days 天内同步过，则整体跳过
    if not full and not limit:
        last = _last_synced_by_type(db)
        if all(_fresh(last.get(bt), days) for bt in board_types):
            progress(f"全部 {len(board_types)} 类板块在 {days} 天内已同步，跳过"
                     f"（需要重拉请加 --full）")
            return {"skipped": True, "boards_seen": 0, "boards_refreshed": 0,
                    "boards_failed": 0, "members_total":
                    db.get_board_member_total(source=SOURCE),
                    "boards_deactivated": 0, "members_added": 0,
                    "members_removed": 0, "dry_run": dry_run, "failures": []}

    client = EastmoneyClient(proxy=proxy, timeout=timeout,
                             retries=retries, rate=rate)
    stock_codes = _load_stock_codes(db)
    progress(f"\n== 拉取东财全市场板块归属（{REPORT}）==")
    try:
        rows, api_count = client.fetch_pairs(progress=progress)
    except Exception as e:  # noqa: BLE001
        db.add_board_sync_log(SOURCE, started_at, "failed", 0, 0, 0, 0,
                              f"{type(e).__name__}: {e}")
        raise
    if not rows:
        msg = "东财板块归属接口返回空数据，放弃本次同步"
        db.add_board_sync_log(SOURCE, started_at, "failed", 0, 0, 0, 0, msg)
        raise RuntimeError(msg)
    progress(f"接口返回 {api_count} 条归属、{len(rows)} 行明细")

    seen_map, members = {}, {}
    for r in rows:
        bcode = str(r.get("NEW_BOARD_CODE") or "").strip()
        name = str(r.get("BOARD_NAME") or "").strip()
        if not BOARD_CODE_RE.match(bcode) or not name:
            continue
        bt = _classify(r.get("BOARD_TYPE"), r.get("BOARD_LEVEL"))
        if bt is None:
            continue
        key = (bt, bcode)
        seen_map[key] = {"board_type": bt, "source_code": bcode, "name": name}
        code = str(r.get("SECURITY_CODE") or "").strip().zfill(6)
        if _is_a_share(code) and (not stock_codes or code in stock_codes):
            members.setdefault(key, set()).add(code)

    existing = {(b["board_type"], b["source_code"]): b
                for b in db.list_boards(source=SOURCE, active_only=False)}
    seen, refreshed, failures = [], [], []
    for key in sorted(seen_map):
        if key[0] not in wanted:
            continue
        board = seen_map[key]
        seen.append(board)
        codes = sorted(members.get(key, ()))
        old = existing.get(key)
        if old and (old.get("stock_count") or 0) >= 20 and not codes:
            failures.append((board, "返回 0 只成分，疑似接口异常，保留旧数据"))
            continue
        refreshed.append({**board, "codes": codes})

    if limit and limit > 0:
        seen = seen[:limit]
        keep = {(b["board_type"], b["source_code"]) for b in seen}
        refreshed = [b for b in refreshed
                     if (b["board_type"], b["source_code"]) in keep]

    progress(f"解析得到 {len(seen)} 个板块，其中可取成分 {len(refreshed)} 个，"
             f"异常 {len(failures)} 个")

    new_total = sum(len(b["codes"]) for b in refreshed)
    if full and not limit and not dry_run:
        prev_total = sum(db.get_board_member_total(source=SOURCE, board_type=bt)
                         for bt in board_types)
        if prev_total >= 1000 and new_total < prev_total * 0.7:
            msg = (f"成分归属总数异常下降（{prev_total} → {new_total}），"
                   f"已中止写入，旧数据保持不变")
            db.add_board_sync_log(SOURCE, started_at, "failed", 0,
                                  len(failures), 0, 0, msg)
            raise RuntimeError(msg)

    result = {
        "boards_seen": len(seen),
        "boards_refreshed": len(refreshed),
        "boards_failed": len(failures),
        "members_total": new_total,
        "dry_run": dry_run,
        "failures": failures,
    }

    if dry_run:
        progress(f"\n[dry-run] 板块 {len(seen)}，可取成分 {len(refreshed)}，"
                 f"失败 {len(failures)}，成分归属 {new_total} 条；未写库")
        return result

    deactivate = () if limit else tuple(board_types)
    stats = db.apply_board_snapshot(SOURCE, seen, refreshed,
                                    deactivate_types=deactivate)
    status = "ok" if not failures else ("partial" if refreshed else "failed")
    message = ""
    if failures:
        message = "；".join(f"{b['name']}({b['source_code']}): {err}"
                            for b, err in failures[:5])
        if len(failures) > 5:
            message += f"；…… 其余 {len(failures) - 5} 个"
    db.add_board_sync_log(SOURCE, started_at, status, stats["boards_refreshed"],
                          len(failures), stats["members_added"],
                          stats["members_removed"], message)
    result["members_refreshed_total"] = new_total
    result["members_total"] = db.get_board_member_total(source=SOURCE)
    result.update(stats)
    result["status"] = status
    return result


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="东财板块（行业/概念/地域）同步")
    p.add_argument("--type", choices=("all", "industry", "concept", "region"),
                   default="all", help="同步类型，默认行业+概念+地域")
    p.add_argument("--full", action="store_true",
                   help="全量重拉（默认增量：N 天内同步过则跳过）")
    p.add_argument("--days", type=int, default=7,
                   help="增量模式下，N 天内同步过则跳过（默认 7）")
    p.add_argument("--limit", type=int, default=0,
                   help="调试：最多同步 N 个板块（不主动下线板块）")
    p.add_argument("--rate", type=float, default=0.15,
                   help="每页请求后的间隔秒数（默认 0.15）")
    p.add_argument("--timeout", type=float, default=20,
                   help="单次 HTTP 超时秒数（默认 20）")
    p.add_argument("--retries", type=int, default=4,
                   help="单次请求重试次数（默认 4）")
    p.add_argument("--proxy", choices=("auto", "on", "off"), default="auto",
                   help="auto=先系统代理再直连；on=只用代理；off=只直连")
    p.add_argument("--dry-run", action="store_true", help="只拉取校验，不写库")
    p.add_argument("--db", default=str(DB_PATH), help="数据库路径")
    return p.parse_args(argv)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    board_types = (("industry", "concept", "region") if args.type == "all"
                   else (args.type,))
    db = Database(args.db)
    try:
        stats = sync_boards(
            db, board_types=board_types, full=args.full, days=args.days,
            proxy=args.proxy, timeout=args.timeout, retries=args.retries,
            rate=args.rate, limit=args.limit, dry_run=args.dry_run,
            progress=lambda text: print(text, flush=True))
    except KeyboardInterrupt:
        print("\n已取消，本次未写入数据库。")
        return 130
    except Exception as e:  # noqa: BLE001
        log.exception("板块同步失败")
        print(f"\n[失败] {e}")
        return 1

    if stats.get("skipped"):
        return 0
    print("\n=== 校验完成（dry-run）===" if stats["dry_run"] else "\n=== 同步完成 ===")
    print(f"板块：接口 {stats['boards_seen']} 个，刷新成分 "
          f"{stats['boards_refreshed']} 个，失败 {stats['boards_failed']} 个，"
          f"下线 {stats.get('boards_deactivated', 0)} 个")
    if stats["dry_run"]:
        print(f"本次可取归属 {stats['members_total']} 条，未写库")
    else:
        print(f"成分：本次刷新归属 {stats['members_refreshed_total']} 条，新增 "
              f"{stats.get('members_added', 0)} 条，移除 "
              f"{stats.get('members_removed', 0)} 条，库内有效归属 "
              f"{stats['members_total']} 条")
    if stats["failures"]:
        print("失败板块（保留旧数据）：")
        for b, err in stats["failures"][:10]:
            print(f"  - {b['name']}({b['source_code']}): {err}")
        if len(stats["failures"]) > 10:
            print(f"  …… 其余 {len(stats['failures']) - 10} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
