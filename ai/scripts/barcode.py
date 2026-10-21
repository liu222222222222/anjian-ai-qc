# -*- coding: utf-8 -*-
"""
barcode.py —— 程序化解表号（免视觉读图）

思路：燃气表铭牌上通常有条码（Code128 / EAN），条码内容往往就是表号。
用 pyzbar 解，解出来与档案表号做归一化比对；解不出返回 None，
交给视觉流程（scan.py mt / batch.py zoom）处理。

用法：
    python scripts/barcode.py --batch 44                 # 解整批
    python scripts/barcode.py --batch 44 --rids 26059,26060
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
from PIL import Image, ImageOps  # noqa: E402

PACKS = os.path.join(D.REPORTS_DIR, "_packs")


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
    """归一化表号：只保留数字与字母大写"""
    if not s:
        return ""
    return re.sub(r"[^0-9A-Za-z]", "", str(s)).upper()


def variants(meter_no):
    """档案表号的可接受变体集合（原样、去掉前导零、去前缀 T 等）"""
    n = norm(meter_no)
    out = {n}
    if n.startswith("T"):
        out.add(n[1:])
    stripped = n.lstrip("0")
    if stripped:
        out.add(stripped)
    return {x for x in out if x}


def decode_all(path, tries=3):
    """尝试多尺度 / 多旋转解码条码，返回候选字符串集合"""
    import numpy as np
    from pyzbar.pyzbar import decode as zb_decode
    import cv2

    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    out = set()

    def add(res):
        for r in res:
            try:
                d = r.data.decode("utf-8", "ignore")
            except Exception:
                continue
            if d:
                out.add(d.strip())

    for scale in (1.0, 1.6, 2.4):
        w, h = im.size
        if scale != 1.0:
            im2 = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        else:
            im2 = im
        arr = np.array(im2)
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        variants_ = [gray]
        # 自适应二值化：条码常印在浅色铭牌上
        for blk in (31, 61, 121):
            ad = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                        cv2.THRESH_BINARY, blk, 5)
            variants_.append(ad)
        # 竖排条码（超声波表侧边）→ 转 90°
        variants_.append(cv2.rotate(gray, cv2.ROTATE_90_CLOCKWISE))
        variants_.append(cv2.rotate(gray, cv2.ROTATE_90_COUNTERCLOCKWISE))
        for v in variants_:
            add(zb_decode(v))
            if out:
                return out
    return out


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
            res.append({"record_id": rid, "codes": [], "verdict": "no_photo"})
            continue
        try:
            codes = decode_all(p)
        except Exception as e:
            codes = set()
            print("ERR %s %s" % (rid, e), file=sys.stderr)
        vs = variants(r.get("meter_no"))
        hits = [c for c in codes if norm(c) in vs]
        # 退一步：条码内容去掉非数字后与档案数字部分相同
        if not hits:
            md = re.sub(r"\D", "", norm(r.get("meter_no")))
            if len(md) >= 8:
                for c in codes:
                    cd = re.sub(r"\D", "", norm(c))
                    if len(cd) >= 8 and (cd == md or cd.endswith(md) or md.endswith(cd)):
                        hits.append(c)
        res.append({
            "record_id": rid,
            "meter_no": r.get("meter_no"),
            "codes": sorted(codes),
            "hits": sorted(set(hits)),
            "verdict": "match" if hits else ("found_nomatch" if codes else "none"),
        })
    con.close()

    ok = sum(1 for x in res if x["verdict"] == "match")
    bad = sum(1 for x in res if x["verdict"] == "found_nomatch")
    non = sum(1 for x in res if x["verdict"] in ("none", "no_photo"))
    print("== batch %d  共 %d 户 ==" % (a.batch, len(res)))
    print("条码解出且与档案一致: %d" % ok)
    print("解出但不一致(需人工看): %d" % bad)
    print("无条码/解不出(需视觉): %d" % non)
    for x in res:
        if x["verdict"] != "none":
            print("  %s %s %s -> %s | codes=%s"
                  % (x["record_id"], x.get("meter_no"), x["verdict"],
                     ",".join(x["hits"]), ",".join(x["codes"])[:90]))
    out = a.out or os.path.join(PACKS, "b%d" % a.batch, "barcode.json")
    json.dump(res, open(out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("saved %s" % out.replace("\\", "/"))


if __name__ == "__main__":
    main()
