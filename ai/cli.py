# -*- coding: utf-8 -*-
"""Agent 通道 —— 让 WorkBuddy 对话框里的模型（我）直接驱动质检流程

设计前提：识别不用外部 API，由 Agent 亲自读图判定。
程序负责：搬数据、下载、抽样、机械相似度计算、落库、报告。
Agent 负责：读图判断，调用 next 领任务、submit 交结果。

命令：
  python cli.py prepare --file <xlsx> [--company C5] [--ratio 0.1] [--limit 30]
        导入 Excel → 只取入户 → 同客户取最新 → 按安检员 10% 抽样
        → 建检查项 → 下载原图。不调用任何模型。
  python cli.py next --n 25 [--items items2,items1] [--all]
        领一批待判定的户，返回每户档案信息 + 本地图片绝对路径
  python cli.py submit --id <record_id> --json '<json>'
        提交一户的判定结果，落 checks/issues + 照片分拣
  python cli.py bulk --file <json文件>   批量提交（省去多次进程启动）
  python cli.py status                   进度
  python cli.py finish                   收尾：出报告、统计
  python cli.py reset --company C5      清空该公司所有检测结果（保留导入的照片）
"""
import os, re, sys, json, glob, shutil, sqlite3, argparse, datetime, hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db as D
import engine as E
import runner as R
from db import ITEMS_NAME, COMPANY_NAME, TIME_GAP_MIN

PY = sys.executable

# Agent 默认只需读这几项，够判定且省上下文
DEFAULT_ITEMS = ["items2", "items1", "items8", "overall_path"]


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def abs_local(local):
    return os.path.normpath(os.path.join(D.BASE, local))


# ============================================================
# 机械相似度：程序能算的，不要浪费 Agent 的视觉
# ============================================================
def _sig8x8(path):
    """16x16 灰度签名（实测 8x8 太粗，厨房场景全被判成重复）"""
    from PIL import Image
    import numpy as np
    with Image.open(path) as im:
        im = im.convert("L").resize((16, 16))
        a = np.asarray(im, dtype=np.float32).ravel()
    n = np.linalg.norm(a)
    return a / n if n else a


# 实测阈值（16x16，三户 105 对样本）：
#   items1 vs items8 造假：0.965 / 0.986 / 0.998  → 阈值 0.93 干净
#   overall_path 内部连拍：0.94~0.977
#   跨检查项偶然相似：0.93~0.97（不能当重复报）
TH_DUP = 0.97        # 同检查项内连拍
TH_MISFILE = 0.93    # items8 其实是工作单


def similarity(pa, pb):
    import numpy as np
    try:
        a, b = _sig8x8(pa), _sig8x8(pb)
        return float(np.dot(a, b))
    except Exception:
        return 0.0


def auto_mechanical(con, rec, photo_list, thresh=None):
    """程序自动判两类机械问题：
    1) 同检查项内照片高度重复（同一画面连拍）
    2) items8（热水器）与 items1（安检单）高度相似 → 热水器项拍的是工作单

    注意：跨检查项的相似不算「重复」——那是内容错位，交给 Agent 视觉判定更准，
    这里只报高置信度的两类。
    """
    res = []
    paths = [(p["id"], _ic(con, p["id"]), abs_local(p["local"]))
             for p in photo_list
             if p["local"] and os.path.exists(abs_local(p["local"]))]
    if len(paths) < 2:
        return res
    # 1) 同一检查项内部连拍
    #    注意：overall_path（厨房/客厅全景）天然长得像，16x16 也压不住，
    #    0.97+ 仍会误报。这项只输出参考，不自动生成问题——由 Agent 视觉确认。
    for i in range(len(paths)):
        for j in range(i + 1, len(paths)):
            if paths[i][1] != paths[j][1]:
                continue
            s = similarity(paths[i][2], paths[j][2])
            if s >= TH_DUP and paths[i][1] != "overall_path":
                res.append(("同检查项照片连拍重复", "warning",
                            f"「{ITEMS_NAME.get(paths[i][1],'')}」两张照片相似度 {s:.3f}，"
                            f"疑似同一画面连拍", paths[j][0]))
    # 2) items8 vs items1
    p8 = [x for x in paths if x[1] == "items8"]
    # 安检单所在 items_code 各公司不同（D 公司 items0 / C 公司 items7），必须按公司取
    FC = D.form_code(rec["company"])
    p1 = [x for x in paths if x[1] == FC]
    for a in p8:
        for b in p1:
            s = similarity(a[2], b[2])
            if s >= TH_MISFILE:
                # 同上：降为 info，不进人工复核队列
                res.append(("热水器项拍的是工作单", "info",
                            f"热水器项与安检单（{FC}）相似度 {s:.3f}，疑似同一张工作单", a[0]))
                break
    return res


