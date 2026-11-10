# -*- coding: utf-8 -*-
"""专项任务 Excel 导出（一户一行，与月度明细同构，问题列合并）

方案 B（2026-11-09 用户定）：
  * 「问题」列 = 系统发现的类型 + 人工意见，顿号连成一段
  * 「说明」列 = 系统逐条描述 + 人工备注
  * 表具照片 / 问题照片 / 全部照片 三列都给 URL
  * 末列「公司反馈」留空
"""
import io, datetime
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

FONT = Font(name="微软雅黑", size=10)
BOLD = Font(name="微软雅黑", size=10, bold=True)
HEAD_FILL = PatternFill("solid", fgColor="EEF2F7")
THIN = Side(style="thin", color="D9DEE5")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
FILL_CR = PatternFill("solid", fgColor="FFF2F0")
FILL_WA = PatternFill("solid", fgColor="FFFBE6")
FILL_FB = PatternFill("solid", fgColor="F2F7FF")

COLS = ["序号", "公司", "客户编号", "客户姓名", "地址", "安检员", "安检时间", "档案表号",
        "最终判定", "问题（系统+人工）", "说明", "照片数",
        "AI 判别", "第一次人工复核", "第二次人工复核", "公司反馈"]
WIDTHS = [5, 8, 15, 10, 32, 9, 17, 15, 9, 38, 44, 7, 12, 15, 15, 22]


def _sev(v):
    return {"critical": "严重提示", "warning": "一般提示", "none": "正常"}.get(v, "待判定")


def _merge(row):
    """系统问题类型 + 人工意见 → 一段（顿号分隔）"""
    kinds = []
    for i in row["issues"]:
        k = i.get("kind") or ""
        if k and k not in kinds:
            kinds.append(k)
    manual = (row.get("note") or "").strip()
    man = [x.strip() for x in manual.replace("；", ";").replace("，", ";").split(";")
           if x.strip()]
    out = kinds + [x for x in man if x not in kinds]
    return "、".join(out)


def _detail(row):
    parts = []
    for i in row["issues"]:
        if i.get("detail"):
            parts.append("%s：%s" % (i.get("kind"), i["detail"]))
    note = (row.get("note") or "").strip()
    if note:
        parts.append("人工意见：%s" % note)
    return "\n".join(parts)


def build(summary, rows, company_name, month_label):
    wb = Workbook()
    ws = wb.active
    ws.title = "专项明细"
    ws.append(COLS)
    for c in range(1, len(COLS) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = BOLD
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER
        ws.column_dimensions[get_column_letter(c)].width = WIDTHS[c - 1]
    ws.row_dimensions[1].height = 28
    ws.freeze_panes = "A2"

    for i, r in enumerate(rows, 1):
        ph = [p for p in (r.get("photos") or []) if not p.get("empty")]
        vals = [i, company_name, r["cust_no"], r.get("cust_name") or "",
                (r.get("community") or "") + " " + (r.get("address") or ""),
                r["inspector"], r.get("check_time") or "", r.get("meter_no") or "",
                _sev(r.get("review") or r["eff"]), _merge(r), _detail(r), len(ph),
                _sev(r.get("ai_verdict") or ""), _sev(r.get("rv1") or "") if r.get("rv1") else "—",
                _sev(r.get("review") or "") if r.get("review") else "—"]
        rr = ws.max_row + 1
        for c, v in enumerate(vals, 1):
            cell = ws.cell(row=rr, column=c, value=v)
            cell.font = FONT
            cell.border = BORDER
            cell.alignment = Alignment(wrap_text=c in (5, 10, 11), vertical="top")
            if c == 7:
                cell.number_format = "yyyy-mm-dd hh:mm"
        fb = ws.cell(row=rr, column=16, value="")
        fb.fill = FILL_FB
        fb.border = BORDER
        fill = FILL_CR if r["eff"] == "critical" else (FILL_WA if r["eff"] == "warning" else None)
        if fill:
            for c in (1, 2, 3, 4, 6, 8, 9):
                ws.cell(row=rr, column=c).fill = fill

    # 汇总 sheet
    ws2 = wb.create_sheet("汇总")
    t = summary["totals"]
    ws2.append(["专项任务", summary["task"]["name"]])
    ws2.append(["公司 / 月份", "%s %s" % (company_name, month_label)])
    ws2.append(["纳入户数（全量）", t["total"]])
    ws2.append(["已判定", t["judged"]])
    ws2.append(["全景符合率", "%.1f%%" % t["rate"]])
    ws2.append(["原口径符合率", ("%.1f%%" % t["rate_m"]) if t["rate_m"] is not None else "—"])
    ws2.append([])
    ws2.append(["安检员", "纳入户", "严重", "一般", "合格", "剔除", "全景符合率", "原口径符合率"])
    for c in range(1, 9):
        cell = ws2.cell(row=ws2.max_row, column=c)
        cell.font = BOLD
        cell.fill = HEAD_FILL
    for a in summary["inspectors"]:
        ws2.append([a["name"], a["total"], a["crit"], a["warn"], a["pass_"],
                    a["excluded"], "%.1f%%" % a["rate"],
                    ("%.1f%%" % a["rate_m"]) if a["rate_m"] is not None else "—"])
    for i, row in enumerate(ws2.iter_rows(), 1):
        for cell in row:
            cell.font = BOLD if i == 8 else FONT
    ws2.column_dimensions["A"].width = 20
    for col in "BCDEFGH":
        ws2.column_dimensions[col].width = 12

    wb.properties.title = "专项复核明细"
    wb.properties.creator = "安检照片AI质检系统"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def filename(company_name, month_label, n):
    return "专项复核明细_%s_%s_%d户.xlsx" % (company_name, month_label, n)
