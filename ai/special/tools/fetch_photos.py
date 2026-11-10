# -*- coding: utf-8 -*-
"""专项任务：下载照片（只写 special/<code>/photos/，不碰主 photos/）

用法：
  python special/tools/fetch_photos.py --task 1 [--workers 6] [--limit 0]

说明：
  B 公司 2026-09 的月度照片已被 pending_delete 30 天清理，本地无副本，
  必须按 photos.url 重新下载。URL 至今有效（实测 0.1–0.2s/张）。
"""
import os, sys, io, json, time, sqlite3, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)
import engine  # noqa: E402
import special_server as S  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", type=int, required=True)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--retry-only", action="store_true", help="只补失败/缺失的")
    a = ap.parse_args()

    t = S.get_task(a.task)
    if not t:
        print("任务不存在")
        return
    root = os.path.join(S.SPECIAL_DIR, t["code"], "photos")
    os.makedirs(root, exist_ok=True)

    c = S.conn()
    rows = c.execute(
        "SELECT id,rid,local,url,downloaded FROM rec_photos WHERE task_id=?", (a.task,)
    ).fetchall()
    cust_map = {r["rid"]: r["cust_no"] for r in c.execute(
        "SELECT rid,cust_no FROM recs WHERE task_id=?", (a.task,))}
    c.close()

    from urllib.parse import urlsplit, unquote

    def name_of(url):
        p = unquote(urlsplit(url or "").path)
        return os.path.basename(p) or ""

    jobs = []
    for p in rows:
        rel = (p["local"] or "").replace("\\", "/")
        cust = None
        parts = rel.split("/")
        if len(parts) >= 4:
            cust = parts[-2]
        if rel:
            bn = os.path.basename(rel)
        else:
            bn = name_of(p["url"])
        if not cust:
            cust = cust_map.get(p["rid"]) or "unknown"
        if not bn:
            jobs.append(("nourl", p["id"], "", "", None))
            continue
        rel2 = "photos/%s/%s/%s/%s" % (t["company"], t["month"], cust, bn)
        abs_path = os.path.join(root, rel2.replace("photos/", "", 1))
        if os.path.exists(abs_path) and os.path.getsize(abs_path) > 2000:
            jobs.append(("skip", p["id"], rel2, abs_path, None))
            continue
        if not p["url"]:
            jobs.append(("nourl", p["id"], rel2, abs_path, None))
            continue
        if a.retry_only and p["downloaded"]:
            continue
        jobs.append(("dl", p["id"], rel2, abs_path, p["url"]))

    todo = [j for j in jobs if j[0] == "dl"]
    if a.limit:
        todo = todo[:a.limit]
    print("总照片 %d · 已存在 %d · 无URL %d · 待下载 %d"
          % (len(jobs), len([j for j in jobs if j[0] == "skip"]),
             len([j for j in jobs if j[0] == "nourl"]), len(todo)))

    c = S.conn()
    ok = fail = 0
    for j in jobs:
        if j[0] == "skip":
            c.execute("UPDATE rec_photos SET local=?,downloaded=1 WHERE id=?", (j[2], j[1]))
    c.commit()

    def work(j):
        _, pid, rel2, abs_path, url = j
        try:
            data = engine.fetch(url)
            if len(data) < 2000:
                raise RuntimeError("too small %d" % len(data))
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "wb") as f:
                f.write(data)
            return pid, rel2, None
        except Exception as e:
            return pid, rel2, str(e)[:120]

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(work, j) for j in todo]
        for i, f in enumerate(as_completed(futs), 1):
            pid, rel2, err = f.result()
            if err:
                fail += 1
                print("  FAIL", rel2, err)
            else:
                ok += 1
                c.execute("UPDATE rec_photos SET local=?,downloaded=1 WHERE id=?", (rel2, pid))
                if i % 25 == 0:
                    c.commit()
                    print("  %d/%d  %.0fs" % (i, len(todo), time.time() - t0), flush=True)
    c.commit()
    left = c.execute("SELECT COUNT(*) FROM rec_photos WHERE task_id=? AND downloaded=0",
                     (a.task,)).fetchone()[0]
    c.close()
    print("完成：成功 %d · 失败 %d · 仍缺 %d · 用时 %.0fs"
          % (ok, fail, left, time.time() - t0))


if __name__ == "__main__":
    main()