_icc = {}


def _ic(con, photo_id):
    if photo_id not in _icc:
        r = con.execute("SELECT items_code FROM photos WHERE id=?", (photo_id,)).fetchone()
        _icc[photo_id] = r["items_code"] if r else ""
    return _icc[photo_id]


# ============================================================
# prepare
# ============================================================
def cmd_prepare(a):
    src = os.path.abspath(a.file)
    if not os.path.exists(src):
        return {"error": f"文件不存在: {src}"}
    company = (a.company or _guess_company(src)).upper()
    if company not in COMPANY_NAME:
        return {"error": f"无法识别公司: {company}，请用 --company 指定 {list(COMPANY_NAME)}"}

    cfg = D.load_config()
    cfg["sample_ratio"] = a.ratio if a.ratio else cfg.get("sample_ratio", 0.1)
    if a.workers:
        cfg["workers"] = a.workers
    D.save_config(cfg)

    job = R.Job(company, a.month, cfg)
    con = D.connect()
    try:
        # 导入前先清掉该公司旧结果，保留 records/photos
        if a.fresh:
            ids = [r["id"] for r in con.execute("SELECT id FROM records WHERE company=?", (company,))]
            if ids:
                ph = ",".join("?" * len(ids))
                for t in ("checks", "issues"):
                    con.execute(f"DELETE FROM {t} WHERE record_id IN ({ph})", ids)
                con.execute(f"DELETE FROM reviews WHERE record_id IN ({ph})", ids)
                con.commit()
            job.log("已清空该公司历史检测结果")

        job.state["phase"] = "import"
        n = job.import_excel(con, src)

        job.state["phase"] = "sample"
        picked = job.sample(con)

        # 试点限流：按安检员轮转取，避免全落在排前面的少数人头上
        if a.limit and len(picked) > a.limit:
            by = {}
            for rid in picked:
                insp = con.execute("SELECT inspector FROM records WHERE id=?", (rid,)).fetchone()["inspector"]
                by.setdefault(insp or "(未填)", []).append(rid)
            # 各安检员按比例分配名额，余数补给户数多的
            quota = {k: max(1, int(len(v) * a.limit / len(picked) + 0.5)) for k, v in by.items()}
            while sum(quota.values()) > a.limit:
                k = max(quota, key=lambda x: (quota[x] - len(by[x]), quota[x]))
                if quota[k] <= 1:
                    break
                quota[k] -= 1
            while sum(quota.values()) < a.limit:
                k = max(by, key=lambda x: len(by[x]) - quota[x])
                if quota[k] >= len(by[k]):
                    break
                quota[k] += 1
            keep = []
            for k, v in by.items():
                keep.extend(v[:quota[k]])
            keepset = set(keep)
            for rid in picked:
                con.execute("UPDATE records SET sampled=? WHERE id=?",
                            (1 if rid in keepset else 0, rid))
            con.commit()
            picked = keep
            job.log(f"试点限流：{len(by)} 名安检员，抽查 {a.limit} 户")

        job.state["phase"] = "check"
        nchk = job.build_checks(con, picked)

        job.state["phase"] = "download"
        t0 = datetime.datetime.now()
        job.download_all(con, picked)
        dt = (datetime.datetime.now() - t0).seconds

        got = con.execute("SELECT COUNT(*) c FROM photos WHERE downloaded=1 AND record_id IN (%s)"
                          % ",".join("?" * len(picked)), picked).fetchone()["c"]
        fail = con.execute("SELECT COUNT(*) c FROM photos WHERE downloaded=0 AND record_id IN (%s)"
                           % ",".join("?" * len(picked)), picked).fetchone()["c"]
    finally:
        con.close()

    return {
        "ok": True, "company": company, "company_name": COMPANY_NAME[company],
        "src": src, "records": n, "sampled": len(picked), "checks": nchk,
        "photos_ok": got, "photos_fail": fail, "download_seconds": dt,
        "next": f'{PY} "{os.path.join(D.BASE,"cli.py")}" next --n 25',
    }


def _guess_company(path):
    for code, name in COMPANY_NAME.items():
        if name in os.path.basename(path):
            return code
    return ""


