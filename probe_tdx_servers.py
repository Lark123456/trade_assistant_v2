"""批量探测 pytdx 服务器可用性：TCP 连通 + pytdx 协议握手 + 真实行情拉取。"""
import time
from pytdx.hq import TdxHq_API

SERVERS = [
    ("上海电信Z1",  "180.153.18.170", 7709),
    ("上海电信Z80", "180.153.18.172", 80),
    ("北京联通Z80", "202.108.253.139", 80),
    ("杭州电信J1",  "60.191.117.167",  7709),
    ("杭州电信J2",  "115.238.56.198",  7709),
    ("杭州电信J3",  "218.75.126.9",    7709),
    ("杭州电信J4",  "115.238.90.165",  7709),
    ("浙江联通J2",  "60.12.136.250",   7709),
    ("广东电信",    "59.36.5.11",      7709),
    ("国泰君安-13", "117.34.114.13",   7709),
    ("国泰君安-14", "117.34.114.14",   7709),
    ("国泰君安-15", "117.34.114.15",   7709),
    ("国泰君安-16", "117.34.114.16",   7709),
    ("国泰君安-17", "117.34.114.17",   7709),
    ("国泰君安-18", "117.34.114.18",   7709),
    ("国泰君安-20", "117.34.114.20",   7709),
    ("国泰君安-27", "117.34.114.27",   7709),
    ("天风-182",    "218.106.92.182",  7709),
    ("天风-183",    "218.106.92.183",  7709),
    ("安徽-71",     "220.178.55.71",   7709),
    ("安徽-86",     "220.178.55.86",   7709),
]


def probe(name, ip, port):
    api = TdxHq_API()
    t0 = time.time()
    try:
        tcp_ok = api.connect(ip, port, time_out=3)
        tcp_ms = int((time.time() - t0) * 1000)
    except Exception:
        tcp_ms, tcp_ok = -1, False
    if not tcp_ok:
        try:
            api.disconnect()
        except Exception:
            pass
        return name, ip, port, False, False, tcp_ms, -1, 0

    proto_ok, qms, last = False, -1, 0
    try:
        t1 = time.time()
        q = api.get_security_quotes([(1, "600000")])
        qms = int((time.time() - t1) * 1000)
        if q and len(q) > 0:
            proto_ok = True
            last = q[0].get("price", 0) or 0
    except Exception:
        pass
    try:
        api.disconnect()
    except Exception:
        pass
    return name, ip, port, True, proto_ok, tcp_ms, qms, last


def main():
    rows = [probe(*s) for s in SERVERS]
    print()
    print(f"{'服务器':<12}{'IP':<18}{'端口':<6}{'TCP':<5}{'协议':<5}"
          f"{'连ms':<8}{'行情ms':<9}浦发现价")
    print("-" * 75)
    tcp_ok_n, proto_ok_n = 0, 0
    for name, ip, port, t, p, tms, qms, last in rows:
        t_s = "OK" if t else "X"
        p_s = "OK" if p else "X"
        tms_s = f"{tms}ms" if tms >= 0 else "fail"
        qms_s = f"{qms}ms" if qms >= 0 else "fail"
        last_s = f"{last:.3f}" if last else "-"
        print(f"{name:<12}{ip:<18}{port:<6}{t_s:<5}{p_s:<5}"
              f"{tms_s:<8}{qms_s:<9}{last_s}")
        if t:
            tcp_ok_n += 1
        if p:
            proto_ok_n += 1
    print()
    print(f"TCP 可达:           {tcp_ok_n}/{len(rows)}")
    print(f"pytdx 协议可用:     {proto_ok_n}/{len(rows)}")
    print()
    if proto_ok_n > 0:
        print("可用服务器 (按行情延迟排序):")
        usable = sorted(
            [r for r in rows if r[4]],
            key=lambda r: (r[6] if r[6] >= 0 else 99999),
        )
        for name, ip, port, _, _, _, qms, _ in usable:
            print(f"  {qms:5d}ms  ({ip:<16}, {port:<5})  {name}")


if __name__ == "__main__":
    main()