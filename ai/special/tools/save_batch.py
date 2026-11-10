# -*- coding: utf-8 -*-
"""专项任务：落判定（写 special.db 的 judg，不碰主库）

用法：
  python special/tools/save_batch.py --task 1 --file reports/_sp9001.json
  python special/tools/save_batch.py --task 1 --files reports/_sp9001.json,reports/_sp9002.json

判定 JSON 与月度同构（数组，每户一条）：
  [{"record_id":123,"watermark_time":"2026-09-05 08:58:52","scene_valid":true,
    "meter_visible":true,"meter_readable":true,"meter_match":true,"meter_read":"182000000002",
    "items_verified_ok":["items2"],
    "comment":"…","issues":[{"kind":"表号无法辨认","severity":"warning",
                             "detail":"…","items_code":"items2"}]}]

铁律 2/7：表号读不出 → **不要**写 meter_readable/meter_match/meter_read，只写 issue。
"""
import os, sys, json, argparse

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)
import special_server as S  # noqa: E402


def one(tid, path, c):
    data = json.load(open(path, encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("items") or data.get("records") or [data]
    ok = skip = 0
    for d in data:
        rid = d.get("record_id")
        if not rid:
            skip += 1
            continue
        issues = d.get("issues") or []
        issues = [i for i in issues if (i.get("severity") in ("critical", "warning"))]
        verdict = S._derive_verdict(issues)
        # 不允许 AI 直接写 pass 覆盖人工复核：只写 judg，reviews 由页面写
        c.execute(
            """INSERT OR REPLACE INTO judg(task_id,rid,scene_valid,meter_visible,
                 meter_readable,meter_match,meter_read,watermark_time,comment,
                 issues,verdict,source,done_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tid, rid, _b(d.get("scene_valid")), _b(d.get("meter_visible")),
             _b(d.get("meter_readable")), _b(d.get("meter_match")),
             d.get("meter_read") or "", d.get("watermark_time") or "",
             d.get("comment") or "", json.dumps(issues, ensure_ascii=False),
             verdict, "new", S.now()))
        ok += 1
    return ok, skip


def _b(v):
    if v is None:
        return None
    return 1 if v else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", type=int, required=True)
    ap.add_argument("--file", default="")
    ap.add_argument("--files", default="")
    a = ap.parse_args()
    paths = [p for p in (a.files.split(",") if a.files else []) if p.strip()]
    if a.file:
        paths.append(a.file)
    if not paths:
        print("给 --file 或 --files")
        return

    c = S.conn()
    tot = 0
    for p in paths:
        if not os.path.exists(p):
            print("跳过不存在：", p)
            continue
        ok, skip = one(a.task, p, c)
        tot += ok
        print("%s  → %d 户%s" % (p, ok, ("，跳过 %d" % skip) if skip else ""))
    c.commit()

    j = c.execute("SELECT COUNT(*) FROM judg WHERE task_id=?", (a.task,)).fetchone()[0]
    t = c.execute("SELECT COUNT(*) FROM recs WHERE task_id=?", (a.task,)).fetchone()[0]
    bad = c.execute("SELECT COUNT(*) FROM judg WHERE task_id=? AND verdict IN ('critical','warning')",
                    (a.task,)).fetchone()[0]
    c.close()
    print("落库 %d 户 · 累计已判 %d/%d · 有问题 %d" % (tot, j, t, bad))


if __name__ == "__main__":
    main()
