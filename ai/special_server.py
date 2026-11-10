# -*- coding: utf-8 -*-
"""专项任务子系统（完全隔离）

设计原则（2026-11-09 用户定）：
  * 主库 ai/db.sqlite3 **只读**，绝不写入。
  * 专项任务各自独立：special/<code>/special.db + photos/。
  * 不复用 cli.py / scripts/batch.py（它们会写主库）。
  * 全量口径：名单内所有可查户全部纳入，不做 10% 抽样。
  * 报告页 = 汇总 + 问题清单 + 人工复核工作台（同页）。

挂载方式：server.py do_GET/do_POST 里
    if path.startswith("/special"): return special_server.handle(self, path, q)
只此一处，主程序其余逻辑不变。
"""
import os, sys, json, sqlite3, datetime, urllib.parse, mimetypes, threading

BASE = os.path.dirname(os.path.abspath(__file__))
SPECIAL_DIR = os.path.join(BASE, "special")
DB_PATH = os.path.join(SPECIAL_DIR, "special.db")
STATIC = os.path.join(BASE, "static")
MAIN_DB = os.path.join(BASE, "db.sqlite3")

# 问题性质分层（用于报告分层展示，不改判定档位）
NATURE = {
    "未拍摄燃气表": ("没有拍摄到燃气表",),
    "档案类": ("档案",),
    "拍照质量": ("表号无法辨认", "照片模糊", "无法辨认"),
    "现场真实性": ("水印", "非燃气安检现场", "未入户", "重复照片"),
}


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today():
    return datetime.date.today().strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 数据库
