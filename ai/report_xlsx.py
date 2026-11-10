# -*- coding: utf-8 -*-
"""安检抽查明细表 —— Excel 导出 / 公司反馈回填导入

设计口径（2026-11-05 用户定）：
  1. 一户一行，与「查看报告」明细表完全同口径；同一户的多条问题在单元格内用「，」隔开。
  2. 不嵌图片，只写照片 URL（可点击超链）。
  3. 表格上不出现月份、不出现任何汇总数字（抽查数/严重数/符合率等一律不进表），
     公司/月份信息只落在**文件名**和**工作簿属性**里，避免公司改动或误读。
     公司名称单独成列（用户明确要求：导出后要能看到是哪家）。
  4. 最后留空列「公司反馈」，公司只填这一列，回传后按 客户编号 回填到 reviews。

两个 sheet：
  ① 问题户   —— 有严重/提示问题的户（误判户不含，与报告默认视图一致）
  ② 全部抽查户 —— 全部 sampled=1 的户，供公司自查

导入（`python report_xlsx.py --import 文件.xlsx` 或 POST /api/report/xlsx/import）：
  读「问题户」sheet 的 客户编号 + 公司反馈 → 追加进 reviews.note_manual / note，
  不改判定、不动 issues；重复导入同一份文件不会重复追加。
"""
import io
import os
import re
import datetime

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

import db as D
import report_data as RD
import report_render as RR

FEEDBACK_MARK = "【公司反馈】"

# 列定义：(表头, 宽度)
COLS_PROBLEM = [
    ("序号", 6), ("公司名称", 10), ("客户编号", 16), ("客户名称", 12), ("地址", 46),
    ("安检员", 10), ("检查时间", 19), ("档案表号", 18), ("判定", 10),
    ("问题", 52), ("AI批注", 30), ("人工审核意见", 34),
    ("表具照片链接", 46), ("问题照片链接", 40), ("公司反馈", 40),
]
COLS_ALL = [
    ("序号", 6), ("公司名称", 10), ("客户编号", 16), ("客户名称", 12), ("地址", 46),
    ("安检员", 10), ("检查时间", 19), ("档案表号", 18), ("判定", 10),
    ("问题条数", 9), ("问题", 52), ("表具照片链接", 46),
]
HDR_FILL = PatternFill("solid", fgColor="1668DC")
HDR_FONT = Font(bold=True, color="FFFFFF", size=10.5)
CELL_FONT = Font(size=10.5)
LINK_FONT = Font(size=10, color="1155CC", underline="single")
FILL_CRIT = PatternFill("solid", fgColor="FFF1F0")
FILL_WARN = PatternFill("solid", fgColor="FFFBEA")
THIN = "D9DEE5"


def _s(v):
    return "" if v is None else str(v).strip()


def _cat(parts):
    """多段文本用中文逗号连接（用户口径）"""
    out = []
    for p in parts:
        p = _s(p)
        if p and p not in out:
            out.append(p)
    return "，".join(out)


def _dt(v):
    """'2026-10-08 09:12:39' → datetime（失败则原样返回字符串）"""
    v = _s(v)
    if not v:
        return ""
    for s, f in ((v[:19], "%Y-%m-%d %H:%M:%S"),
                 (v[:16], "%Y-%m-%d %H:%M"),
                 (v[:10], "%Y-%m-%d")):
        try:
            return datetime.datetime.strptime(s, f)
        except Exception:
            pass
    return v


def _level_label(sev):
    return {"critical": "严重", "warning": "提示"}.get(sev, "")


