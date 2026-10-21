# -*- coding: utf-8 -*-
"""
ocr_meter.py —— 用 RapidOCR 程序化读表号（免视觉读图）

燃气表铭牌上表号是印刷体数字（如 2017.06 201706000001），
RapidOCR 对这类高对比数字识别率不错。策略：
  1. 对整图 + 分带裁切 + 多尺度 做 OCR
  2. 抽取所有长度 >=7 的数字串候选
  3. 与档案表号做归一化匹配（等值 / 后缀 / 前缀 / 编辑距离<=1）
  4. 命中即返回 match，且记录读到的字符串

用法：python scripts/ocr_meter.py --batch 44 [--rids ...] [--workers 6]
"""
import os
import re
import sys
import json
import sqlite3
import argparse
from concurrent.futures import ProcessPoolExecutor

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import db as D  # noqa: E402

PACKS = os.path.join(D.REPORTS_DIR, "_packs")
BANDS = [(0.0, 1.0), (0.10, 0.55), (0.30, 0.75), (0.50, 0.95), (0.20, 0.40)]
SCALES = (1.0, 1.8)
_OCR = None


def get_ocr():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    return _OCR


def norm(s):
    return re.sub(r"[^0-9A-Za-z]", "", str(s or "")).upper()


def variants(meter_no):
    n = norm(meter_no)
    out = {n}
    if n.startswith("T"):
        out.add(n[1:])
    st = n.lstrip("0")
    if st:
        out.add(st)
    return {x for x in out if x}


def digits(s):
    return re.sub(r"\D", "", s)


def edit_leq1(a, b):
    """编辑距离 <= 1"""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = sum(1 for x, y in zip(a, b) if x != y)
        return diff <= 1
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = j = 0
    skipped = False
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
        else:
            if skipped:
                return False
            skipped = True
            j += 1
    return True


def read_one(args):
    rid, path, meter_no = args
    try:
        import numpy as np
        from PIL import Image, ImageOps
        import cv2
        ocr = get_ocr()
        base = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        cands = []
        for scale in SCALES:
            w, h = base.size
            im = base if scale == 1.0 else base.resize(
                (int(w * scale), int(h * scale)), Image.LANCZOS)
            arr = np.array(im)
            hh = arr.shape[0]
            for (t, b) in BANDS:
                sub = arr[int(hh * t):int(hh * b), :]
                if sub.size == 0 or sub.shape[0] < 30:
                    continue
                try:
                    res, _ = ocr(sub)
                except Exception:
                    continue
                if not res:
                    continue
                for line in res:
                    try:
                        txt = line[1]
                        conf = float(line[2])
                    except Exception:
                        continue
                    if conf < 0.5:
                        continue
                    for m in re.findall(r"[0-9]{7,}", txt):
                        cands.append((m, conf))
                    # 字母开头（T130000001）
                    for m in re.findall(r"[A-Za-z][0-9A-Za-z\-]{6,}", txt):
                        cands.append((m, conf))
        vs = variants(meter_no)
        md = digits(norm(meter_no))
        hits = []
        for c, conf in cands:
            n = norm(c)
            if n in vs:
                hits.append((c, conf, "exact"))
                continue
            cd = digits(n)
            if len(md) >= 8 and len(cd) >= 8 and len(cd) == len(md):
                if edit_leq1(cd, md):
                    hits.append((c, conf, "edit1"))
                elif cd.endswith(md) or md.endswith(cd):
                    hits.append((c, conf, "suffix"))
        if hits:
            best = max(hits, key=lambda x: x[1])
            return {"record_id": rid, "verdict": "match", "read": best[0],
                    "how": best[2], "conf": round(best[1], 3)}
        return {"record_id": rid, "verdict": "none", "cands": cands[:12]}
    except Exception as e:
        return {"record_id": rid, "verdict": "err", "msg": str(e)}


def load_batch(n):
    p = os.path.join(PACKS, "b%d" % n, "batch.json")
    return json.load(open(p, encoding="utf-8"))["records"]


def meter_photo(con, rid, company):
    mc = D.meter_code(company or "")
    row = con.execute(
        "SELECT local FROM photos WHERE record_id=? AND items_code=? AND downloaded=1 "
        "ORDER BY id LIMIT 1", (rid, mc)).fetchone()
    if not row or not row["local"]:
        return None
    p = os.path.join(D.BASE, row["local"])
    return p if os.path.exists(p) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, required=True)
    ap.add_argument("--rids", default="")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    recs = load_batch(a.batch)
    if a.rids:
        want = {int(x) for x in a.rids.replace(",", " ").split()}
        recs = [r for r in recs if r["record_id"] in want]

    con = D.connect()
    con.row_factory = sqlite3.Row
    jobs = []
    for r in recs:
        p = meter_photo(con, r["record_id"], r.get("company"))
        if p:
            jobs.append((r["record_id"], p, r.get("meter_no")))
    con.close()

    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        res = list(ex.map(read_one, jobs))

    ok = sum(1 for x in res if x["verdict"] == "match")
    print("== batch %d 共 %d 户 ==" % (a.batch, len(res)))
    print("OCR 命中: %d   未命中(需视觉): %d" % (ok, len(res) - ok))
    print("需视觉: %s" % " ".join(str(x["record_id"]) for x in res if x["verdict"] != "match"))
    for x in res:
        if x["verdict"] == "match":
            print("  %s -> %s (%s, %.2f)" % (x["record_id"], x["read"], x["how"], x["conf"]))
    out = a.out or os.path.join(PACKS, "b%d" % a.batch, "ocr.json")
    json.dump(res, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("saved %s" % out.replace("\\", "/"))


if __name__ == "__main__":
    main()