def conn(readonly=False):
    os.makedirs(SPECIAL_DIR, exist_ok=True)
    if readonly:
        c = sqlite3.connect("file:%s?mode=ro" % DB_PATH.replace("\\", "/"), uri=True)
    else:
        c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def main_conn():
    """主库：只读打开，物理上不可能写坏"""
    c = sqlite3.connect("file:%s?mode=ro" % MAIN_DB.replace("\\", "/"), uri=True, timeout=20)
    c.row_factory = sqlite3.Row
    return c


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT UNIQUE,
  name TEXT,
  company TEXT, company_name TEXT,
  month TEXT, mfrom TEXT, mto TEXT,
  inspectors TEXT,            -- JSON 数组
  requirement TEXT,           -- 用户给的需求原文
  note TEXT,
  status TEXT DEFAULT 'active',   -- active / judging / done
  created_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS recs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER, rid INTEGER,       -- rid = 主库 records.id
  cust_no TEXT, cust_name TEXT, inspector TEXT,
  community TEXT, address TEXT, check_time TEXT,
  meter_no TEXT, gas_nature TEXT, hazard_raw TEXT,
  photo_count INTEGER, origin TEXT,   -- monthly=月度已判 / new=本次新判
  sampled_month INTEGER DEFAULT 0,
  UNIQUE(task_id, rid)
);
CREATE TABLE IF NOT EXISTS rec_photos(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER, rid INTEGER,
  items_code TEXT, category TEXT, seq INTEGER,
  local TEXT, url TEXT, is_meter INTEGER,
  downloaded INTEGER DEFAULT 0,
  ext_only INTEGER DEFAULT 0,    -- 1=辅助照片，只存 URL 供人看，不参与判定
  empty INTEGER DEFAULT 0        -- 1=Excel 该列为空（前端显示「未拍摄」）
);
CREATE TABLE IF NOT EXISTS judg(
  task_id INTEGER, rid INTEGER,
  scene_valid INTEGER, meter_visible INTEGER,
  meter_readable INTEGER, meter_match INTEGER,
  meter_read TEXT, watermark_time TEXT, comment TEXT,
  issues TEXT,                -- JSON: [{kind,severity,detail,items_code}]
  verdict TEXT,               -- critical / warning / none
  source TEXT,                -- monthly / new
  done_at TEXT,
  PRIMARY KEY(task_id, rid)
);
CREATE TABLE IF NOT EXISTS reviews(
  task_id INTEGER, rid INTEGER,
  verdict TEXT,               -- false_positive / warning / critical
  note TEXT, tags TEXT,
  note_auto TEXT DEFAULT '', note_manual TEXT DEFAULT '',
  reviewer TEXT, updated_at TEXT,
  PRIMARY KEY(task_id, rid)
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS ix_recs ON recs(task_id, inspector);
CREATE INDEX IF NOT EXISTS ix_ph ON rec_photos(task_id, rid);
"""


def init_db():
    c = conn()
    c.executescript(SCHEMA)
    cols = [r[1] for r in c.execute("PRAGMA table_info(reviews)")]
    for col, ddl in (("note_auto", "ALTER TABLE reviews ADD COLUMN note_auto TEXT DEFAULT ''"),
                     ("note_manual", "ALTER TABLE reviews ADD COLUMN note_manual TEXT DEFAULT ''")):
        if col not in cols:
            c.execute(ddl)
    # rec_photos 的两个辅助标记（老库由 import_extra_photos.py 补过，新库直接在 SCHEMA 里）
    pcols = [r[1] for r in c.execute("PRAGMA table_info(rec_photos)")]
    for col, ddl in (("ext_only", "ALTER TABLE rec_photos ADD COLUMN ext_only INTEGER DEFAULT 0"),
                     ("empty", "ALTER TABLE rec_photos ADD COLUMN empty INTEGER DEFAULT 0")):
        if col not in pcols:
            c.execute(ddl)
    c.commit()
    c.close()


# ---------------------------------------------------------------- 业务：任务
def task_dir(code):
    d = os.path.join(SPECIAL_DIR, code)
    os.makedirs(os.path.join(d, "photos"), exist_ok=True)
    return d


def list_tasks():
    c = conn(readonly=True) if os.path.exists(DB_PATH) else init_and_conn()
    try:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM tasks ORDER BY id DESC").fetchall()]
    finally:
        c.close()
    for t in rows:
        t["inspectors"] = json.loads(t.get("inspectors") or "[]")
        t.update(_task_progress(t["id"]))
    return rows


_inited = False


def _ensure_db():
    """首次任何请求前把库建好/补齐（挂到 handle 入口，避免只有建任务才初始化）"""
    global _inited
    if not _inited:
        init_db()
        _inited = True


def init_and_conn():
    init_db()
    return conn(readonly=True)


def _task_progress(tid):
    c = conn(readonly=True)
    try:
        total = c.execute("SELECT COUNT(*) FROM recs WHERE task_id=?", (tid,)).fetchone()[0]
        judged = c.execute("SELECT COUNT(*) FROM judg WHERE task_id=?", (tid,)).fetchone()[0]
        reviewed = c.execute("SELECT COUNT(*) FROM reviews WHERE task_id=?", (tid,)).fetchone()[0]
        photos = c.execute("SELECT COUNT(*), SUM(downloaded) FROM rec_photos WHERE task_id=?",
                           (tid,)).fetchone()
    finally:
        c.close()
    return {"total": total, "judged": judged, "reviewed": reviewed,
            "photos": photos[0] or 0, "photos_ok": photos[1] or 0}


def get_task(tid):
    c = conn(readonly=True)
    try:
        r = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    finally:
        c.close()
    if not r:
        return None
    t = dict(r)
    t["inspectors"] = json.loads(t.get("inspectors") or "[]")
    t.update(_task_progress(tid))
    return t


def _derive_verdict(issues):
    sev = [i.get("severity") for i in issues or []]
    if "critical" in sev:
        return "critical"
    if "warning" in sev:
        return "warning"
    return "none"


def create_task(payload):
    """建任务 + 复制元数据（主库只读）。payload:
       {company, month, inspectors:[...], requirement, name?}
    """
    company = (payload.get("company") or "").strip().upper()
    month = (payload.get("month") or "").strip()
    insp = [s.strip() for s in (payload.get("inspectors") or []) if s and s.strip()]
    if not company or not month or not insp:
        raise ValueError("company / month / inspectors 三项必填")

    import db as D
    cname = None
    for code, nm in D.COMPANY_ORDER:
        if code == company:
            cname = nm
    if not cname:
        raise ValueError("未知公司代码：%s" % company)

    mfrom, mto = month, month
    if "-" in month and len(month) == 7:
        pass
    else:
        raise ValueError("month 需为 YYYY-MM")

    code = "S%03d_%s_%s_%d人" % (_next_seq(), company, month, len(insp))
    tdir = task_dir(code)

    init_db()
    c = conn()
    try:
        cur = c.execute(
            """INSERT INTO tasks(code,name,company,company_name,month,mfrom,mto,
                 inspectors,requirement,note,status,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (code, payload.get("name") or "%s %s 专项 %d 人" % (cname, month, len(insp)),
             company, cname, month, mfrom, mto,
             json.dumps(insp, ensure_ascii=False),
             payload.get("requirement") or "", payload.get("note") or "",
             "active", now()))
        tid = cur.lastrowid
        c.commit()
    finally:
        c.close()

    stat = _import_records(tid, company, month, insp)
    if not stat.get("records"):
        # 一个人都没匹配到 → 回滚，别留空任务
        import shutil
        c = conn()
        try:
            c.execute("DELETE FROM recs WHERE task_id=?", (tid,))
            c.execute("DELETE FROM rec_photos WHERE task_id=?", (tid,))
            c.execute("DELETE FROM tasks WHERE id=?", (tid,))
            c.commit()
        finally:
            c.close()
        shutil.rmtree(os.path.join(SPECIAL_DIR, code), ignore_errors=True)
        raise ValueError("名单里没有匹配到任何户（姓名写法与 records.inspector 不一致，"
                         "先用 /api/special/inspectors 核对）")
    stat["task_id"] = tid
    stat["code"] = code
    return stat


def _next_seq():
    try:
        c = conn(readonly=True)
        n = c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        c.close()
        return n + 1
    except Exception:
        return 1


def _import_records(tid, company, month, inspectors):
    """从主库只读复制：records + photos + 已有判定(checks/issues/reviews)

    抽样池口径与主库一致（db.checkable_cond）：民用 + 有表具照片 + 未 skip。
    """
    import db as D
    mc = main_conn()
    cond, args = D.checkable_cond(company, "r")
    ph = ",".join("?" * len(inspectors))
    sql = f"""SELECT r.* FROM records r
              WHERE r.company=? AND substr(r.check_time,1,7)=?
                AND r.inspector IN ({ph}) AND {cond}
              ORDER BY r.inspector, r.cust_no"""
    rows = mc.execute(sql, [company, month] + inspectors + list(args)).fetchall()

    sc = conn()
    n_rec = n_ph = n_old = 0
    try:
        for r in rows:
            sampled = 1 if r["sampled"] else 0
            # 主库该户是否已有 done 判定
            old = mc.execute(
                """SELECT COUNT(*) FROM checks WHERE company=? AND month=?
                    AND record_id=? AND status='done'""",
                (company, month, r["id"])).fetchone()[0]
            origin = "monthly" if (sampled or old) else "new"
            sc.execute(
                """INSERT OR IGNORE INTO recs(task_id,rid,cust_no,cust_name,inspector,
                     community,address,check_time,meter_no,gas_nature,hazard_raw,
                     photo_count,origin,sampled_month)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tid, r["id"], r["cust_no"], r["cust_name"], r["inspector"],
                 r["community"], r["address"], r["check_time"], r["meter_no"],
                 r["gas_nature"], r["hazard_raw"], r["photo_count"], origin, sampled))
            n_rec += 1
            if origin == "monthly":
                n_old += 1

            for p in mc.execute(
                    """SELECT items_code,category,seq,local,url,is_meter_photo
                         FROM photos WHERE record_id=? ORDER BY seq""", (r["id"],)).fetchall():
                sc.execute(
                    """INSERT INTO rec_photos(task_id,rid,items_code,category,seq,local,url,
                         is_meter,downloaded) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (tid, r["id"], p["items_code"], p["category"], p["seq"],
                     p["local"], p["url"], p["is_meter_photo"],
                     1 if p["local"] else 0))
                n_ph += 1

            if origin == "monthly":
                _copy_judgment(sc, mc, tid, company, month, r["id"])
        sc.commit()
    finally:
        sc.close()
        mc.close()
    return {"records": n_rec, "photos": n_ph, "monthly_judged": n_old}


def _copy_judgment(sc, mc, tid, company, month, rid):
    """沿用主库已有判定（口径：severity 最高档）"""
    iss = mc.execute(
        """SELECT severity,kind,detail,items_code FROM v_issue_eff
            WHERE record_id=? AND active=1""", (rid,)).fetchall()
    issues = [{"kind": i["kind"], "severity": i["severity"],
               "detail": i["detail"] or "", "items_code": i["items_code"] or ""}
              for i in iss]
    rv = mc.execute("SELECT verdict,note FROM reviews WHERE record_id=? AND month=?",
                    (rid, month)).fetchone()
    rv_v, rv_note = (rv["verdict"], rv["note"]) if rv else (None, None)
    verdict = _derive_verdict(issues)
    if rv_v in ("critical", "warning", "false_positive"):
        verdict = "none" if rv_v == "false_positive" else rv_v
    sc.execute(
        """INSERT OR REPLACE INTO judg(task_id,rid,issues,verdict,source,done_at,comment)
           VALUES(?,?,?,?,?,?,?)""",
        (tid, rid, json.dumps(issues, ensure_ascii=False), verdict, "monthly", now(),
         rv_note or "沿用 %s 月度判定" % month))
    if rv_v:
        sc.execute(
            """INSERT OR REPLACE INTO reviews(task_id,rid,verdict,note,tags,reviewer,updated_at)
               VALUES(?,?,?,?,?,?,?)""",
            (tid, rid, rv_v, rv_note or "", json.dumps([], ensure_ascii=False),
             "月度复核", now()))


# ---------------------------------------------------------------- 业务：统计
def _nature_of(kind):
    for nat, keys in NATURE.items():
        for k in keys:
            if k in (kind or ""):
                return nat
    return "其他"


def month_baseline(company, month):
    """月度报告口径：该公司该月各安检员的抽样户数 / 合格户数（v_issue_eff 有效档位）
    返回 {inspector: {"sample":n, "ok":n, "crit":n, "warn":n}}"""
    out = {}
    dbp = os.path.join(BASE, "db.sqlite3")
    if not os.path.exists(dbp):
        return out
    try:
        mc = sqlite3.connect("file:%s?mode=ro" % dbp.replace("\\", "/"), uri=True)
        mc.row_factory = sqlite3.Row
        rows = mc.execute(
            """SELECT r.inspector AS insp,
                      COUNT(DISTINCT r.id) AS sample,
                      SUM(CASE WHEN EXISTS(SELECT 1 FROM v_issue_eff i
                            WHERE i.record_id=r.id AND i.eff_severity='critical')
                          THEN 1 ELSE 0 END) AS crit,
                      SUM(CASE WHEN EXISTS(SELECT 1 FROM v_issue_eff i
                            WHERE i.record_id=r.id AND i.eff_severity='warning')
                          AND NOT EXISTS(SELECT 1 FROM v_issue_eff i2
                            WHERE i2.record_id=r.id AND i2.eff_severity='critical')
                          THEN 1 ELSE 0 END) AS warn
               FROM records r
              WHERE r.company=? AND substr(r.check_time,1,7)=? AND r.sampled=1
              GROUP BY r.inspector""", (company, month)).fetchall()
        for r in rows:
            n = r["sample"] or 0
            cr = r["crit"] or 0
            wn = r["warn"] or 0
            out[r["insp"]] = {"sample": n, "crit": cr, "warn": wn,
                              "ok": max(0, n - cr - wn)}
        mc.close()
    except Exception as e:
        print("month_baseline:", e)
    return out


def summary(tid):
    """两口径并列：全景（全部纳入户） / 原口径（月度已判户）"""
    t = get_task(tid)
    if not t:
        return None
    c = conn(readonly=True)
    try:
        recs = [dict(r) for r in c.execute(
            "SELECT rid,inspector,origin FROM recs WHERE task_id=?", (tid,))]
        jm = {r["rid"]: dict(r) for r in c.execute(
            "SELECT * FROM judg WHERE task_id=?", (tid,))}
        rm = {r["rid"]: dict(r) for r in c.execute(
            "SELECT * FROM reviews WHERE task_id=?", (tid,))}
    finally:
        c.close()

    def house_state(rid):
        """返回 (档位, 是否剔除, 问题性质集合)"""
        j = jm.get(rid)
        rv = rm.get(rid)
        issues = json.loads(j["issues"]) if j and j.get("issues") else []
        if rv and rv.get("verdict") == "false_positive":
            return "none", True, set()
        if rv and rv.get("verdict") in ("critical", "warning"):
            nats = {_nature_of(i.get("kind")) for i in issues}
            return rv["verdict"], False, nats
        v = j.get("verdict") if j else None
        nats = {_nature_of(i.get("kind")) for i in issues}
        return (v or "pending"), False, nats

    per = {}
    for r in recs:
        a = per.setdefault(r["inspector"], dict(
            name=r["inspector"], total=0, judged=0, reviewed=0,
            crit=0, warn=0, pass_=0, pending=0, excluded=0,
            m_total=0, m_crit=0, m_warn=0, m_pass=0,
            arch=0, photo_q=0, scene=0, nometer=0, other=0))
        st, excl, nats = house_state(r["rid"])
        is_m = r["origin"] == "monthly"
        a["total"] += 1
        if r["rid"] in jm:
            a["judged"] += 1
        if r["rid"] in rm:
            a["reviewed"] += 1
        if st == "pending":
            a["pending"] += 1
        elif st == "critical":
            a["crit"] += 1
        elif st == "warning":
            a["warn"] += 1
        else:
            a["pass_"] += 1
        if excl:
            a["excluded"] += 1
        for n in nats:
            if n == "未拍摄燃气表":
                a["nometer"] += 1
            elif n == "档案类":
                a["arch"] += 1
            elif n == "拍照质量":
                a["photo_q"] += 1
            elif n == "现场真实性":
                a["scene"] += 1
            elif n != "其他":
                a["other"] += 1
        if is_m:
            a["m_total"] += 1
            if st == "critical":
                a["m_crit"] += 1
            elif st == "warning":
                a["m_warn"] += 1
            elif st == "none":
                a["m_pass"] += 1

    base = month_baseline(t["company"], t["month"])
    out = []
    for a in per.values():
        a["denom"] = a["total"] - a["pending"]
        a["rate"] = round(a["pass_"] * 100.0 / a["denom"], 1) if a["denom"] else 0.0
        a["rate_m"] = round(a["m_pass"] * 100.0 / a["m_total"], 1) if a["m_total"] else None
        a["bad"] = a["crit"] + a["warn"]
        # 三个占比（越低越好）：严重 / 一般 / 问题
        a["p_crit"] = round(a["crit"] * 100.0 / a["denom"], 1) if a["denom"] else 0.0
        a["p_warn"] = round(a["warn"] * 100.0 / a["denom"], 1) if a["denom"] else 0.0
        a["p_issue"] = round(a["bad"] * 100.0 / a["denom"], 1) if a["denom"] else 0.0
        # 月度基线（整公司该月，按同名人匹配）
        b = base.get(a["name"]) or {}
        a["ms_sample"] = b.get("sample", 0)
        a["ms_ok"] = b.get("ok", 0)
        a["ms_rate"] = round(b["ok"] * 100.0 / b["sample"], 1) if b.get("sample") else None
        out.append(a)
    out.sort(key=lambda x: (-x["total"], x["name"]))

    tot = {k: sum(a[k] for a in out) for k in
           ("total", "judged", "reviewed", "crit", "warn", "pass_", "pending",
            "excluded", "m_total", "m_crit", "m_warn", "m_pass",
            "arch", "photo_q", "scene", "nometer")}
    tot["denom"] = tot["total"] - tot["pending"]
    tot["rate"] = round(tot["pass_"] * 100.0 / tot["denom"], 1) if tot["denom"] else 0.0
    tot["rate_m"] = round(tot["m_pass"] * 100.0 / tot["m_total"], 1) if tot["m_total"] else None
    tot["p_crit"] = round(tot["crit"] * 100.0 / tot["denom"], 1) if tot["denom"] else 0.0
    tot["p_warn"] = round(tot["warn"] * 100.0 / tot["denom"], 1) if tot["denom"] else 0.0
    tot["p_issue"] = round((tot["crit"] + tot["warn"]) * 100.0 / tot["denom"], 1) if tot["denom"] else 0.0
    tot["ms_sample"] = sum(a["ms_sample"] for a in out)
    tot["ms_ok"] = sum(a["ms_ok"] for a in out)
    tot["ms_rate"] = round(tot["ms_ok"] * 100.0 / tot["ms_sample"], 1) if tot["ms_sample"] else None

    return {"task": t, "inspectors": out, "totals": tot,
            "progress": {"total": t["total"], "judged": t["judged"],
                         "reviewed": t["reviewed"]}}


def recs_list(tid, filt="all", inspector=None, limit=5000, offset=0):
    import db as D
    _t = get_task(tid) or {}
    tco = _t.get("company", "")
    tcode = _t.get("code", "")
    c = conn(readonly=True)
    try:
        w = ["r.task_id=?"]
        a = [tid]
        if inspector:
            w.append("r.inspector=?"); a.append(inspector)
        sql = ("SELECT r.*, j.verdict jv, j.issues, j.comment jc, j.source jsrc, "
               "v.verdict rvv, v.note rvn, v.tags rvt, v.reviewer rvrw "
               "FROM recs r LEFT JOIN judg j ON j.task_id=r.task_id AND j.rid=r.rid "
               "LEFT JOIN reviews v ON v.task_id=r.task_id AND v.rid=r.rid "
               "WHERE " + " AND ".join(w) + " ORDER BY r.inspector, r.cust_no")
        rows = c.execute(sql, a).fetchall()
        # 第一次人工复核（月度，主库 reviews）批量取
        mrev = {}
        try:
            dbp = os.path.join(BASE, "db.sqlite3")
            if os.path.exists(dbp) and rows:
                ids = [str(r["rid"]) for r in rows]
                mc = sqlite3.connect("file:%s?mode=ro" % dbp.replace("\\", "/"), uri=True)
                mc.row_factory = sqlite3.Row
                for x in mc.execute(
                        "SELECT record_id,verdict,reviewer,note FROM reviews "
                        "WHERE record_id IN (%s) AND COALESCE(verdict,'')<>''"
                        % ",".join(ids)):
                    mrev[x["record_id"]] = dict(x)
                mc.close()
        except Exception as e:
            print("recs_list mrev:", e)
        out = []
        for r in rows:
            issues = json.loads(r["issues"]) if r["issues"] else []
            rv = r["rvv"]
            if rv == "false_positive":
                eff = "none"
            elif rv in ("critical", "warning"):
                eff = rv
            else:
                eff = r["jv"] or "pending"
            if r["rid"] not in (None,):
                pass

            if filt == "issue" and eff not in ("critical", "warning"):
                continue
            if filt == "undone" and r["rvv"] and (r["rvrw"] or "") != "月度复核":
                continue
            if filt == "pending" and eff != "pending":
                continue
            photos = [dict(p) for p in c.execute(
                "SELECT items_code,category,seq,local,url,is_meter,ext_only,empty "
                "FROM rec_photos WHERE task_id=? AND rid=? ORDER BY seq", (tid, r["rid"]))]
            for p_ in photos:
                p_["name"] = D.item_display(p_["items_code"], p_["category"], tco)
                p_["is_meter"] = 1 if p_["items_code"] == D.meter_code(tco) else 0
                _loc = (p_["local"] or "").replace("\\", "/")
                p_["path"] = ("special/photo?t=%s&f=%s" % (
                    urllib.parse.quote(tcode), urllib.parse.quote(_loc))) if _loc else (p_["url"] or "")
            _mc = D.meter_code(tco)
            photos.sort(key=lambda x: (0 if x["items_code"] == _mc else 1))
            out.append({
                "rid": r["rid"], "cust_no": r["cust_no"], "cust_name": r["cust_name"],
                "inspector": r["inspector"], "community": r["community"],
                "address": r["address"], "check_time": r["check_time"],
                "meter_no": r["meter_no"], "gas_nature": r["gas_nature"],
                "origin": r["origin"], "photo_count": r["photo_count"],
                "photos": photos,
                "eff": eff, "issues": issues, "comment": r["jc"] or "",
                "src": r["jsrc"] or "", "review": rv,
                "ai_verdict": r["jv"] or "",
                "rv1": (mrev.get(r["rid"]) or {}).get("verdict") or "",
                "rv1_note": (mrev.get(r["rid"]) or {}).get("note") or "",
                "note": r["rvn"] or "", "tags": json.loads(r["rvt"] or "[]"),
                "reviewer": r["rvrw"] or "",
            })
        return out[offset:offset + limit]
    finally:
        c.close()


def save_review(tid, rid, verdict, note="", tags=None, reviewer="人工",
                note_auto="", note_manual=""):
    c = conn()
    try:
        c.execute(
            """INSERT OR REPLACE INTO reviews(task_id,rid,verdict,note,tags,
                 note_auto,note_manual,reviewer,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (tid, rid, verdict, note or "", json.dumps(tags or [], ensure_ascii=False),
             note_auto or "", note_manual or "", reviewer, now()))
        c.commit()
    finally:
        c.close()




def house_detail(tid, rid):
    """单户详情：全部照片 + AI 判定 + 复核 + 月度原判对比"""
    import db as D
    t = get_task(tid)
    if not t:
        return None
    c = conn(readonly=True)
    try:
        r = c.execute("SELECT * FROM recs WHERE task_id=? AND rid=?", (tid, rid)).fetchone()
        if not r:
            return None
        r = dict(r)
        ph = [dict(x) for x in c.execute(
            "SELECT * FROM rec_photos WHERE task_id=? AND rid=? ORDER BY seq", (tid, rid))]
        j0 = c.execute("SELECT * FROM judg WHERE task_id=? AND rid=?", (tid, rid)).fetchone()
        v0 = c.execute("SELECT * FROM reviews WHERE task_id=? AND rid=?", (tid, rid)).fetchone()
        j = dict(j0) if j0 else None
        v = dict(v0) if v0 else None
    finally:
        c.close()

    mcode = D.meter_code(t["company"])
    fcode = D.form_code(t["company"]) if hasattr(D, "form_code") else "items1"
    photos = []
    for p_ in ph:
        local = (p_["local"] or "").replace("\\", "/")
        photos.append({
            "items_code": p_["items_code"],
            "name": D.item_display(p_["items_code"], p_["category"], t["company"]),
            "is_meter": 1 if p_["items_code"] == mcode else 0,
            "url": p_["url"] or "",
            "path": ("special/photo?t=%s&f=%s" % (
                urllib.parse.quote(t["code"]), urllib.parse.quote(local))) if local else (p_["url"] or ""),
            "ext_only": p_["ext_only"] or 0,
            "empty": p_["empty"] or 0,
        })
    photos.sort(key=lambda x: (0 if x["items_code"] == mcode else
                               1 if x["items_code"] == fcode else 2))

    issues = json.loads(j["issues"]) if j and j.get("issues") else []
    eff = None
    if v and v["verdict"] == "false_positive":
        eff = "none"
    elif v and v["verdict"] in ("critical", "warning"):
        eff = v["verdict"]
    elif j:
        eff = j["verdict"]
    for i in issues:
        i["name"] = D.item_display(i.get("items_code"), None, t["company"])

    # 月度原判（主库只读）
    monthly = None
    try:
        mc = main_conn()
        try:
            row = mc.execute(
                """SELECT MAX(CASE WHEN eff_severity='critical' THEN 1 ELSE 0 END) crit,
                          MAX(CASE WHEN eff_severity='warning' THEN 1 ELSE 0 END) warn,
                          COUNT(*) n FROM v_issue_eff WHERE record_id=?""", (rid,)).fetchone()
            rv = mc.execute("SELECT verdict,note FROM reviews WHERE record_id=?", (rid,)).fetchone()
            monthly = {"crit": row["crit"] or 0, "warn": row["warn"] or 0,
                       "issues": row["n"] or 0,
                       "verdict": rv["verdict"] if rv else None,
                       "note": (rv["note"] if rv else "") or ""}
        finally:
            mc.close()
    except Exception:
        monthly = None

    return {
        "task": {"id": t["id"], "code": t["code"], "name": t["name"],
                 "company": t["company"], "company_name": t["company_name"],
                 "month": t["month"]},
        "rec": r, "photos": photos, "issues": issues,
        "ph_items": len({x["items_code"] for x in photos}),
        "ph_shots": sum(1 for x in photos if not x["empty"]),
        "ph_empty": sum(1 for x in photos if x["empty"]),
        "judg": ({"verdict": j["verdict"], "comment": j["comment"] or "",
                  "source": j["source"] or "", "done_at": j["done_at"] or ""} if j else None),
        "review": ({"verdict": v["verdict"], "note": v["note"] or "",
                    "reviewer": v["reviewer"] or "",
                    "updated_at": v["updated_at"] or ""} if v else None),
        "eff": eff, "monthly": monthly,
    }


def review_queue(tid, pending_only=False):
    """人工复核队列（结构与月度 /api/queue 同构，前端 review.html 直接复用）

    只放「已判定且有效档位为 critical/warning」的户——没判定的户没有可复核内容。
    reviewer='月度复核' 的继承记录视为「待专项确认」，仍进队列。
    """
    import db as D
    t = get_task(tid)
    if not t:
        return {"rows": [], "pending": 0, "done": 0}
    code = t["code"]
    c = conn(readonly=True)
    try:
        jm = {r["rid"]: dict(r) for r in c.execute(
            "SELECT * FROM judg WHERE task_id=?", (tid,))}
        rm = {r["rid"]: dict(r) for r in c.execute(
            "SELECT * FROM reviews WHERE task_id=?", (tid,))}
        recs = [dict(r) for r in c.execute(
            "SELECT * FROM recs WHERE task_id=?", (tid,))]
        ph = {}
        for r in c.execute("SELECT * FROM rec_photos WHERE task_id=? ORDER BY seq", (tid,)):
            ph.setdefault(r["rid"], []).append(dict(r))
    finally:
        c.close()

    mcode = D.meter_code(t["company"])
    fcode = D.form_code(t["company"]) if hasattr(D, "form_code") else "items1"
    rows = []
    for r in recs:
        rid = r["rid"]
        j = jm.get(rid)
        if not j:
            continue
        iss = json.loads(j.get("issues") or "[]")
        rv = rm.get(rid)
        eff_list = []
        for i in iss:
            sev = i.get("severity")
            if rv and rv.get("verdict") == "false_positive":
                continue
            if rv and rv.get("verdict") in ("critical", "warning"):
                sev = rv["verdict"]
            if sev in ("critical", "warning"):
                eff_list.append({
                    "items_code": i.get("items_code") or "",
                    "severity": i.get("severity"),
                    "eff_severity": sev,
                    "kind": i.get("kind") or "",
                    "detail": i.get("detail") or "",
                    "name": D.item_display(i.get("items_code"), None, t["company"]),
                })
        if not eff_list and not (rv and rv.get("verdict")):
            continue
        photos = []
        for p_ in ph.get(rid, []):
            local = (p_["local"] or "").replace("\\", "/")
            if local:
                path = "special/photo?t=%s&f=%s" % (
                    urllib.parse.quote(code), urllib.parse.quote(local))
            else:
                path = p_["url"] or ""
            photos.append({
                "items_code": p_["items_code"],
                "name": p_["category"] or D.item_display(p_["items_code"], p_["category"], t["company"]),
                "path": path,
                "url": p_["url"] or "",
                "ext_only": p_["ext_only"] or 0,
                "empty": p_["empty"] or 0,
            })
        photos.sort(key=lambda x: (0 if x["items_code"] == mcode else
                                   1 if x["items_code"] == fcode else 2))
        review = None
        if rv:
            review = {"verdict": rv.get("verdict"), "note": rv.get("note") or "",
                      "note_auto": rv.get("note_auto") or "",
                      "note_manual": rv.get("note_manual") or "",
                      "reviewer": rv.get("reviewer") or "",
                      "created_at": rv.get("updated_at") or ""}
        if pending_only and review and review.get("reviewer") != "月度复核":
            continue
        rows.append({
            "record_id": rid,
            "company": t["company"], "company_name": t["company_name"],
            "month": t["month"],
            "cust_no": r["cust_no"], "cust_name": r["cust_name"],
            "inspector": r["inspector"], "meter_no": r["meter_no"],
            "photo_count": r["photo_count"],
            "meter_type": "", "check_time": r["check_time"], "address": r["address"],
            "critical": sum(1 for x in eff_list if x["eff_severity"] == "critical"),
            "warning": sum(1 for x in eff_list if x["eff_severity"] == "warning"),
            "kinds": "||".join(x["kind"] for x in eff_list),
            "issues": eff_list, "photos": photos, "review": review,
            "ai_verdict": r["jv"] or "",
            "rv1": (mrev.get(r["rid"]) or {}).get("verdict") or "",
            "rv1_note": (mrev.get(r["rid"]) or {}).get("note") or "",
            "rv2": r["rvv"] or "",
            "ph_items": len({x["items_code"] for x in photos}),
            "ph_shots": sum(1 for x in photos if not x["empty"]),
            "ph_empty": sum(1 for x in photos if x["empty"]),
        })
    rows.sort(key=lambda x: (-x["critical"], -x["warning"], x["record_id"]))
    for i, r in enumerate(rows, 1):
        r["seq"] = i
    pend = sum(1 for r in rows if not r["review"] or r["review"].get("reviewer") == "月度复核")
    return {"rows": rows, "pending": pend, "done": len(rows) - pend,
            "pending_only": pending_only}



def month_rates(company, month):
    """查主库（只读）：该公司该月每个安检员的抽样户数与合格率，用于专项 vs 月度对比"""
    import sqlite3
    path = os.path.join(BASE, "db.sqlite3")
    if not os.path.exists(path):
        return {}
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT r.inspector insp, COUNT(DISTINCT r.id) total,
                      COUNT(DISTINCT CASE WHEN i.record_id IS NOT NULL THEN r.id END) bad
                 FROM records r
                 LEFT JOIN v_issue_eff i
                        ON i.record_id = r.id AND i.eff_severity IN ('critical','warning')
                WHERE r.company=? AND substr(r.check_time,1,7)=? AND r.sampled=1
                GROUP BY 1""", (company, month)).fetchall()
        out = {}
        for r in rows:
            tt, bb = r["total"] or 0, r["bad"] or 0
            out[r["insp"]] = {
                "total": tt, "bad": bb, "pass": tt - bb,
                "rate": round((tt - bb) * 100.0 / tt, 1) if tt else None,
            }
        return out
    except Exception:
        return {}
    finally:
        con.close()


def export_html(tid):
    """生成外链版专项报告，返回文件路径"""
    import special_report as SR
    s = summary(tid)
    if not s:
        raise ValueError("任务不存在")
    t = s["task"]
    rows = recs_list(tid, "all")
    houses = []
    for r in rows:
        if r["eff"] == "pending":
            continue
        houses.append({
            "inspector": r["inspector"], "cust_no": r["cust_no"],
            "cust_name": r["cust_name"], "check_time": r["check_time"],
            "community": r["community"], "address": r["address"],
            "meter_no": r["meter_no"], "photos": r["photos"],
            "issues": r["issues"], "eff": r["eff"],
            "review": r["review"], "note": r["note"], "comment": r["comment"],
            "rid": r["rid"], "rv1": r.get("rv1") or "", "rv1_note": r.get("rv1_note") or "",
            "ai_verdict": r.get("ai_verdict") or "",
        })
    m = t["month"].split("-")
    mlab = "%s年%s月" % (m[0], int(m[1])) if len(m) == 2 else t["month"]
    fname = "专项复核报告_%s_%s_%d人.html" % (
        t["company_name"], mlab, len(t["inspectors"]))
    root = os.path.join(os.path.dirname(BASE), "安检报告", "专项", t["code"])
    out = os.path.join(root, fname)
    SR.build(s, houses, out)
    return out, fname



def export_xlsx(tid):
    """生成专项 Excel，返回 (路径, 文件名)"""
    import special_xlsx as SX
    s = summary(tid)
    if not s:
        raise ValueError("任务不存在")
    t = s["task"]
    rows = [r for r in recs_list(tid, "all") if r["eff"] != "pending"]
    m = t["month"].split("-")
    mlab = "%s年%s月" % (m[0], int(m[1])) if len(m) == 2 else t["month"]
    data = SX.build(s, rows, t["company_name"], mlab)
    fname = SX.filename(t["company_name"], mlab, len(rows))
    root = os.path.join(os.path.dirname(BASE), "安检报告", "专项", t["code"])
    os.makedirs(root, exist_ok=True)
    out = os.path.join(root, fname)
    with open(out, "wb") as f:
        f.write(data)
    return out, fname


def undo_review(tid, rid):
    c = conn()
    try:
        c.execute("DELETE FROM reviews WHERE task_id=? AND rid=?", (tid, rid))
        c.commit()
    finally:
        c.close()


def options():
    """新建任务用：公司列表 + 主库近 13 个月"""
    import db as D
    mc = main_conn()
    try:
        ms = [r[0] for r in mc.execute(
            "SELECT DISTINCT substr(check_time,1,7) m FROM records "
            "WHERE m IS NOT NULL ORDER BY m DESC LIMIT 18")]
    finally:
        mc.close()
    return {"companies": [{"code": c, "name": n} for c, n in D.COMPANY_ORDER],
            "months": ms}


def inspectors_of(company, month):
    """新建任务时列人选（主库只读）——带每人可查户数"""
    import db as D
    mc = main_conn()
    cond, args = D.checkable_cond(company, "r")
    try:
        rows = mc.execute(
            f"""SELECT r.inspector, COUNT(*) n FROM records r
                 WHERE r.company=? AND substr(r.check_time,1,7)=? AND {cond}
                 GROUP BY 1 ORDER BY n DESC""",
            [company, month] + list(args)).fetchall()
    finally:
        mc.close()
    return [{"name": r["inspector"], "pool": r["n"]} for r in rows]


# ---------------------------------------------------------------- HTTP
def _json(h, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(body)


def _html(h, text, code=200):
    body = text.encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", "text/html; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(body)


def _file(h, p, ctype=None):
    if not os.path.exists(p):
        return _html(h, "not found", 404)
    ctype = ctype or mimetypes.guess_type(p)[0] or "application/octet-stream"
    with open(p, "rb") as f:
        body = f.read()
    h.send_response(200)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "max-age=3600")
    h.end_headers()
    h.wfile.write(body)


def _body(h):
    n = int(h.headers.get("Content-Length") or 0)
    if not n:
        return {}
    try:
        return json.loads(h.rfile.read(n).decode("utf-8"))
    except Exception:
        return {}


def handle(h, path, q):
    """GET 入口；返回 True 表示已处理"""
    def q1(k, d=""):
        return (q.get(k, [d])[0] or d)

    _ensure_db()

    if path in ("/special", "/special/"):
        return _file(h, os.path.join(STATIC, "special.html")), True

    if path in ("/special/house", "/special/house/"):
        return _file(h, os.path.join(STATIC, "special_house.html")), True

    if path == "/api/special/house":
        d = house_detail(int(q1("id", "0") or 0), int(q1("rid", "0") or 0))
        return _json(h, d or {"error": "not found"}), True

    if path == "/special/photo":
        code = q1("t")
        rel = q1("f")
        base = os.path.join(SPECIAL_DIR, code)
        p = os.path.normpath(os.path.join(base, rel))
        if not p.startswith(base):
            return _html(h, "forbidden", 403), True
        if not os.path.exists(p):
            # fallback：月度已下载的照片仍在主 photos/ 下（只读引用，不复制）
            p2 = os.path.normpath(os.path.join(BASE, rel))
            if p2.startswith(BASE) and os.path.exists(p2):
                return _file(h, p2), True
        return _file(h, p), True

    if path == "/api/special/tasks":
        return _json(h, {"tasks": list_tasks()}), True

    if path == "/api/special/task":
        s = summary(int(q1("id", "0") or 0))
        return _json(h, s or {"error": "not found"}), True

    if path == "/api/special/xlsx":
        try:
            out, fname = export_xlsx(int(q1("id", "0") or 0))
        except Exception as e:
            return _json(h, {"error": str(e)}, 400), True
        with open(out, "rb") as f:
            body = f.read()
        h.send_response(200)
        h.send_header("Content-Type",
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Content-Disposition",
                      "attachment; filename*=UTF-8''%s" % urllib.parse.quote(fname))
        h.end_headers()
        h.wfile.write(body)
        return True, True

    if path == "/api/special/download":
        try:
            out, fname = export_html(int(q1("id", "0") or 0))
        except Exception as e:
            return _json(h, {"error": str(e)}, 400), True
        with open(out, "rb") as f:
            body = f.read()
        h.send_response(200)
        h.send_header("Content-Type", "text/html; charset=utf-8")
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Content-Disposition",
                      "attachment; filename*=UTF-8''%s" % urllib.parse.quote(fname))
        h.end_headers()
        h.wfile.write(body)
        return True, True

    if path == "/api/special/queue":
        tid = int(q1("id", "0") or 0)
        po = q1("pending_only", "0") in ("1", "true", "yes")
        return _json(h, review_queue(tid, po)), True

    if path == "/api/special/recs":
        tid = int(q1("id", "0") or 0)
        return _json(h, {"recs": recs_list(tid, q1("filter", "all"),
                                           q1("inspector") or None)}), True

    if path == "/api/special/options":
        return _json(h, options()), True

    if path == "/api/special/inspectors":
        return _json(h, {"inspectors": inspectors_of(q1("company"), q1("month"))}), True

    return _json(h, {"error": "no route " + path}, 404), True