# ============================================================
# next —— 领任务
# ============================================================
def cmd_next(a):
    con = D.connect()
    try:
        w, args = "c.status!='done'", []
        if a.company:
            w += " AND c.company=?"; args.append(a.company.upper())
        if a.month:
            w += " AND c.month=?"; args.append(a.month)
        if a.cust:
            w += " AND r.cust_no=?"; args.append(a.cust)
        rows = con.execute(
            f"""SELECT c.record_id, c.items_code, r.*
                FROM checks c JOIN records r ON r.id=c.record_id
                WHERE {w}
                GROUP BY c.record_id
                ORDER BY c.record_id
                LIMIT ?""", args + [a.n]).fetchall()
        if not rows:
            tot = con.execute("SELECT COUNT(*) c FROM checks").fetchone()["c"]
            done = con.execute("SELECT COUNT(*) c FROM checks WHERE status='done'").fetchone()["c"]
            return {"empty": True, "hint": "没有待判定的户了", "checks_total": tot, "checks_done": done}

        keep = set(a.items.split(",")) if a.items else None
        batch = []
        for r in rows:
            rid = r["record_id"]
            keep_c = keep if keep else set(D.default_items(r["company"]))
            ph = con.execute(
                "SELECT id, items_code, local, downloaded, category FROM photos WHERE record_id=? ORDER BY id",
                (rid,)).fetchall()
            items = []
            for p in ph:
                if p["downloaded"] != 1 or not p["local"]:
                    continue
                code = p["items_code"]
                if not a.all and code not in keep_c:
                    continue
                path = abs_local(p["local"])
                if os.path.exists(path):
                    items.append({"photo_id": p["id"], "items_code": code,
                                  "items_name": D.item_display(code, p["category"], r["company"]),
                                  "path": path.replace("\\", "/")})
            if not items and not a.all:
                # 关键项没图就退回全部
                items = [{"photo_id": p["id"], "items_code": p["items_code"],
                          "items_name": D.item_display(p["items_code"], p["category"], r["company"]),
                          "path": abs_local(p["local"]).replace("\\", "/")}
                         for p in ph if p["downloaded"] == 1 and p["local"]
                         and os.path.exists(abs_local(p["local"]))]
            dup_hints = []
            try:
                for kind, sev, detail, pid in auto_mechanical(con, r, ph):
                    if kind == "同检查项照片连拍重复":
                        dup_hints.append(detail)   # 只作参考，Agent 看了再定
            except Exception:
                pass
            batch.append({
                "record_id": rid,
                "cust_no": r["cust_no"], "cust_name": r["cust_name"],
                "inspector": r["inspector"], "check_time": r["check_time"],
                "company": r["company"],
                "meter_no": r["meter_no"], "meter_type": r["meter_type"],
                "meter_brand": r["meter_brand"], "address": r["address"],
                "community": r["community"], "photo_count": r["photo_count"],
                "dup_hints": dup_hints,       # 疑似连拍，Agent 亲眼确认后再报
                "items": items,
            })
        return {"count": len(batch), "records": batch,
                "submit_help": f'{PY} "{os.path.join(D.BASE,"cli.py")}" submit --id <record_id> --json \'<json>\''}
    finally:
        con.close()


