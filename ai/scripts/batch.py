#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""批次操作工具 — 安检抽查 Agent 通道的"一条命令"流水线

把多批反复手工执行的准备动作固化成命令：
  pack      领一批 → 生成 水印联排图 / 表号联排图 / checklist / batch.json
  gap       批量算「水印时间 vs 工单时间」间隔，标出 >168h 与边界值（168h 一刀切）
  zoom      单户单区域高倍放大（边界复核用）
  precheck  提交前预检（表号编辑距离 / gap 边界 / 字段矛盾 / rid 存在性）
  postcheck 提交后核对（列出机械追加的问题，对比人工预期）

用法（在 ai/ 目录下，必须用 venv python）：
  python scripts/batch.py pack --n 32 --company C1 --month 2026-09
  python scripts/batch.py gap --pairs "13755=2026-09-02 13:00:04,13769=2026-08-13 08:47:51"
  python scripts/batch.py zoom --rids 13755,13769 --zone wm --scale 3
  python scripts/batch.py precheck --file reports/_batch24.json
  python scripts/batch.py postcheck --batch 24 --payload reports/_batch24.json
"""
import os, sys, re, json, argparse, subprocess, datetime
from PIL import Image, ImageDraw, ImageFont, ImageOps

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # ai/
sys.path.insert(0, BASE)
import db as D

PACKS = os.path.join(D.REPORTS_DIR, "_packs")
ZOOM_DIR = os.path.join(PACKS, "_zoom")

# 水印/表号裁切区域（相对高度）
WM_TOP, WM_BOT = 0.76, 1.00      # 水印在照片底部
MT_TOP, MT_BOT = 0.25, 0.62      # 表号（条码下方数字行）常在此带

FONT_CANDIDATES = ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
                   "C:/Windows/Fonts/arial.ttf"]


def _font(size):
    for fp in FONT_CANDIDATES:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _connect():
    return D.connect()


def _parse_time(s):
    if not s:
        return None
    s = str(s).strip().replace("/", "-")
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s[:19 if len(s) >= 19 else len(s)], f)
        except Exception:
            continue
    return None


def _norm(t):
    """表号规范化：只留字母数字并大写，用于编辑距离对齐"""
    return re.sub(r"[^0-9A-Za-z]", "", str(t or "")).upper()


def _lev(a, b):
    """Levenshtein 编辑距离"""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if not la:
        return lb
    if not lb:
        return la
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1,
                         prev[j - 1] + (a[i - 1] != b[j - 1]))
        prev = cur
    return prev[lb]


def _meter_paths(r):
    """表具照片路径（公司差异：A 公司 items4、C 公司 items1，其余 items2）

    兜底：个别户的表具照片被系统挂在别的 items_code 下（实测 C3 有 22 户挂在 items2），
    按 items_name == '表具照片' 再认一次，避免 checklist 误标「⚠无表具照片」。
    """
    mc = D.meter_code(r.get("company") or "")
    hits = [it["path"] for it in r.get("items", []) if it.get("items_code") == mc]
    if hits:
        return hits
    return [it["path"] for it in r.get("items", [])
            if (it.get("items_name") or "") == "表具照片"]


def _stamp(im, text, size):
    """在图片左上角盖白底红字户号（防止多户联排时行归因错位）"""
    dr = ImageDraw.Draw(im, "RGBA")
    f = _font(size)
    x, y = 10, 8
    bbox = dr.textbbox((x, y), text, font=f)
    dr.rectangle((bbox[0] - 6, bbox[1] - 3, bbox[2] + 6, bbox[3] + 4),
                 fill=(255, 255, 255, 235))
    dr.text((x, y), text, font=f, fill=(200, 0, 0))


def _stack(records, top, bot, out_prefix, per_sheet, target_w, stamp_size=44,
           label_size=40):
    """多户竖排裁剪图：每格 顶部红字标签 + 图内户号章"""
    sheets = []
    n = 0
    for i in range(0, len(records), per_sheet):
        group = records[i:i + per_sheet]
        tiles = []
        for r in group:
            paths = _meter_paths(r)
            if not paths:
                continue
            im = Image.open(paths[0])
            im = ImageOps.exif_transpose(im).convert("RGB")
            w, h = im.size
            c = im.crop((0, int(h * top), w, int(h * bot)))
            nw = target_w
            nh = max(1, int(c.size[1] * nw / c.size[0]))
            c = c.resize((nw, nh), Image.LANCZOS)
            _stamp(c, str(r["record_id"]), stamp_size)
            tiles.append((str(r["record_id"]), c))
        if not tiles:
            continue
        W = max(t.size[0] for _, t in tiles)
        H = sum(t.size[1] + 40 for _, t in tiles)
        canvas = Image.new("RGB", (W, H), "white")
        lf = _font(label_size)
        y = 0
        for rid, t in tiles:
            dr = ImageDraw.Draw(canvas)
            dr.text((8, y + 6), rid, font=lf, fill=(200, 0, 0))
            canvas.paste(t, (0, y + 40))
            y += t.size[1] + 40
        out = os.path.join(os.path.dirname(out_prefix), "%s%d.jpg" % (os.path.basename(out_prefix), n))
        canvas.save(out, quality=88)
        sheets.append(out.replace("\\", "/"))
        n += 1
    return sheets


# ============================================================
# pack —— 领批 + 出全套读图材料
# ============================================================
def _next_batch_no():
    if not os.path.isdir(PACKS):
        return 1
    mx = 0
    for d in os.listdir(PACKS):
        m = re.fullmatch(r"b(\d+)", d)
        if m:
            mx = max(mx, int(m.group(1)))
    return mx + 1


def _records_from_db(rids):
    """手动指定 rids 时（调试用），从 DB 构造与 next 一致的结构"""
    con = _connect()
    out = []
    for rid in rids:
        r = con.execute("SELECT * FROM records WHERE id=?", (int(rid),)).fetchone()
        if not r:
            continue
        items = []
        for p in con.execute(
                "SELECT id, items_code, local, downloaded, category FROM photos WHERE record_id=? ORDER BY id", (rid,)):
            if p["downloaded"] != 1 or not p["local"]:
                continue
            path = os.path.join(D.BASE, p["local"]).replace("\\", "/")
            if os.path.exists(path):
                items.append({"photo_id": p["id"], "items_code": p["items_code"],
                              "items_name": D.item_display(p["items_code"], p["category"],
                                                           r["company"]),
                              "path": path})
        out.append({"record_id": int(rid), "cust_no": r["cust_no"], "cust_name": r["cust_name"],
                    "inspector": r["inspector"], "check_time": r["check_time"],
                    "company": r["company"],
                    "meter_no": r["meter_no"], "meter_type": r["meter_type"],
                    "meter_brand": r["meter_brand"], "address": r["address"],
                    "community": r["community"], "photo_count": r["photo_count"],
                    "dup_hints": [], "items": items})
    con.close()
    return out


def cmd_pack(a):
    n = a.batch or _next_batch_no()
    pdir = os.path.join(PACKS, "b%d" % n)
    os.makedirs(pdir, exist_ok=True)

    if a.rids:
        rids = [x for x in re.split(r"[,\s]+", a.rids) if x]
        records = _records_from_db(rids)
    else:
        cmd = [sys.executable, os.path.join(BASE, "cli.py"), "next",
               "--n", str(a.n), "--items", D.meter_code((a.company or "").upper())]
        if a.company:
            cmd += ["--company", a.company]
        if a.month:
            cmd += ["--month", a.month]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        raw = (p.stdout or "").strip()
        d = json.loads(raw[raw.find("{"):]) if raw.find("{") >= 0 else {"empty": True}
        if d.get("empty"):
            print("没有待判定的户了（或该条件下为空）:", d.get("hint", ""))
            return
        records = d["records"]

    if not records:
        print("没有可打包的记录")
        return

    # batch.json（保持与旧格式兼容）
    bj = os.path.join(pdir, "batch.json")
    json.dump({"count": len(records), "records": records},
              open(bj, "w", encoding="utf-8"), ensure_ascii=False)

    # 水印联排：3 户/张、宽 1400（读取时不被压缩，字比旧版大 ~75%）
    # 2026-10-16 用户定：默认改为直读完整原图（--no-sheets），不再用切图联排做主读
    wm, mt = [], []
    if not a.no_sheets:
        wm = _stack(records, WM_TOP, WM_BOT, os.path.join(pdir, "wm"),
                    per_sheet=3, target_w=1400, stamp_size=46, label_size=42)
        # 表号联排：4 户/张、宽 1000（辅助定位，主判定用直读原图）
        mt = _stack(records, MT_TOP, MT_BOT, os.path.join(pdir, "mt"),
                    per_sheet=4, target_w=1000, stamp_size=36, label_size=34)

    # checklist.md —— 直读原图的路径清单
    lines = ["# 批次 %d — %d 户" % (n, len(records)),
             "", "生成时间: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "", "## 户清单（直读原图用）", ""]
    for r in records:
        paths = _meter_paths(r)
        warn = "" if paths else " ⚠无表具照片"
        lines.append("- **%s** %s %s | 工单 %s | 档案表号 %s | 安检员 %s%s" % (
            r["record_id"], r["cust_no"], r.get("cust_name") or "",
            r.get("check_time") or "", r.get("meter_no") or "",
            r.get("inspector") or "", warn))
        for p in paths:
            lines.append("    - `%s`" % p)
    ck = os.path.join(pdir, "checklist.md")
    open(ck, "w", encoding="utf-8").write("\n".join(lines))

    result = {
        "batch": n, "dir": pdir.replace("\\", "/"), "n": len(records),
        "wm_sheets": wm, "mt_sheets": mt, "checklist": ck.replace("\\", "/"),
        "batch_json": bj.replace("\\", "/"),
        "records": [{"record_id": r["record_id"], "cust_no": r["cust_no"],
                     "cust_name": r.get("cust_name"), "inspector": r.get("inspector"),
                     "check_time": r.get("check_time"), "meter_no": r.get("meter_no")}
                    for r in records],
    }
    print("批次 %d：%d 户 → %s" % (n, len(records), pdir.replace("\\", "/")))
    print("  水印图 %d 张: %s" % (len(wm), " ".join(os.path.basename(x) for x in wm)))
    print("  表号图 %d 张: %s" % (len(mt), " ".join(os.path.basename(x) for x in mt)))
    for r in result["records"]:
        print("  %s %s %s | %s | %s" % (r["record_id"], r["cust_no"], r["cust_name"] or "",
                                        r["check_time"] or "", r["meter_no"] or ""))
    print("JSON::" + json.dumps(result, ensure_ascii=False))


# ============================================================
# gap —— 水印与工单时间差批量核算（168h 一刀切）
# ============================================================
def cmd_gap(a):
    pairs = []
    if a.file:
        d = json.load(open(a.file, encoding="utf-8"))
        for p in d:
            pairs.append((p["record_id"], p.get("watermark_time") or p.get("watermark_date")))
    if a.pairs:
        for seg in a.pairs.split(","):
            seg = seg.strip()
            if not seg:
                continue
            rid, _, wt = seg.partition("=")
            pairs.append((rid.strip(), wt.strip()))
    if not pairs:
        print("用法: gap --pairs \"13755=2026-09-02 13:00:04,13769=2026-08-13 08:47:51\"")
        return
    con = _connect()
    rows = []
    for rid, wt in pairs:
        rec = con.execute("SELECT cust_no, inspector, check_time FROM records WHERE id=?",
                          (int(rid),)).fetchone()
        if not rec:
            rows.append((int(rid), "?", "?", wt, "?", "record 不存在"))
            continue
        c, w = _parse_time(rec["check_time"]), _parse_time(wt)
        if not c or not w:
            rows.append((int(rid), rec["cust_no"], rec["inspector"], wt,
                         rec["check_time"], "时间解析失败"))
            continue
        gap = abs((c - w).total_seconds()) / 3600.0
        if gap >= 168:
            verdict = "→ critical（超7天 %.1fh）" % gap
        elif gap >= 160:
            verdict = "！边界 %.1fh 建议 zoom 复核" % gap
        elif gap >= 144:
            verdict = "边界带 %.1fh" % gap
        else:
            verdict = "不标记 %.1fh" % gap
        rows.append((int(rid), rec["cust_no"], rec["inspector"], wt, rec["check_time"], verdict))
    con.close()
    print("%-8s %-14s %-8s %-20s %-20s %s" % ("rid", "客户", "安检员", "水印读值", "工单时间", "判定"))
    for row in rows:
        print("%-8s %-14s %-8s %-20s %-20s %s" % row)


# ============================================================
# zoom —— 单户单区域高倍放大
# 注：读图端会把大图缩到长边 1568px，所以"有效放大倍数"由【裁剪区域】决定，
#     裁得越紧显示越大。水印复核默认只裁左侧 56%（水印文字在左）、底部 16%。
# ============================================================
def cmd_zoom(a):
    rids = [x for x in re.split(r"[,\s]+", a.rids) if x]
    if not rids:
        print("用法: zoom --rids 13755,13769 --zone wm|mt --scale 3")
        return
    # zone -> (x0, x1, top, bot, scale)
    defaults = {"wm": (0.00, 0.56, 0.84, 1.00, 3), "mt": (0.00, 1.00, 0.25, 0.62, 3)}
    x0, x1, top, bot, scale = defaults.get(a.zone, defaults["wm"])
    if a.x0:
        x0 = a.x0
    if a.x1:
        x1 = a.x1
    if a.top:
        top = a.top
    if a.bot:
        bot = a.bot
    if a.scale:
        scale = a.scale
    os.makedirs(ZOOM_DIR, exist_ok=True)
    con = _connect()
    saved = []
    for rid in rids:
        rec = con.execute("SELECT company FROM records WHERE id=?", (int(rid),)).fetchone()
        mc = D.meter_code(rec["company"] if rec else "")
        ph = con.execute(
            "SELECT id, local FROM photos WHERE record_id=? AND items_code=? AND downloaded=1 ORDER BY id",
            (int(rid), mc)).fetchall()
        if not ph:
            print("⚠ %s 无已下载的表具照片（%s，可能还没下载）" % (rid, mc))
            continue
        for p in ph:
            src = os.path.join(D.BASE, p["local"])
            if not os.path.exists(src):
                print("⚠ %s 照片文件不存在: %s" % (rid, src))
                continue
            im = Image.open(src)
            im = ImageOps.exif_transpose(im).convert("RGB")
            w, h = im.size
            c = im.crop((int(w * x0), int(h * top), int(w * x1), int(h * bot)))
            c = c.resize((c.size[0] * scale, c.size[1] * scale), Image.LANCZOS)
            from PIL import ImageEnhance
            c = ImageEnhance.Contrast(c).enhance(1.15)
            # 文件名带区域参数（避免不同裁切参数互相覆盖）
            tag = "%d_%d_%d_%d" % (round(x0 * 100), round(x1 * 100), round(top * 100), round(bot * 100))
            out = os.path.join(ZOOM_DIR, "%s_%s_%s_%sx.jpg" % (rid, a.zone, tag, scale))
            c.save(out, quality=92)
            saved.append(out.replace("\\", "/"))
    con.close()
    for s in saved:
        print("saved", s)


# ============================================================
# precheck —— 提交前预检（防误报/防漏报的硬性检查）
# ============================================================
def cmd_precheck(a):
    payloads = json.load(open(a.file, encoding="utf-8"))
    if isinstance(payloads, dict):
        payloads = payloads.get("records", payloads.get("payloads", []))
    con = _connect()
    n_err = n_warn = 0
    print("%-8s %-4s %s" % ("rid", "级别", "内容"))
    for p in payloads:
        rid = int(p.get("record_id") or 0)
        rec = con.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
        if not rec:
            print("%-8s %-4s 不存在该 record_id" % (rid, "ERR"))
            n_err += 1
            continue
        msgs = []   # (level, text)
        mn, mr = _norm(rec["meter_no"]), _norm(p.get("meter_read"))
        # 1) 表号自洽
        if p.get("meter_match") is True and not mr:
            msgs.append(("ERR", "meter_match=true 但 meter_read 为空"))
        if p.get("meter_readable") is False and mr:
            msgs.append(("ERR", "meter_readable=false 但 meter_read=%s 有值" % p.get("meter_read")))
        if p.get("meter_visible") is False and mr:
            msgs.append(("ERR", "meter_visible=false 但 meter_read 有值"))
        # 2) 编辑距离（防单字符误读）
        if mr and mn:
            if mr == mn:
                msgs.append(("OK", "表号与档案完全一致"))
            elif mr in mn or mn in mr:
                msgs.append(("INFO", "读到的是档案号的一部分（核心段/短号口径，comment 需说明）"))
            else:
                d = _lev(mr, mn)
                if 1 <= d <= 2:
                    msgs.append(("WARN", "表号 %s vs 档案 %s 编辑距离=%d —— 疑似单字符差异，务必 zoom --zone mt 复核！" % (p.get("meter_read"), rec["meter_no"], d)))
                else:
                    msgs.append(("WARN", "表号 %s 与档案 %s 差异较大（若属特殊形态口径请确认 comment）" % (p.get("meter_read"), rec["meter_no"])))
        # 3) 时间检查
        wt = p.get("watermark_time") or p.get("watermark_date")
        if wt:
            w = _parse_time(wt)
            c = _parse_time(rec["check_time"])
            if not w:
                msgs.append(("ERR", "watermark_time 无法解析: %r" % wt))
            elif c:
                gap = abs((c - w).total_seconds()) / 3600.0
                if gap >= 168:
                    msgs.append(("INFO", "gap=%.1fh 超7天，引擎将自动记 critical（确认水印读值无误）" % gap))
                elif gap >= 160:
                    msgs.append(("WARN", "gap=%.1fh 紧邻168h阈值 —— 务必 zoom --zone wm 复核水印！" % gap))
                elif gap >= 144:
                    msgs.append(("INFO", "gap=%.1fh 边界带，注意复核" % gap))
                elif w == c:
                    msgs.append(("WARN", "水印与工单时间完全相同（秒级一致），核对是否误抄成工单时间"))
        if p.get("check_time") and p["check_time"] != rec["check_time"]:
            msgs.append(("ERR", "payload 里的 check_time 与 DB 权威值不一致（易误判，建议删除该字段）"))
        # 4) 问题项合法性
        for it in p.get("issues", []) or []:
            if it.get("kind") == "表号无法辨认" and mr:
                msgs.append(("WARN", "标了「表号无法辨认」但 meter_read 有值"))
        lvl_rank = {"ERR": 0, "WARN": 1, "INFO": 2, "OK": 3}
        msgs.sort(key=lambda m: lvl_rank[m[0]])
        shown = False
        for lvl, text in msgs:
            if lvl == "OK":
                continue
            print("%-8s %-4s %s" % (rid, lvl, text))
            shown = True
            if lvl == "ERR":
                n_err += 1
            elif lvl == "WARN":
                n_warn += 1
        if not shown and msgs:
            print("%-8s OK   %s" % (rid, msgs[0][1] if msgs else ""))
    con.close()
    print("—— 预检完成：ERR %d，WARN %d（ERR 必须修正，WARN 逐条确认后再提交）" % (n_err, n_warn))
    if n_err:
        sys.exit(1)


# ============================================================
# postcheck —— 提交后核对（机械追加的问题一目了然）
# ============================================================
def cmd_postcheck(a):
    pdir = os.path.join(PACKS, "b%d" % a.batch)
    bj = os.path.join(pdir, "batch.json")
    if not os.path.exists(bj):
        print("找不到 %s" % bj)
        return
    d = json.load(open(bj, encoding="utf-8"))
    rids = [r["record_id"] for r in d["records"]]
    declared = {}
    if a.payload and os.path.exists(a.payload):
        for p in json.load(open(a.payload, encoding="utf-8")):
            declared[int(p["record_id"])] = [it.get("kind") for it in (p.get("issues") or [])]
    con = _connect()
    tot_c = tot_w = 0
    for rid in rids:
        rows = con.execute(
            "SELECT kind, severity, detail FROM issues WHERE record_id=? ORDER BY severity", (rid,)).fetchall()
        if not rows:
            continue
        marks = []
        for r in rows:
            auto = ""
            if rid in declared and r["kind"] not in declared[rid]:
                auto = " ★机械追加"     # payload 没声明、程序加的 → 重点核对
            marks.append("[%s]%s%s" % (r["severity"], r["kind"], auto))
            if r["severity"] == "critical":
                tot_c += 1
            elif r["severity"] == "warning":
                tot_w += 1
        rec = con.execute("SELECT cust_no FROM records WHERE id=?", (rid,)).fetchone()
        print("%-8s %-14s %s" % (rid, rec["cust_no"] if rec else "?", " | ".join(marks)))
    con.close()
    print("—— 本批合计 critical %d / warning %d（★=payload 未声明、程序机械追加，需复核）" % (tot_c, tot_w))


# ============================================================
def main():
    ap = argparse.ArgumentParser(description="安检抽查批处理工具")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("pack", help="领批+生成读图材料")
    p.add_argument("--n", type=int, default=32)
    p.add_argument("--batch", type=int, default=0, help="批次号，默认自动递增")
    p.add_argument("--company", default="")
    p.add_argument("--month", default="")
    p.add_argument("--rids", default="", help="调试用：手动指定户（逗号分隔）")
    p.add_argument("--no-sheets", action="store_true",
                   help="不生成联排切图，只用 checklist 直读完整原图（用户 2026-10-16 定的默认方式）")
    p.set_defaults(fn=cmd_pack)

    p = sub.add_parser("gap", help="水印与工单间隔批量核算")
    p.add_argument("--pairs", default="", help='"rid=时间,rid=时间"')
    p.add_argument("--file", default="", help="JSON: [{record_id, watermark_time}]")
    p.set_defaults(fn=cmd_gap)

    p = sub.add_parser("zoom", help="单户单区域高倍放大")
    p.add_argument("--rids", required=True)
    p.add_argument("--zone", default="wm", choices=["wm", "mt"])
    p.add_argument("--scale", type=int, default=0)
    p.add_argument("--x0", type=float, default=0)
    p.add_argument("--x1", type=float, default=0)
    p.add_argument("--top", type=float, default=0)
    p.add_argument("--bot", type=float, default=0)
    p.set_defaults(fn=cmd_zoom)

    p = sub.add_parser("precheck", help="提交前预检")
    p.add_argument("--file", required=True)
    p.set_defaults(fn=cmd_precheck)

    p = sub.add_parser("postcheck", help="提交后核对")
    p.add_argument("--batch", type=int, required=True)
    p.add_argument("--payload", default="")
    p.set_defaults(fn=cmd_postcheck)

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return
    args.fn(args)


if __name__ == "__main__":
    main()
