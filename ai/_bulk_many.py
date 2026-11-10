# -*- coding: utf-8 -*-
"""批量落库助手：一次进程内按顺序 bulk 多个 payload，失败自动重试。

用法: python _bulk_many.py 320 321 322
      python _bulk_many.py 320 --force     # 允许重判已 done 的户

★ 2026-11-04 加防回归护栏：
   重判（bulk 同一批已 done 的户）会用 **payload 原版** 覆盖掉后来人工修正的判定。
   实测：手工重落 955a/957a 后，103187 / 103202 / 103205 三户已修正的假 critical /
   假撤销信号又被还原回来（103205 差点被撤销抽样）。
   因此默认 **跳过「全部户都已 done」的 payload**，只有显式 --force 才重判。
"""
import os, sys, json, time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import cli  # noqa
import db as D  # noqa


def done_ratio(payloads):
    """返回 (已成 done 户数, 总户数)。"""
    rids = [p.get("record_id") for p in payloads if p.get("record_id")]
    if not rids:
        return 0, 0
    con = D.connect()
    try:
        ph = ",".join("?" * len(rids))
        n = con.execute(
            f"SELECT COUNT(DISTINCT record_id) FROM checks "
            f"WHERE status='done' AND record_id IN ({ph})", rids).fetchone()[0]
    finally:
        con.close()
    return n, len(rids)


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
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    for n in args:
        p = os.path.join(BASE, "reports", "_batch%s.json" % n)
        if not os.path.exists(p):
            print("b%s SKIP (no payload)" % n)
            continue
        try:
            payloads = json.load(open(p, encoding="utf-8"))
        except Exception as e:
            print("b%s SKIP (解析失败 %s)" % (n, e))
            continue
        nd, tot = done_ratio(payloads)
        if not force and tot and nd == tot:
            print("b%-4s SKIP (全部 %d 户已 done，防回归；要重判加 --force)" % (n, tot))
            continue
        t0 = time.time()
        res = bulk_one(p)
        ok = res.get("ok")
        print("b%-4s ok=%s submitted=%s failed=%s crit=%s warn=%s (%.1fs)"
              % (n, ok, res.get("submitted", "-"), res.get("failed", "-"),
                 res.get("critical", "-"), res.get("warning", "-"), time.time() - t0))
        sys.stdout.flush()
        time.sleep(0.3)


if __name__ == "__main__":
    main()