# ============================================================
# submit —— 交结果
# ============================================================
def _apply_one(con, rec_id, payload, run_id=None, do_triage=True):
    """把一户的 Agent 判定写入数据库"""
    rec = con.execute("SELECT * FROM records WHERE id=?", (rec_id,)).fetchone()
    if not rec:
        return {"error": f"record_id={rec_id} 不存在"}
    # 先把档案信息补进 payload，后面的问题描述才能引用
    for k in ("cust_no", "cust_name", "inspector", "check_time", "meter_no", "address"):
        payload.setdefault(k, rec[k])

    probs = []          # (kind, severity, detail, photo_id)
    # 该公司「表具照片」所在 items_code（E 公司 items2 / C 公司 items1 / A 公司 items4）
    MC = D.meter_code(rec["company"])
    # 该户所有照片：items_code → photo_id（Agent 提交时习惯写 items_code，这里自动换算，
    # 否则报告里问题行关联不上照片，格子会是空的）
    pid_by_code = {}
    for p in con.execute("SELECT id, items_code FROM photos WHERE record_id=?",
                         (rec_id,)).fetchall():
        pid_by_code.setdefault(p["items_code"], p["id"])
    # 1) Agent 报的问题
    for it in payload.get("issues", []) or []:
        pid = it.get("photo_id")
        if pid is None:
            pid = pid_by_code.get(it.get("items_code"))
        probs.append((it.get("kind") or "其他",
                      it.get("severity") or "warning",
                      it.get("detail") or "",
                      pid))
    # 2) Agent 明确否定的项 → 自动转问题（防止漏报），并挂上照片，报告里能点开
    for code, val in (payload.get("item_misfiled") or {}).items():
        # 用户 2026-10-15 决定：「热水器项拍的是工作单」不算问题项，
        # 只做提示（info），不需要人工核实。
        sev = "info" if val == "热水器项拍的是工作单" else "warning"
        probs.append((val, sev,
                      f"检查项「{ITEMS_NAME.get(code, code)}」内容不符",
                      pid_by_code.get(code)))
    if payload.get("meter_readable") is False:
        # 2026-10-19 改：表号读不出是「拍照不清晰」，属提示不属严重
        # 旧版这里是 critical —— 反复冤枉安检员的根源，不要再改回去
        probs.append(("表号读不出", "warning",
                      f"登记表号 {payload.get('meter_no') or ''}，照片上未读出",
                      pid_by_code.get(MC)))
    if payload.get("meter_match") is False:
        probs.append(("表号与档案不一致", "critical",
                      f"照片读出 {payload.get('meter_read') or '?'}，档案 {payload.get('meter_no') or '?'}",
                      pid_by_code.get(MC)))
    # 水印日期与工单不符：用户 2026-10-16 定「168 小时一刀切」
    #   gap >= 168h → critical；不足 168h 不标记（先拍照后补工单属正常）
    #   机械兜底按 checks 里的工单时间计算，Agent 无需再给 time_match
    try:
        wt, ot = payload.get("watermark_time"), payload.get("check_time")
        if wt and ot:
            dt_p = datetime.datetime.strptime(wt, "%Y-%m-%d %H:%M:%S")
            dt_o = datetime.datetime.strptime(ot, "%Y-%m-%d %H:%M:%S")
            gap_min = abs((dt_o - dt_p).total_seconds()) / 60
            if gap_min >= TIME_GAP_MIN and not any(
                    p[0] == "水印日期与工单不符" for p in probs):
                probs.append(("水印日期与工单不符", "critical",
                              f"照片水印时间 {wt}，安检工单时间 {ot}，"
                              f"相差约{gap_min/60:.1f}小时（超过7天）",
                              pid_by_code.get(MC)))
    except Exception:
        pass
    if payload.get("form_date_valid") is False:
        probs.append(("手写检查日期异常", "critical",
                      f"手写 {payload.get('form_check_date') or '?'}",
                      pid_by_code.get(D.form_code(rec["company"]))))
    if payload.get("scene_valid") is False:
        probs.append(("照片非安检现场", "critical", "", pid_by_code.get(MC)))
    for h in payload.get("hidden_trouble") or []:
        probs.append((f"重大隐患：{h}", "critical", "", None))

    # 3) 机械相似度自动补（只补 Agent 没报的类型）
    #    注意：Agent 亲眼看过图，若它明确说该项内容正确，就不再机械推翻。
    ph = con.execute("SELECT id, items_code, local, downloaded FROM photos WHERE record_id=?",
                     (rec_id,)).fetchall()
    # Agent 主动声明「这些项我看过，是对的」
    agent_ok = set(payload.get("items_verified_ok") or [])
    have_kinds = {p[0] for p in probs}
    for kind, sev, detail, pid in auto_mechanical(con, rec, ph):
        if kind in have_kinds:
            continue
        ic = _ic(con, pid) if pid else ""
        if ic and ic in agent_ok:
            continue
        probs.append((kind, sev, detail, pid))

    # 4) 写 checks：整户所有检查项标 done，附 result_json
    rj = json.dumps(payload, ensure_ascii=False)
    con.execute("UPDATE checks SET status='done', result_json=?, error=NULL,"
                " updated_at=CURRENT_TIMESTAMP WHERE record_id=?", (rj, rec_id))
    # 5) 重判：旧判定不删，置 active=0 留档（2026-10-19），新判定作为新版本写入
    rev = (con.execute("SELECT COALESCE(MAX(rev),0) r FROM issues WHERE record_id=?",
                       (rec_id,)).fetchone()["r"] or 0) + 1
    con.execute("UPDATE issues SET active=0 WHERE record_id=?", (rec_id,))
    seen = set()
    for kind, sev, detail, pid in probs:
        key = (kind, detail if detail else pid)
        if key in seen:
            continue
        seen.add(key)
        con.execute(
            """INSERT INTO issues(run_id,record_id,company,month,items_code,photo_id,
                                  severity,kind,detail,evidence,active,rev)
               VALUES(?,?,?,?,?,?,?,?,?,?,1,?)""",
            (run_id, rec_id, rec["company"], R.month_of(rec["check_time"]),
             _ic(con, pid) if pid else None, pid, sev, kind, detail,
             json.dumps({k: payload.get(k) for k in
                         ("meter_read", "watermark_date", "form_check_date", "comment")},
                        ensure_ascii=False), rev))
    con.commit()

    # 6) 照片分拣
    moved = {"photos": 0, "pending_delete": 0}
    if do_triage:
        moved = _triage(con, rec, probs)
    return {"ok": True, "record_id": rec_id, "cust_no": rec["cust_no"],
            "issues": len(probs),
            "critical": sum(1 for p in probs if p[1] == "critical"),
            "warning": sum(1 for p in probs if p[1] == "warning"),
            "moved": moved}


