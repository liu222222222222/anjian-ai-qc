# -*- coding: utf-8 -*-
"""专项任务：领批（生成 checklist）

用法：
  python special/tools/pack.py --task 1 --n 8 [--inspector 王五] [--ids 1,2,3]

输出：
  reports/_sp<批次号>.md   给人/子代理读的清单（含照片绝对路径）
  reports/_sp<批次号>.json 空壳（待填判定）
"""
import os, sys, json, argparse, datetime

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)
import special_server as S  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", type=int, required=True)
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--inspector", default="")
    ap.add_argument("--ids", default="", help="指定 record_id，逗号分隔")
    a = ap.parse_args()

    t = S.get_task(a.task)
    if not t:
        print("任务不存在")
        return
    root = os.path.join(S.SPECIAL_DIR, t["code"], "photos")

    rep = os.path.join(BASE, "reports")
    os.makedirs(rep, exist_ok=True)

    # 已派发但还没落库的户不能再领（否则两批重复）
    import glob
    issued = set()
    for f in glob.glob(os.path.join(rep, "_sp*.json")):
        try:
            for d in json.load(open(f, encoding="utf-8")):
                if isinstance(d, dict) and d.get("record_id"):
                    issued.add(int(d["record_id"]))
        except Exception:
            pass

    c = S.conn(readonly=True)
    if a.ids:
        ids = [int(x) for x in a.ids.split(",") if x.strip()]
        q = "SELECT * FROM recs WHERE task_id=? AND rid IN (%s)" % ",".join("?" * len(ids))
        rows = c.execute(q, [a.task] + ids).fetchall()
    else:
        w = "r.task_id=? AND r.rid NOT IN (SELECT rid FROM judg WHERE task_id=?)"
        args = [a.task, a.task]
        if a.inspector:
            w += " AND r.inspector=?"
            args.append(a.inspector)
        if issued:
            w += " AND r.rid NOT IN (%s)" % ",".join("?" * len(issued))
            args = args + sorted(issued)
        rows = c.execute(
            "SELECT r.* FROM recs r WHERE " + w + " ORDER BY r.inspector, r.cust_no LIMIT ?",
            args + [a.n]).fetchall()

    if not rows:
        print("没有待判定的户了（已派发未落库的户会被跳过）")
        return

    # 批次号：9001 起，取未占用的
    n = 9001
    while os.path.exists(os.path.join(rep, "_sp%d.md" % n)):
        n += 1

    lines = ["# 专项批次 SP%d · %s · %s" % (n, t["code"], t["month"]),
             "",
             "任务：%s" % t["name"],
             "批次：%d 户  ·  生成 %s" % (len(rows), datetime.datetime.now().strftime("%Y-%m-%d %H:%M")),
             "",
             "> 读图零处理：直接读原图，不裁切不放大不拼图。表号读不出只写 warning，删掉 meter_* 三字段。",
             ""]
    payload = []
    for i, r in enumerate(rows, 1):
        ph = c.execute(
            "SELECT items_code,category,seq,local,url,is_meter FROM rec_photos "
            "WHERE task_id=? AND rid=? ORDER BY seq", (a.task, r["rid"])).fetchall()
        lines.append("## %d. %s %s · 安检员 %s" % (i, r["cust_no"], r["cust_name"] or "", r["inspector"]))
        lines.append("")
        lines.append("- record_id: `%d`" % r["rid"])
        lines.append("- 安检时间: %s" % (r["check_time"] or ""))
        lines.append("- 用气性质: %s · 小区: %s" % (r["gas_nature"] or "", r["community"] or ""))
        lines.append("- 地址: %s" % (r["address"] or ""))
        lines.append("- 档案表号: `%s`" % (r["meter_no"] or "空"))
        lines.append("- 原判定: %s（%s）" % (r["origin"], "月度已判" if r["origin"] == "monthly" else "本次新判"))
        lines.append("- 照片 %d 张：" % len(ph))
        for p in ph:
            rel = (p["local"] or "").replace("\\", "/")
            abs_p = os.path.join(root, rel.replace("photos/", "", 1)) if rel else ""
            if not abs_p or not os.path.exists(abs_p):
                cand = os.path.join(root, "photos", t["company"], t["month"],
                                    r["cust_no"] or "", os.path.basename(rel) if rel else "")
                abs_p = cand if os.path.exists(cand) else "(缺)"
            lines.append("  - `%s`  %s%s" % (os.path.normpath(abs_p), p["items_code"] or "",
                                            "  [表具]" if p["is_meter"] else ""))
        lines.append("")
        payload.append({"record_id": r["rid"], "cust_no": r["cust_no"],
                        "inspector": r["inspector"], "meter_no": r["meter_no"]})
    c.close()

    md = os.path.join(rep, "_sp%d.md" % n)
    js = os.path.join(rep, "_sp%d.json" % n)
    with open(md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    with open(js, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("批次 SP%d：%d 户" % (n, len(rows)))
    print("清单 %s" % md)
    print("壳子 %s（填判定后跑 save_batch.py）" % js)


if __name__ == "__main__":
    main()
