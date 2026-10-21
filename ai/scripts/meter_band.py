# -*- coding: utf-8 -*-
"""
meter_band.py —— 自动定位表号带（免手工试裁切带）

思路：表号印刷行永远紧贴条形码。用 OpenCV 的形态学梯度找条码状区域，
取条码框 + 下方 12% 高度作为「表号带」，每户单独裁出，再横向拼成高分辨率大图。
拼图每格等宽（默认 1000px），保证每格有效像素 ≈ 原始表号字号，
一图可放 2-3 户（读取端长边 1568px 上限）。

用法：
    python scripts/meter_band.py --batch 44 --per 3 --tag a
    python scripts/meter_band.py --batch 44 --rids 26059,26062 --per 1
输出：reports/_packs/b<N>/smb<tag><i>.jpg，同时打印每格户号
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
from PIL import Image, ImageOps, ImageDraw, ImageFont  # noqa: E402

PACKS = os.path.join(D.REPORTS_DIR, "_packs")
FONT_PATH = "C:/Windows/Fonts/msyh.ttc"


def _font(size):
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except Exception:
        return ImageFont.load_default()


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


def find_barcode_box(path):
    """返回条码框 (x0,y0,x1,y1) 归一化；找不到返回 None"""
    import numpy as np
    import cv2
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    arr = np.array(im)
    h, w = arr.shape[:2]
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    # 横向梯度（条码竖条纹 → x 方向变化大）
    gx = cv2.Sobel(gray, cv2.CV_16S, 1, 0, ksize=3)
    gx = cv2.convertScaleAbs(gx)
    # 形态学闭运算把条纹连成块
    k = max(9, w // 60) | 1
    closed = cv2.morphologyEx(gx, cv2.MORPH_CLOSE,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (k, 3)))
    _, bw = cv2.threshold(closed, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    # 条码通常是「细高或细长」的区域：宽>高*1.5 且 宽>图宽*0.10
    cnts, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    best_score = -1
    for c in cnts:
        x, y, cw, ch = cv2.boundingRect(c)
        if cw < w * 0.10 or ch < 12:
            continue
        ar = cw / max(1, ch)
        if ar < 1.4 or ar > 30:
            continue
        # 分数：宽占比 + 长宽比适中 + 位于画面中上部加分
        score = (cw / w) * min(ar, 8) / 8.0
        if 0.15 <= y / h <= 0.85:
            score += 0.25
        if score > best_score:
            best_score = score
            best = (x, y, cw, ch)
    if not best:
        return None
    x, y, cw, ch = best
    return (x / w, y / h, (x + cw) / w, (y + ch) / h)


def crop_band(path, pad_up=0.02, pad_dn=0.16):
    """裁条码 + 下方印刷行。找不到条码则回退到画面中部 30-70%"""
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    w, h = im.size
    box = find_barcode_box(path)
    if box:
        x0, y0, x1, y1 = box
        # 左右各留边，避免切到表号首尾
        lx = max(0.0, x0 - 0.06)
        rx = min(1.0, x1 + 0.06)
        ty = max(0.0, y0 - pad_up)
        by = min(1.0, y1 + pad_dn)
        if (by - ty) < 0.10:
            by = min(1.0, y1 + 0.22)
        if (rx - lx) < 0.20:
            lx, rx = 0.0, 1.0
        return im.crop((int(lx * w), int(ty * h), int(rx * w), int(by * h))), "auto"
    return im.crop((0, int(h * 0.30), w, int(h * 0.72))), "mid"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, required=True)
    ap.add_argument("--rids", default="")
    ap.add_argument("--per", type=int, default=3)
    ap.add_argument("--width", type=int, default=1150)
    ap.add_argument("--tag", default="")
    a = ap.parse_args()

    recs = load_batch(a.batch)
    if a.rids:
        want = {int(x) for x in a.rids.replace(",", " ").split()}
        recs = [r for r in recs if r["record_id"] in want]

    con = D.connect()
    con.row_factory = sqlite3.Row
    tiles = []
    for r in recs:
        p = meter_photo(con, r["record_id"], r.get("company"))
        if not p:
            print("NO_PHOTO %s" % r["record_id"])
            continue
        c, how = crop_band(p)
        nh = max(1, int(c.size[1] * a.width / c.size[0]))
        c = c.resize((a.width, nh), Image.LANCZOS)
        tiles.append((str(r["record_id"]), c, how))
    con.close()

    outdir = os.path.join(PACKS, "b%d" % a.batch)
    os.makedirs(outdir, exist_ok=True)
    f = _font(40)
    sheets = []
    for i in range(0, len(tiles), a.per):
        grp = tiles[i:i + a.per]
        H = sum(t.size[1] + 48 for _, t, _ in grp)
        canvas = Image.new("RGB", (a.width, H), "white")
        dr = ImageDraw.Draw(canvas)
        y = 0
        for rid, t, how in grp:
            dr.text((8, y + 6), rid + ("*" if how == "mid" else ""),
                    fill=(200, 0, 0), font=f)
            canvas.paste(t, (0, y + 46))
            y += t.size[1] + 48
        out = os.path.join(outdir, "smb%s%d.jpg" % (a.tag, i // a.per))
        canvas.save(out, quality=92)
        sheets.append(out.replace("\\", "/"))
        print("saved %s  户: %s" % (out.replace("\\", "/"),
                                    " ".join("%s%s" % (r, "(mid)" if h == "mid" else "")
                                             for r, _, h in grp)))
    print("JSON::" + json.dumps({"sheets": sheets}, ensure_ascii=False))


if __name__ == "__main__":
    main()