def _triage(con, rec, probs):
    """照片分拣：critical/warning → 留存 photos/；只有 info 或无问题 → pending_delete/

    2026-10-15 用户确认：热水器项拍工作单降为 info 后，
    这类户的照片不再算「有问题的证据」，进待删除。
    """
    moved = {"photos": 0, "pending_delete": 0}
    ph = con.execute("SELECT id, local FROM photos WHERE record_id=? AND downloaded=1",
                     (rec["id"],)).fetchall()
    real = [p for p in probs if p[1] in ("critical", "warning")]
    has_problem = bool(real)          # info 不算
    for p in ph:
        if not p["local"]:
            continue
        # 人工复核判「误判」的户：照片在 false_positive/ 永久留档，任何分拣都不动它
        if "false_positive" in p["local"]:
            continue
        src = abs_local(p["local"])
        if not os.path.exists(src):
            continue
        kind = "photos" if has_problem else "pending_delete"
        dst_dir = E.photo_dir(rec["company"], R.month_of(rec["check_time"]), rec["cust_no"], kind)
        dst = os.path.join(dst_dir, os.path.basename(src))
        try:
            if os.path.abspath(src) != os.path.abspath(dst):
                if os.path.exists(dst):
                    os.remove(src)
                else:
                    shutil.move(src, dst)
            rel = os.path.relpath(dst, D.BASE).replace("\\", "/")
            # local 必须同步更新：读图走的是 local，文件已被移走
            con.execute("UPDATE photos SET moved_to=?, local=? WHERE id=?",
                        (rel, rel, p["id"]))
            moved[kind] += 1
        except Exception:
            pass
    con.commit()
    return moved


def cmd_retriage(a):
    """按当前判定口径重跑照片分拣（口径调整后不必重新读图）

    从 issues 表重建每户的问题列表，再走 _triage。
    """
    con = D.connect()
    try:
        w, args = "1=1", []
        if a.company:
            w += " AND company=?"; args.append(a.company)
        if a.month:
            w += " AND month=?"; args.append(a.month)
        rids = [r["record_id"] for r in con.execute(
            f"SELECT DISTINCT record_id FROM v_issue_eff"
            f" WHERE {w} AND eff_severity IS NOT NULL ORDER BY record_id", args)]
        tot = {"photos": 0, "pending_delete": 0}
        for rid in rids:
            rec = con.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
            if not rec:
                continue
            probs = [(r["kind"], r["eff_severity"]) for r in con.execute(
                "SELECT kind, eff_severity FROM v_issue_eff WHERE record_id=?", (rid,))]
            m = _triage(con, rec, probs)
            tot["photos"] += m["photos"]
            tot["pending_delete"] += m["pending_delete"]
        return {"ok": True, "records": len(rids), "moved": tot}
    finally:
        con.close()


def cmd_submit(a):
    payload = json.loads(a.json) if a.json.strip().startswith("{") else json.load(open(a.json, encoding="utf-8"))
    if isinstance(payload, list):
        return cmd_bulk_payload(payload)
    con = D.connect()
    try:
        return _apply_one(con, int(a.id), payload)
    finally:
        con.close()


def cmd_bulk_payload(payloads):
    con = D.connect()
    res = []
    try:
        for p in payloads:
            res.append(_apply_one(con, int(p["record_id"]), p))
    finally:
        con.close()
    ok = [r for r in res if r.get("ok")]
    return {"ok": True, "submitted": len(ok), "failed": len(res) - len(ok),
            "issues": sum(r.get("issues", 0) for r in ok),
            "critical": sum(r.get("critical", 0) for r in ok),
            "detail": res}


def cmd_bulk(a):
    with open(a.file, encoding="utf-8") as f:
        return cmd_bulk_payload(json.load(f))