# ---------------------------------------------------------------- 取数
def collect(con, code, month=""):
    """返回 {company,name,month, problem:[户], all:[户]}，口径与报告明细一致"""
    code = (code or "").upper()
    name = D.COMPANY_NAME.get(code, code)
    rw = "r.company=?" + (" AND substr(r.check_time,1,7)=?" if month else "")
    ra = [code] + ([month] if month else [])
    iw = "i.company=?" + (" AND i.month=?" if month else "")
    ia = [code] + ([month] if month else [])

    recs = con.execute(
        f"""SELECT r.id, r.cust_no, r.cust_name, r.address, r.inspector,
                   r.check_time, r.meter_no
              FROM records r WHERE {rw} AND r.sampled=1
             ORDER BY r.cust_no""", ra).fetchall()
    if not recs:
        return {"company": code, "name": name, "month": month,
                "problem": [], "all": []}
    ids = [r["id"] for r in recs]

    # 问题（v_issue_eff 已按人工复核裁决改档，误判 → NULL 自动剔除）
    issues = {}
    photo_rows = con.execute(
        f"""SELECT i.record_id, i.kind, i.detail, i.eff_severity AS severity,
                   i.items_code, p.url
              FROM v_issue_eff i
              LEFT JOIN photos p ON p.id = COALESCE(i.photo_id,
                   (SELECT ph.id FROM photos ph WHERE ph.record_id=i.record_id
                     ORDER BY CASE ph.items_code WHEN ? THEN 0
                              WHEN 'overall_path' THEN 2 ELSE 1 END, ph.id LIMIT 1))
             WHERE {iw} AND i.eff_severity IN ('critical','warning')
               AND i.record_id IN (SELECT r.id FROM records r WHERE {rw} AND r.sampled=1)
             ORDER BY CASE i.eff_severity WHEN 'critical' THEN 0 ELSE 1 END,
                      i.kind""", [D.meter_code(code)] + ia + ra).fetchall()
    for r in photo_rows:
        issues.setdefault(r["record_id"], []).append(r)

    # 复核意见（每户一条）
    reviews = {}
    for r in con.execute(
            "SELECT record_id, verdict, note, note_auto, note_manual "
            "FROM reviews WHERE company=?" + (" AND month=?" if month else ""), ra):
        reviews[r["record_id"]] = r

    meter_ph = RR.meter_photo_map(con, code, ids)

    def meter_urls(rid):
        out = []
        for _ic, _pth, ur, _cat in meter_ph.get(rid, []):
            ur = _s(ur)
            if ur and ur not in out:
                out.append(ur)
        return out

    all_rows, prob_rows = [], []
    for r in recs:
        rid = r["id"]
        its = issues.get(rid, [])
        rv = reviews.get(rid)
        sev = ""
        if any(i["severity"] == "critical" for i in its):
            sev = "critical"
        elif its:
            sev = "warning"
        prob_text = _cat(i["kind"] for i in its)
        note_text = _cat(i["detail"] for i in its)
        m_urls = meter_urls(rid)
        # 问题照片：issue 挂的那张，且不在表具图里（避免同一张重复出现）
        q_urls = []
        for i in its:
            ur = _s(i["url"])
            if ur and ur not in m_urls and ur not in q_urls:
                q_urls.append(ur)
        item = {
            "cust_no": _s(r["cust_no"]), "cust_name": _s(r["cust_name"]),
            "address": _s(r["address"]), "inspector": _s(r["inspector"]),
            "check_time": _dt(r["check_time"]), "meter_no": _s(r["meter_no"]),
            "level": _level_label(sev), "n_issue": len(its),
            "problem": prob_text, "ai_note": note_text,
            "meter_urls": m_urls, "issue_urls": q_urls,
            "review_note": (RR.review_text(rv["note"], rv["note_auto"], rv["note_manual"])
                            if rv else ""),
            # 导入时若无复核记录，拿这个当兜底判定
            "sev_code": sev,
        }
        all_rows.append(item)
        if its:
            prob_rows.append(item)
    return {"company": code, "name": name, "month": month,
            "problem": prob_rows, "all": all_rows}


# ---------------------------------------------------------------- 导出
def _style_header(ws, cols):
    for c, (title, width) in enumerate(cols, start=1):
        cell = ws.cell(row=1, column=c, value=title)
        cell.fill = HDR_FILL
        cell.font = HDR_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(c)].width = width
    ws.row_dimensions[1].height = 24
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}1"


def _write_link(ws, row, col, urls):
    """一个单元格放多条 URL：换行分隔，超链接指向第一条"""
    urls = [u for u in urls if u]
    cell = ws.cell(row=row, column=col, value="\n".join(urls) if urls else "")
    cell.font = LINK_FONT if urls else CELL_FONT
    cell.alignment = Alignment(wrap_text=True, vertical="top")
    if urls:
        cell.hyperlink = urls[0]


def _fill_for(level):
    return FILL_CRIT if level == "严重" else (FILL_WARN if level == "提示" else None)