def handle_post(h, path):
    _ensure_db()
    body = _body(h)
    if path == "/api/special/tasks":
        try:
            return _json(h, create_task(body)), True
        except Exception as e:
            return _json(h, {"error": str(e)}, 400), True
    if path == "/api/special/review":
        save_review(int(body.get("task_id")), int(body.get("record_id")),
                    body.get("verdict"), body.get("note", ""),
                    body.get("tags"), body.get("reviewer", "人工"),
                    body.get("note_auto", ""), body.get("note_manual", ""))
        return _json(h, {"ok": True}), True
    if path == "/api/special/review/undo":
        undo_review(int(body.get("task_id")), int(body.get("record_id")))
        return _json(h, {"ok": True}), True
    return _json(h, {"error": "no route " + path}, 404), True


# ---------------------------------------------------------------- CLI
def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="专项任务 CLI（独立库，不碰主库）")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("new", help="建任务")
    p.add_argument("--company", required=True)
    p.add_argument("--month", required=True)
    p.add_argument("--inspectors", required=True, help="逗号分隔")
    p.add_argument("--requirement", default="")
    p.add_argument("--name", default="")
    sub.add_parser("list", help="列任务")
    s = sub.add_parser("summary", help="看汇总")
    s.add_argument("--id", type=int, required=True)
    args = ap.parse_args()

    if args.cmd == "new":
        r = create_task({"company": args.company, "month": args.month,
                         "inspectors": [x for x in args.inspectors.split(",") if x.strip()],
                         "requirement": args.requirement, "name": args.name})
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif args.cmd == "list":
        print(json.dumps(list_tasks(), ensure_ascii=False, indent=2))
    elif args.cmd == "summary":
        s = summary(args.id)
        print(json.dumps({"totals": s["totals"],
                          "inspectors": s["inspectors"]}, ensure_ascii=False, indent=2))
    else:
        ap.print_help()


if __name__ == "__main__":
    _cli()