# ============================================================
# status / finish / reset
# ============================================================
def cmd_status(a):
    con = D.connect()
    try:
        w, args = "1=1", []
        if a.company:
            w += " AND company=?"; args.append(a.company.upper())
        if a.month:
            w += " AND month=?"; args.append(a.month)
        tot = con.execute(f"SELECT COUNT(*) c FROM checks WHERE {w}", args).fetchone()["c"]
        done = con.execute(f"SELECT COUNT(*) c FROM checks WHERE {w} AND status='done'", args).fetchone()["c"]
        recs = con.execute(f"SELECT COUNT(DISTINCT record_id) c FROM checks WHERE {w}", args).fetchone()["c"]
        recs_done = con.execute(
            f"SELECT COUNT(DISTINCT record_id) c FROM checks WHERE {w} AND status='done'", args).fetchone()["c"]
        ph = con.execute("SELECT COUNT(*) c FROM photos WHERE downloaded=1").fetchone()["c"]
        iss = con.execute(
            "SELECT severity, COUNT(*) c FROM issues WHERE active=1 GROUP BY severity").fetchall()
        kinds = con.execute(
            "SELECT kind, COUNT(*) c FROM issues WHERE active=1 GROUP BY kind ORDER BY c DESC LIMIT 15").fetchall()
        rv = con.execute("SELECT verdict, COUNT(*) c FROM reviews GROUP BY verdict").fetchall()
        return {
            "检查项": f"{done}/{tot}", "户": f"{recs_done}/{recs}",
            "待判定户": recs - recs_done, "已下载照片": ph,
            "问题分布": {r["severity"]: r["c"] for r in iss},
            "问题TOP": {r["kind"]: r["c"] for r in kinds},
            "人工复核": {r["verdict"]: r["c"] for r in rv},
        }
    finally:
        con.close()


def cmd_finish(a):
    con = D.connect()
    try:
        m = a.month or datetime.date.today().strftime("%Y-%m")
        c = a.company.upper() if a.company else ""
        iw, iargs = "i.active=1", []
        if c:
            iw += " AND i.company=?"; iargs.append(c)
        iw += " AND i.month=?"; iargs.append(m)
        rw, rargs = "1=1", []
        if c:
            rw += " AND r.company=?"; rargs.append(c)
        rw += " AND substr(r.check_time,1,7)=?"; rargs.append(m)
        by_insp = con.execute(
            f"""SELECT r.inspector, COUNT(DISTINCT r.id) recs,
                       SUM(CASE WHEN i.severity='critical' THEN 1 ELSE 0 END) crit,
                       SUM(CASE WHEN i.severity='warning' THEN 1 ELSE 0 END) warn
                FROM issues i JOIN records r ON r.id=i.record_id
                WHERE {iw} GROUP BY r.inspector ORDER BY crit DESC""", iargs).fetchall()
        by_kind = con.execute(
            f"SELECT i.kind, i.severity, COUNT(*) c FROM issues i WHERE {iw}"
            " GROUP BY i.kind, i.severity ORDER BY c DESC", iargs).fetchall()
        recs = con.execute(f"SELECT COUNT(*) c FROM records r WHERE {rw}", rargs).fetchone()["c"]
        sampled = con.execute(f"SELECT COUNT(*) c FROM records r WHERE {rw} AND r.sampled=1",
                              rargs).fetchone()["c"]
        pending_house = con.execute(
            f"""SELECT COUNT(DISTINCT i.record_id) c FROM issues i WHERE {iw} AND i.severity='critical'""",
            iargs).fetchone()["c"]
        warn_house = con.execute(
            f"""SELECT COUNT(DISTINCT i.record_id) c FROM issues i WHERE {iw} AND i.severity='warning'""",
            iargs).fetchone()["c"]
        rv = con.execute("SELECT verdict, COUNT(*) c FROM reviews GROUP BY verdict").fetchall()
        pd = _pending_dir_stats()
    finally:
        con.close()
    return {
        "月份": m, "公司": COMPANY_NAME.get(c, c or "全部"),
        "入户总户数": recs, "本次抽查户数": sampled,
        "有严重问题户": pending_house, "仅提示户": warn_house,
        "合格户": max(0, sampled - pending_house - warn_house),
        "问题类型": {r["kind"]: r["c"] for r in by_kind},
        "安检员排行": [{"安检员": r["inspector"] or "(未填)", "涉及户": r["recs"],
                       "严重": r["crit"] or 0, "提示": r["warn"] or 0} for r in by_insp],
        "人工复核": {r["verdict"]: r["c"] for r in rv},
        "待删除照片": pd,
        "报告页": "http://127.0.0.1:8790/report",
        "复核页": "http://127.0.0.1:8790/review",
    }


def _pending_dir_stats():
    now = datetime.datetime.now()
    n = b = 0
    for root, _, files in os.walk(D.PENDING_DIR):
        for f in files:
            n += 1
            try:
                st = os.stat(os.path.join(root, f))
                if (now - datetime.datetime.fromtimestamp(st.st_mtime)).days > 30:
                    b += 1
            except Exception:
                pass
    return {"总数": n, "超30天可清理": b}


