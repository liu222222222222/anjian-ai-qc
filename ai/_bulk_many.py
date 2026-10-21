# -*- coding: utf-8 -*-
"""批量落库助手：一次进程内按顺序 bulk 多个 payload，失败自动重试。

用法: python _bulk_many.py 320 321 322
"""
import os, sys, json, time, traceback

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import cli  # noqa
import db as D  # noqa


def bulk_one(path, tries=4):
    for i in range(tries):
        try:
            payloads = json.load(open(path, encoding="utf-8"))
            res = cli.cmd_bulk_payload(payloads)
            return res
        except Exception as e:
            print("  retry %d: %s" % (i + 1, e))
            time.sleep(1.5)
    return {"ok": False, "error": "all retries failed"}


def main():
    nums = sys.argv[1:]
    for n in nums:
        p = os.path.join(BASE, "reports", "_batch%s.json" % n)
        if not os.path.exists(p):
            print("b%s SKIP (no payload)" % n)
            continue
        t0 = time.time()
        res = bulk_one(p)
        ok = res.get("ok")
        sub = res.get("submitted", "-")
        failed = res.get("failed", "-")
        crit = res.get("critical", "-")
        warn = res.get("warning", "-")
        print("b%-4s ok=%s submitted=%s failed=%s crit=%s warn=%s (%.1fs)"
              % (n, ok, sub, failed, crit, warn, time.time() - t0))
        sys.stdout.flush()
        time.sleep(0.3)


if __name__ == "__main__":
    main()
