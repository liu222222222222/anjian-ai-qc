# -*- coding: utf-8 -*-
"""
barcode2.py —— 强化版条码解码（提高命中率，免视觉读图）

对每张照片做多路预处理 × 多区域裁切 × 多旋转，尽力解出条码：
  1. 整图多尺度（1.0/1.5/2.2/3.0）
  2. 灰度 + CLAHE 局部对比度增强 + 锐化
  3. Otsu 自适应二值化（多 block size）
  4. 横向分带裁切（0-35% / 25-60% / 45-80% / 60-100%）
  5. 竖排（超声波表侧边条码）→ 90° 旋转
所有解出的字符串汇总，只有与档案表号匹配才算命中。

用法：python scripts/barcode2.py --batch 44 [--rids ...]
"""
import os
import re
import sys
import json
import sqlite3
import argparse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import db as D  # noqa: E402
from PIL import Image, ImageOps, ImageFilter  # noqa: E402

PACKS = os.path.join(D.REPORTS_DIR, "_packs")
BANDS = [(0.0, 0.38), (0.24, 0.62), (0.44, 0.82), (0.58, 1.0)]
SCALES = (1.0, 1.5, 2.2, 3.0)


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


def decode_img(path):
    """返回该照片解出的所有条码字符串集合"""
    import numpy as np
    from pyzbar.pyzbar import decode as zb
    import cv2

    base = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    codes = set()

    def harvest(arr):
        if arr is None or arr.size == 0:
            return
        for r in zb(arr):
            try:
                d = r.data.decode("utf-8", "ignore").strip()
            except Exception:
                continue
            if d:
                codes.add(d)

    for scale in SCALES:
        w, h = base.size
        if scale == 1.0:
            im = base
        else:
            im = base.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        arr = np.array(im)
        if arr.ndim == 3:
            gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        else:
            gray = arr
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
        sharp = cv2.addWeighted(clahe, 1.8, cv2.GaussianBlur(clahe, (0, 0), 3), -0.8, 0)
        hh, ww = gray.shape
        for (t, b) in BANDS:
            y0, y1 = int(hh * t), int(hh * b)
            if y1 - y0 < 40:
                continue
            sub = sharp[y0:y1, :]
            harvest(sub)
            harvest(cv2.rotate(sub, cv2.ROTATE_90_CLOCKWISE))
            harvest(cv2.rotate(sub, cv2.ROTATE_90_COUNTERCLOCKWISE))
            for blk in (31, 91):
                ad = cv2.adaptiveThreshold(sub, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                           cv2.THRESH_BINARY, blk, 7)
                harvest(ad)
                harvest(cv2.rotate(ad, cv2.ROTATE_90_CLOCKWISE))
    # 单独：整图 Otsu（铭牌浅底黑字时最有效）
    return codes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, required=True)
    ap.add_argument("--rids", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    recs = load_batch(a.batch)
    if a.rids:
        want = {int(x) for x in a.rids.replace(",", " ").split()}
        recs = [r for r in recs if r["record_id"] in want]

    con = D.connect()
    con.row_factory = sqlite3.Row
    res = []
    for r in recs:
        rid = r["record_id"]
        p = meter_photo(con, rid, r.get("company"))
        if not p:
            res.append({"record_id": rid, "verdict": "no_photo", "codes": []})
            continue
        try:
            codes = decode_img(p)
        except Exception as e:
            codes = set()
            print("ERR %s %s" % (rid, e), file=sys.stderr)
        vs = variants(r.get("meter_no"))
        hits = [c for c in codes if norm(c) in vs]
        if not hits:
            md = re.sub(r"\D", "", norm(r.get("meter_no")))
            if len(md) >= 8:
                for c in codes:
                    cd = re.sub(r"\D", "", norm(c))
                    if len(cd) >= 8 and (cd == md or cd.endswith(md) or md.endswith(cd)):
                        hits.append(c)
        res.append({"record_id": rid, "meter_no": r.get("meter_no"),
                    "codes": sorted(codes), "hits": sorted(set(hits)),
                    "verdict": "match" if hits else ("found_nomatch" if codes else "none")})
    con.close()

    ok = sum(1 for x in res if x["verdict"] == "match")
    bad = sum(1 for x in res if x["verdict"] == "found_nomatch")
    print("== batch %d  共 %d 户 ==" % (a.batch, len(res)))
    print("条码命中(与档案一致): %d    解出不匹配: %d    无条码: %d" % (ok, bad, len(res) - ok - bad))
    print("需视觉读表号的户: %s" % " ".join(str(x["record_id"]) for x in res if x["verdict"] != "match"))
    out = a.out or os.path.join(PACKS, "b%d" % a.batch, "barcode2.json")
    json.dump(res, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("saved %s" % out.replace("\\", "/"))


if __name__ == "__main__":
    main()