def cmd_reset(a):
    c = a.company.upper()
    con = D.connect()
    try:
        ids = [r["id"] for r in con.execute("SELECT id FROM records WHERE company=?", (c,))]
        if not ids:
            return {"ok": True, "msg": "无记录"}
        ph = ",".join("?" * len(ids))
        for t in ("checks", "issues", "reviews"):
            con.execute(f"DELETE FROM {t} WHERE record_id IN ({ph})", ids)
        con.execute("UPDATE records SET sampled=0 WHERE company=?", (c,))
        con.commit()
        return {"ok": True, "cleared": len(ids), "company": c}
    finally:
        con.close()


# ============================================================
# 实时进度（供大屏 /live 使用，与 runner.Job 无关）
# ============================================================
def summary_from_db(con, company=None, month=None):
    """从库里实时汇总进度。server 的 /api/state 和 SSE 都调这个，
    这样 Agent 通道（cli.py）跑的时候大屏照样动。"""
    w, args = "1=1", []
    if company:
        w += " AND c.company=?"; args.append(company.upper())
    if month:
        w += " AND c.month=?"; args.append(month)
    total_checks = con.execute(f"SELECT COUNT(*) c FROM checks c WHERE {w}", args).fetchone()["c"]
    done_checks = con.execute(
        f"SELECT COUNT(*) c FROM checks c WHERE {w} AND c.status='done'", args).fetchone()["c"]
    total_records = con.execute(
        f"SELECT COUNT(DISTINCT c.record_id) c FROM checks c WHERE {w}", args).fetchone()["c"]
    done_records = con.execute(
        f"SELECT COUNT(DISTINCT c.record_id) c FROM checks c WHERE {w} AND c.status='done'",
        args).fetchone()["c"]
    # 最近完成的户——大屏上的大字
    cur = con.execute(
        f"""SELECT r.cust_no, r.cust_name, r.inspector FROM checks c
            JOIN records r ON r.id=c.record_id
            WHERE {w} AND c.status='done'
            ORDER BY c.updated_at DESC, c.id DESC LIMIT 1""", args).fetchone()
    # 最近一条问题 / 问题统计 / 问题流：传了公司/月就过滤（多公司避免串台）
    iw, iargs = "i.active=1", []
    if company:
        iw += " AND i.company=?"; iargs.append(company.upper())
    if month:
        iw += " AND i.month=?"; iargs.append(month)
    last_iss = con.execute(
        f"""SELECT i.kind, i.severity, r.cust_no, r.cust_name FROM issues i
           JOIN records r ON r.id=i.record_id
           WHERE {iw} ORDER BY i.id DESC LIMIT 1""", iargs).fetchone()
    sev = {r["severity"]: r["c"] for r in con.execute(
        f"SELECT i.severity, COUNT(*) c FROM issues i WHERE {iw} GROUP BY i.severity", iargs)}
    recent = con.execute(
        f"""SELECT r.cust_no, r.cust_name, r.inspector, i.severity, i.kind, i.detail
           FROM issues i JOIN records r ON r.id=i.record_id
           WHERE {iw} ORDER BY i.id DESC LIMIT 12""", iargs).fetchall()
    pw, pargs = "1=1", []
    if company:
        pw += " AND company=?"; pargs.append(company.upper())
    if month:
        pw += " AND month=?"; pargs.append(month)
    ph_total = con.execute(f"SELECT COUNT(*) c FROM photos WHERE {pw}", pargs).fetchone()["c"]
    ph_ok = con.execute(f"SELECT COUNT(*) c FROM photos WHERE {pw} AND downloaded=1", pargs).fetchone()["c"]
    return {
        "phase": "check" if done_checks < total_checks else "idle",
        "done_checks": done_checks, "total_checks": total_checks,
        "done_records": done_records, "total_records": total_records,
        "issues": sum(sev.values()), "critical": sev.get("critical", 0),
        "warning": sev.get("warning", 0), "info": sev.get("info", 0),
        "current_cust": cur["cust_no"] if cur else "",
        "current_name": cur["cust_name"] if cur else "",
        "current_inspector": cur["inspector"] if cur else "",
        "last_issue": (dict(last_iss) if last_iss else {}),
        "recent_issues": [dict(r) for r in recent],
        "photos_total": ph_total, "photos_ok": ph_ok,
        "percent": round(done_checks * 100.0 / total_checks, 1) if total_checks else 0,
    }


