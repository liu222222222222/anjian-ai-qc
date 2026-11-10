# -*- coding: utf-8 -*-
"""专项任务：补导辅助照片（只存 URL，不下载、不参与 AI 判定）

背景：月度导入只认列名含「照片/图片」的列，B 公司 Excel 另有 5 列叫「…拍照」，
      全部漏掉。本脚本按 Excel 原始列补进 special.db，只做外链展示用。

用法：
  python special/tools/import_extra_photos.py --task 1 \
      --excel "D:/anjian-qc/安检表格/2026-09/B 公司202609月安检明细.xlsx"

规则：
  * 已有的 3 个判定用项（overall_path / items2 / items3）只补 url，不新增
  * 其余列新增 rec_photos 记录：local='' downloaded=0 ext_only=1
  * Excel 该列为空 → 插一条 empty=1 记录，供前端显示「未拍摄」
  * 不写主库、不下载图片
"""
import os, sys, json, sqlite3, argparse, re

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE)
import special_server as S  # noqa: E402

# Excel 列名 → (items_code, 是否判定用项)
COL_MAP = {
    "总体安检照片": ("overall_path", True),
    "表具照片（机械、电子读数及表号）": ("items2", True),
    "表具整体照片(表具全景及防盗卡)": ("items3", True),
    "天然气用气须知拍照": ("ext_notice", False),
    "入户安检贴拍照": ("ext_sticker", False),
    "用气设备拍照": ("ext_device", False),
    "自闭阀拍照": ("ext_valve", False),
    "安检单拍照": ("ext_form", False),
}


def urls_of(v):
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            out.extend(urls_of(x))
        return out
    s = str(v).replace("\r", "\n")
    parts = re.split(r"[\n\s,;]+", s)
    return [p.strip() for p in parts if p.strip().startswith("http")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", type=int, required=True)
    ap.add_argument("--excel", required=True)
    ap.add_argument("--sheet", default="")
    a = ap.parse_args()

    t = S.get_task(a.task)
    if not t:
        print("任务不存在")
        return

    from openpyxl import load_workbook
    wb = load_workbook(a.excel, read_only=True, data_only=True)
    ws = wb[a.sheet] if a.sheet else wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    hdr = [str(h).strip() if h else "" for h in next(it)]
    idx = {h: i for i, h in enumerate(hdr)}

    for need in ("客户编号", "安检状态", "安检时间", "用气性质"):
        if need not in idx:
            print("Excel 缺列：", need)
            return

    cols = [h for h in hdr if h in COL_MAP]
    print("识别到 %d 个照片列：%s" % (len(cols), "、".join(cols)))

    # 目标户（专项名单内的客户编号）
    c = S.conn()
    rid_of = {}
    for r in c.execute("SELECT rid,cust_no FROM recs WHERE task_id=?", (a.task,)):
        rid_of[str(r["cust_no"])] = r["rid"]

    best = {}
    for row in it:
        cust = str(row[idx["客户编号"]] or "").strip()
        if cust not in rid_of:
            continue
        if str(row[idx["安检状态"]] or "").strip() != "入户":
            continue
        tm = str(row[idx["安检时间"]] or "")
        d = {}
        for h in cols:
            d[h] = urls_of(row[idx[h]])
        if cust not in best or tm > best[cust][0]:
            best[cust] = (tm, d)
    wb.close()
    print("匹配到 %d 户" % len(best))

    stat = {"added": 0, "updated": 0, "empty": 0, "houses": 0}
    for cust, (tm, d) in best.items():
        rid = rid_of[cust]
        stat["houses"] += 1
        seq = 100
        for h, urls in d.items():
            code, is_judge = COL_MAP[h]
            if is_judge:
                # 已有记录：补/校正 url
                row0 = c.execute(
                    "SELECT id FROM rec_photos WHERE task_id=? AND rid=? AND items_code=?",
                    (a.task, rid, code)).fetchone()
                if row0 and urls:
                    c.execute("UPDATE rec_photos SET url=?, category=? WHERE id=?",
                              (urls[0], h, row0["id"]))
                    stat["updated"] += 1
                    extra = urls[1:]
                    for u in extra:
                        c.execute(
                            """INSERT INTO rec_photos(task_id,rid,items_code,category,seq,local,url,
                                 is_meter,downloaded,ext_only,empty)
                               VALUES(?,?,?,?,?,?,?,?,0,0,0)""",
                            (a.task, rid, code, h, seq, "", u, 0))
                        seq += 1
                        stat["added"] += 1
                elif not row0 and urls:
                    for u in urls:
                        c.execute(
                            """INSERT INTO rec_photos(task_id,rid,items_code,category,seq,local,url,
                                 is_meter,downloaded,ext_only,empty)
                               VALUES(?,?,?,?,?,?,?,?,0,0,0)""",
                            (a.task, rid, code, h, seq, "", u, 0))
                        seq += 1
                        stat["added"] += 1
            else:
                # 辅助照片：每个 URL 一条，全部 ext_only
                c.execute("DELETE FROM rec_photos WHERE task_id=? AND rid=? AND items_code=?",
                          (a.task, rid, code))
                if urls:
                    for u in urls:
                        c.execute(
                            """INSERT INTO rec_photos(task_id,rid,items_code,category,seq,local,url,
                                 is_meter,downloaded,ext_only,empty)
                               VALUES(?,?,?,?,?,?,?,?,0,1,0)""",
                            (a.task, rid, code, h, seq, "", u, 0))
                        seq += 1
                        stat["added"] += 1
                else:
                    c.execute(
                        """INSERT INTO rec_photos(task_id,rid,items_code,category,seq,local,url,
                             is_meter,downloaded,ext_only,empty)
                           VALUES(?,?,?,?,?,?,?,?,0,1,1)""",
                        (a.task, rid, code, h, seq, "", "", 0))
                    seq += 1
                    stat["empty"] += 1
        c.execute("UPDATE recs SET photo_count=? WHERE task_id=? AND rid=?",
                  (c.execute("SELECT COUNT(*) FROM rec_photos WHERE task_id=? AND rid=? AND empty=0",
                             (a.task, rid)).fetchone()[0], a.task, rid))
    c.commit()

    tot = c.execute("SELECT COUNT(*) FROM rec_photos WHERE task_id=?", (a.task,)).fetchone()[0]
    judge_n = c.execute("SELECT COUNT(*) FROM rec_photos WHERE task_id=? AND COALESCE(ext_only,0)=0",
                        (a.task,)).fetchone()[0]
    ext_n = c.execute("SELECT COUNT(*) FROM rec_photos WHERE task_id=? AND ext_only=1",
                      (a.task,)).fetchone()[0]
    empty_n = c.execute("SELECT COUNT(*) FROM rec_photos WHERE task_id=? AND empty=1",
                        (a.task,)).fetchone()[0]
    c.close()
    print("完成：新增 %d · 校正 %d · 空项 %d" % (stat["added"], stat["updated"], stat["empty"]))
    print("当前：总 %d 条（判定用 %d · 辅助外链 %d · 未拍摄 %d）" % (tot, judge_n, ext_n, empty_n))


if __name__ == "__main__":
    main()