def to_xlsx(data):
    """生成 xlsx 二进制"""
    wb = Workbook()
    name = data["name"]
    month_txt = RR.month_label(data["month"]) if data["month"] else ""

    # ---- sheet ① 问题户（一户一行）
    ws = wb.active
    ws.title = "问题户"
    _style_header(ws, COLS_PROBLEM)
    for idx, it in enumerate(data["problem"], start=1):
        vals = [idx, name, it["cust_no"], it["cust_name"], it["address"],
                it["inspector"], it["check_time"], it["meter_no"], it["level"],
                it["problem"], it["ai_note"], it["review_note"]]
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=idx + 1, column=c, value=v)
            cell.font = CELL_FONT
            cell.alignment = Alignment(
                wrap_text=c in (5, 10, 11, 12), vertical="top")
            if c == 7:
                cell.number_format = "yyyy-mm-dd hh:mm"
        _write_link(ws, idx + 1, 13, it["meter_urls"])
        _write_link(ws, idx + 1, 14, it["issue_urls"])
        fb = ws.cell(row=idx + 1, column=15, value="")
        fb.fill = PatternFill("solid", fgColor="F2F7FF")
        fb.alignment = Alignment(wrap_text=True, vertical="top")
        f = _fill_for(it["level"])
        if f:
            for c in (1, 2, 3, 4, 6, 8, 9):
                ws.cell(row=idx + 1, column=c).fill = f

    # ---- sheet ② 全部抽查户（一户一行）
    ws2 = wb.create_sheet("全部抽查户")
    _style_header(ws2, COLS_ALL)
    for idx, it in enumerate(data["all"], start=1):
        vals = [idx, name, it["cust_no"], it["cust_name"], it["address"],
                it["inspector"], it["check_time"], it["meter_no"],
                it["level"] or "正常", it["n_issue"], it["problem"]]
        for c, v in enumerate(vals, start=1):
            cell = ws2.cell(row=idx + 1, column=c, value=v)
            cell.font = CELL_FONT
            cell.alignment = Alignment(wrap_text=c in (5, 11), vertical="top")
            if c == 7:
                cell.number_format = "yyyy-mm-dd hh:mm"
        _write_link(ws2, idx + 1, 12, it["meter_urls"])
        f = _fill_for(it["level"])
        if f:
            for c in (1, 2, 3, 4, 6, 8, 9):
                ws2.cell(row=idx + 1, column=c).fill = f

    # ---- 识别信息：只进文件属性，不进表格（用户 2026-11-05 定）
    wb.properties.title = f"安检抽查明细_反馈表_{name}"
    wb.properties.subject = f"{data['company']}|{data['month']}"
    wb.properties.creator = "安检照片AI质检系统"
    wb.properties.description = (
        f"公司={name}（{data['company']}） 月份={month_txt or '全部'} "
        f"导出={datetime.datetime.now():%Y-%m-%d %H:%M}")
    wb.properties.keywords = f"{data['company']},{data['month']}"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def filename(company_code, month):
    name = D.COMPANY_NAME.get((company_code or "").upper(), company_code)
    mt = RR.month_label(month) if month else "全部月份"
    return f"安检抽查明细_{name}_{mt}_{datetime.date.today():%Y%m%d}.xlsx"


def export_file(code, month=""):
    con = D.connect()
    try:
        data = collect(con, code, month)
    finally:
        con.close()
    return to_xlsx(data), filename(code, month)


# ---------------------------------------------------------------- 导入
def _meta_from_book(wb, fname):
    """从工作簿属性 / 文件名里捞 (公司代码, 月份)；捞不到返回 (None, None)"""
    code = month = None
    sub = _s(wb.properties.subject)
    if "|" in sub:
        a, b = sub.split("|", 1)
        if a.strip().upper() in D.COMPANY_NAME:
            code, month = a.strip().upper(), b.strip() or None
    if not code:
        for k, v in D.COMPANY_NAME.items():
            if v in (fname or ""):
                code = k
                break
    if not month:
        m = re.search(r"(20\d{2})\D{0,2}(\d{1,2})\s*月", fname or "")
        if m:
            month = f"{m.group(1)}-{int(m.group(2)):02d}"
    return code, month