def cmd_watch(a):
    """命令行看进度"""
    con = D.connect()
    try:
        return summary_from_db(con, a.company, a.month)
    finally:
        con.close()


def cmd_report(a):
    """生成自包含 HTML 报告
    2026-10-19 起统一走 report_render：与报告页「下载」生成的完全同一份模板，
    不再维护第二套旧模板（两套模板会给出不一致的数字和排版）。
    """
    import report_render as RR
    import report_data as RD
    con = D.connect()
    try:
        code = (a.company or "").upper()
        if not code:
            return {"error": "需要指定公司（--company C1/C4/C5...）"}
        stats = RD.company_stats(con, a.month, code)
        if not stats:
            return {"error": f"未知公司 {code}"}
        d = stats[0]
        embed = bool(getattr(a, "embed", True))
        html = RR.render_company(con, code, month=a.month, embed=embed)
        stamp = datetime.date.today().strftime("%Y%m%d")
        ml = (a.month.replace("-", "年") + "月") if a.month else "全部月份"
        suffix = "_离线版" if embed else ""
        os.makedirs(D.REPORTS_DIR, exist_ok=True)
        fn = os.path.join(D.REPORTS_DIR,
                          f"安检抽查报告_{d['name']}_{ml}_{stamp}{suffix}.html")
        with open(fn, "w", encoding="utf-8") as f:
            f.write(html)
        return {"ok": True, "file": fn, "company": code, "name": d["name"],
                "rows": d["problem_house"], "critical": d["critical_house"],
                "warning": d["warning_house"], "sampled": d["sampled"]}
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser(description="安检抽查 Agent 通道")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("prepare", help="导入+抽样+下载")
    p.add_argument("--file", required=True)
    p.add_argument("--company", default="")
    p.add_argument("--month", default="")
    p.add_argument("--ratio", type=float, default=0)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--workers", type=int, default=0)
    p.add_argument("--fresh", action="store_true", help="先清空该公司历史检测结果")
    p.set_defaults(fn=cmd_prepare)

    p = sub.add_parser("next", help="领一批待判定户")
    p.add_argument("--n", type=int, default=25)
    p.add_argument("--company", default="")
    p.add_argument("--month", default="")
    p.add_argument("--cust", default="")
    p.add_argument("--items", default="", help="逗号分隔的检查项，默认关键四项")
    p.add_argument("--all", action="store_true", help="返回全部检查项照片")
    p.set_defaults(fn=cmd_next)

    p = sub.add_parser("submit", help="提交一户判定")
    p.add_argument("--id", required=True)
    p.add_argument("--json", required=True)
    p.set_defaults(fn=cmd_submit)

    p = sub.add_parser("bulk", help="批量提交")
    p.add_argument("--file", required=True)
    p.set_defaults(fn=cmd_bulk)

    p = sub.add_parser("status", help="进度")
    p.add_argument("--company", default="")
    p.add_argument("--month", default="")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("finish", help="收尾统计")
    p.add_argument("--company", default="")
    p.add_argument("--month", default="")
    p.set_defaults(fn=cmd_finish)

    p = sub.add_parser("reset", help="清空某公司检测结果")
    p.add_argument("--company", required=True)
    p.set_defaults(fn=cmd_reset)

    p = sub.add_parser("watch", help="实时进度（大屏同源）")
    p.add_argument("--company", default="")
    p.add_argument("--month", default="")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("report", help="生成 HTML 报告（可直接发领导）")
    p.add_argument("--company", default="")
    p.add_argument("--month", default="")
    p.add_argument("--embed", action="store_true", help="照片内嵌，生成单文件可外发版")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("retriage", help="按当前口径重跑照片分拣（不重新读图）")
    p.add_argument("--company", default="")
    p.add_argument("--month", default="")
    p.set_defaults(fn=cmd_retriage)

    a = ap.parse_args()
    if not a.cmd:
        ap.print_help()
        return
    if a.cmd == "prepare" and not a.month:
        a.month = R.month_of(_first_check_time(a.file)) or datetime.date.today().strftime("%Y-%m")
    out(a.fn(a))


def _first_check_time(src):
    try:
        from openpyxl import load_workbook
        wb = load_workbook(src, read_only=True, data_only=True)
        ws = wb.worksheets[0]
        it = ws.iter_rows(values_only=True)
        hdr = [str(h).strip() if h else "" for h in next(it)]
        i = hdr.index("安检时间")
        si = hdr.index("安检状态")
        for r in it:
            if r and r[si] and str(r[si]).strip() == "入户" and r[i]:
                v = str(r[i])
                wb.close()
                return v
        wb.close()
    except Exception:
        pass
    return ""


if __name__ == "__main__":
    main()