def import_feedback(data, fname=""):
    """把填好的 xlsx 回填进 reviews。返回统计结果 dict。

    - 只处理「问题户」sheet（或第一个 sheet）
    - 只认「客户编号」+「公司反馈」两列，其余列一律忽略（公司改了别的列也不影响）
    - 不改判定、不动 issues；写 note_manual / note，留 review_log 痕迹
    """
    wb = load_workbook(io.BytesIO(data), data_only=True)
    ws = wb["问题户"] if "问题户" in wb.sheetnames else wb.worksheets[0]
    code, month = _meta_from_book(wb, fname or "")

    hdr = {}
    for c in range(1, (ws.max_column or 0) + 1):
        t = _s(ws.cell(row=1, column=c).value).replace(" ", "")
        if t:
            hdr.setdefault(t, c)
    c_no = hdr.get("客户编号")
    c_fb = hdr.get("公司反馈") or hdr.get("公司反馈/整改说明")
    c_tm = hdr.get("检查时间")          # 用来消歧：同客户编号跨月重号时定位到正确月份
    if not c_no or not c_fb:
        return {"ok": False, "msg": "表头缺少「客户编号」或「公司反馈」列，"
                                    "请用系统导出的原表填写，不要改动表头"}

    rows = []
    for r in range(2, (ws.max_row or 0) + 1):
        no = _s(ws.cell(row=r, column=c_no).value)
        fb = _s(ws.cell(row=r, column=c_fb).value)
        if no and fb:
            mh = ""
            if c_tm:
                v = ws.cell(row=r, column=c_tm).value
                if isinstance(v, datetime.datetime):
                    mh = f"{v:%Y-%m}"
                else:
                    mm = re.match(r"(20\d{2})[-/年](\d{1,2})", _s(v))
                    if mm:
                        mh = f"{mm.group(1)}-{int(mm.group(2)):02d}"
            rows.append((no, fb, mh))
    if not rows:
        return {"ok": True, "updated": 0, "skipped": 0, "missing": [],
                "msg": "没有填写任何反馈内容"}

    con = D.connect()
    try:
        if not code:
            # 文件名被改过也能救：拿第一个客户编号反查公司
            first = con.execute("SELECT company FROM records WHERE cust_no=? "
                                "ORDER BY id DESC LIMIT 1", (rows[0][0],)).fetchone()
            code = first["company"] if first else ""
        if not code:
            return {"ok": False, "msg": "无法识别这份表属于哪家公司，"
                                        "请勿修改文件名，或用系统重新导出"}

        updated, skipped, missing, touched = 0, 0, [], []
        for no, fb, mh in rows:
            sql = "SELECT id, company, cust_no, check_time FROM records WHERE company=? AND cust_no=?"
            args = [code, no]
            use_m = mh or month
            if use_m:
                sql += " AND substr(check_time,1,7)=?"
                args.append(use_m)
            rec = con.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
            if not rec and use_m:
                rec = con.execute(
                    "SELECT id, company, cust_no, check_time FROM records "
                    "WHERE company=? AND cust_no=? ORDER BY id DESC LIMIT 1",
                    (code, no)).fetchone()
            if not rec:
                missing.append(no)
                continue
            rid = rec["id"]
            m = (rec["check_time"] or "")[:7] or month or \
                datetime.datetime.now().strftime("%Y-%m")
            old = con.execute("SELECT verdict, note, note_auto, note_manual "
                              "FROM reviews WHERE record_id=?", (rid,)).fetchone()
            add = f"{FEEDBACK_MARK}{fb}"
            if old and add in _s(old["note_manual"]) + _s(old["note"]):
                skipped += 1
                continue
            note_manual = _cat([_s(old["note_manual"]) if old else "", add])
            note = _cat([_s(old["note"]) if old else "",
                         _s(old["note_auto"]) if old else "", add])
            if old:
                verdict = old["verdict"] or ""
            else:
                # 该户集团还没人工审核：只存反馈内容，verdict 留空。
                # review_map 会过滤空 verdict，所以不会被算成「已审核」，
                # 等复核时照常判档（反馈文字已在 note 里，不会被覆盖）。
                verdict = ""
            con.execute(
                """INSERT OR REPLACE INTO reviews(record_id,company,month,seq,verdict,
                                                  note,note_auto,note_manual,reviewer,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (rid, code, m, 0, verdict, note,
                 _s(old["note_auto"]) if old else "", note_manual, "公司反馈导入"))
            con.execute(
                """INSERT INTO review_log(record_id,company,month,action,verdict_from,
                                          verdict_to,note,reviewer)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (rid, code, m, "import", _s(old["verdict"]) if old else "",
                 verdict, add, "公司反馈导入"))
            updated += 1
            touched.append(rid)
        con.commit()
        return {"ok": True, "company": code,
                "name": D.COMPANY_NAME.get(code, code), "month": month,
                "updated": updated, "skipped": skipped,
                "missing": missing[:50], "missing_n": len(missing)}
    finally:
        con.close()


# ---------------------------------------------------------------- CLI
def _main():
    import argparse
    ap = argparse.ArgumentParser(description="安检抽查明细 Excel 导出 / 回填导入")
    ap.add_argument("--company", help="公司代码 C1/C2/C3/C4/C5/C6")
    ap.add_argument("--month", default="", help="2026-10")
    ap.add_argument("--out", default="", help="输出路径（默认存到 安检报告/<N>月/）")
    ap.add_argument("--import", dest="imp", default="", help="回填的 xlsx 路径")
    a = ap.parse_args()

    if a.imp:
        with open(a.imp, "rb") as f:
            r = import_feedback(f.read(), os.path.basename(a.imp))
        print(r if not r.get("ok") else
              f"✓ {r['name']} {r.get('month') or ''} 回填 {r['updated']} 户，"
              f"跳过重复 {r['skipped']} 户，未匹配 {r['missing_n']} 户")
        if r.get("missing"):
            print("未匹配客户编号：", "，".join(r["missing"][:20]))
        return

    if not a.company:
        ap.error("需要 --company 或 --import")
    body, fname = export_file(a.company, a.month)
    out = a.out
    if not out:
        mt = RR.month_label(a.month) if a.month else "全部月份"
        out = os.path.join("E:\\Desktop\\安检抽查系统\\安检报告", mt, fname)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as f:
        f.write(body)
    print(f"✓ 已导出 {out}（{len(body)/1024:.0f} KB）")


if __name__ == "__main__":
    _main()
