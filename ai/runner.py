# -*- coding: utf-8 -*-
"""运行编排 - Excel导入 → 抽样 → 下载 → 检测 → 分拣 → 报告

断点续查：checks 表按 (record_id, items_code) 唯一，恢复时只跑 pending/failed。
"""
import os, re, json, time, hashlib, datetime, sqlite3, traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import db as D
import engine as E
from db import COMPANY_NAME, ITEMS_NAME

VALID_EXT = (".jpg", ".jpeg", ".png")
ITEMS_RE = re.compile(r"-f_(items\d+|overall_path)\.", re.I)
# 检查项 → 是否属于"要发给模型"的组（表具照片必查，其余按项）
ALL_ITEMS = ["items0", "items1", "items2", "items3", "items4", "items5",
             "items6", "items7", "items8", "items9", "items10", "overall_path"]
# 每户最多发几张（防超上下文）
MAX_PHOTOS_PER_CHECK = 6


def month_of(check_time):
    m = re.match(r"(\d{4})-(\d{2})", str(check_time or ""))
    return f"{m.group(1)}-{m.group(2)}" if m else "unknown"


def clean_urls(value):
    if not value:
        return []
    out = []
    for line in str(value).split("\n"):
        u = line.strip()
        if u.lower().endswith(VALID_EXT) and ITEMS_RE.search(u):
            out.append(u)
    return out


def items_of(url):
    m = ITEMS_RE.search(url)
    return m.group(1).lower() if m else None


class Job:
    """一次运行任务，带事件总线供前端 SSE 订阅"""

    def __init__(self, company, month, cfg, run_id=None):
        self.company = company
        self.month = month
        self.cfg = cfg
        self.run_id = run_id
        self.adapter = None
        self.events = []          # 最近事件（前端轮询用）
        self.listeners = []
        self.stop = False
        self.state = {
            "phase": "idle", "total_records": 0, "total_checks": 0,
            "done_checks": 0, "fail_checks": 0, "downloaded": 0, "total_photos": 0,
            "current_cust": "", "current_items": "", "issues": 0,
            "tokens_in": 0, "tokens_out": 0, "cost": 0, "log": [],
        }

    # ---------- 事件 ----------
    def emit(self, type_, **kw):
        ev = {"t": type_, "ts": time.time(), **kw}
        self.events.append(ev)
        if len(self.events) > 400:
            self.events = self.events[-200:]
        for q in list(self.listeners):
            try:
                q.append(ev)
            except Exception:
                pass

    def log(self, msg):
        line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}"
        self.state["log"].append(line)
        if len(self.state["log"]) > 300:
            self.state["log"] = self.state["log"][-150:]
        self.emit("log", line=line)

    def subscribe(self):
        q = []
        self.listeners.append(q)
        return q

    def unsubscribe(self, q):
        if q in self.listeners:
            self.listeners.remove(q)

    # ---------- 主流程 ----------
    def run(self, src_file):
        try:
            self._run(src_file)
        except Exception:
            self.log("运行异常: " + traceback.format_exc(limit=3))
            self.state["phase"] = "error"
            self.emit("error")

    def _run(self, src_file):
        from models import build_adapter
        self.adapter = build_adapter(self.cfg)
        con = D.connect()
        self.run_id = con.execute(
            "INSERT INTO runs(company,month,status,model_used,started_at) VALUES(?,?,?,?,?)",
            (self.company, self.month, "running",
             f"{self.cfg.get('model_source')}/{self.cfg.get('model_id')}",
             datetime.datetime.now().isoformat(timespec="seconds"))).lastrowid
        con.commit()

        # 1. 导入
        self.state["phase"] = "import"
        self.log(f"开始导入 {os.path.basename(src_file)}")
        n = self.import_excel(con, src_file)
        self.log(f"导入完成：{n} 户")

        # 2. 抽样
        self.state["phase"] = "sample"
        recs = self.sample(con)
        self.state["total_records"] = len(recs)
        self.log(f"抽样完成：{len(recs)} 户（每人 {self.cfg.get('sample_ratio', 0.1):.0%}）")

        # 3. 建 checks（幂等）
        total_checks = self.build_checks(con, recs)
        self.state["total_checks"] = total_checks
        self.log(f"检查项共 {total_checks} 个（按检查项原子提交，可断点续查）")

        # 4. 下载
        self.state["phase"] = "download"
        self.download_all(con, recs)
        self.log(f"照片下载完成：{self.state['downloaded']} 张")

        # 5. 检测
        self.state["phase"] = "check"
        self.check_all(con, recs)

        # 6. 汇总
        con.execute("UPDATE runs SET status='done', finished_at=?, total_records=?, "
                    "total_checks=?, done_checks=?, fail_checks=?, tokens_in=?, tokens_out=?, cost=? WHERE id=?",
                    (datetime.datetime.now().isoformat(timespec="seconds"),
                     self.state["total_records"], self.state["total_checks"],
                     self.state["done_checks"], self.state["fail_checks"],
                     self.state["tokens_in"], self.state["tokens_out"], self.state["cost"], self.run_id))
        con.commit()
        self.state["phase"] = "done"
        self.log(f"全部完成：问题 {self.state['issues']} 项，tokens 入{self.state['tokens_in']}/出{self.state['tokens_out']}")

    # ---------- 导入 ----------
    def import_excel(self, con, src_file):
        from openpyxl import load_workbook
        wb = load_workbook(src_file, read_only=True, data_only=True)
        ws = wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        header = next(rows)
        header = [str(h).strip() if h else "" for h in header]
        idx = {h: i for i, h in enumerate(header)}

        need = ["客户编号", "客户姓名", "客户电话", "客户类型", "小区", "客户地址",
                "安检状态", "到访不遇次数", "安检时间", "安检员", "安检计划名称",
                "安检计划类型", "表类型", "表品牌表型号", "用气性质", "表号", "隐患明细"]
        for k in need:
            if k not in idx:
                raise RuntimeError(f"Excel 缺少列：{k}")
        photo_cols = [h for h in header if h and ("照片" in h or "图片" in h)]
        if not photo_cols:
            raise RuntimeError("Excel 未找到照片列")
        self.log(f"识别到 {len(photo_cols)} 个照片列")

        company = self.company
        month = self.month
        n = 0
        for r in rows:
            if not r or not r[idx["客户编号"]]:
                continue
            cust = str(r[idx["客户编号"]]).strip()
            status = str(r[idx["安检状态"]] or "").strip()
            # 只保留入户
            if status != "入户":
                continue
            # 同客户取最新
            cur = con.execute(
                "SELECT id, check_time FROM records WHERE company=? AND cust_no=? AND src_file=?",
                (company, cust, os.path.basename(src_file))).fetchone()
            t = str(r[idx["安检时间"]] or "")
            if cur:
                if t <= (cur["check_time"] or ""):
                    continue
                con.execute("DELETE FROM records WHERE id=?", (cur["id"],))
                con.execute("DELETE FROM photos WHERE record_id=?", (cur["id"],))
                con.execute("DELETE FROM checks WHERE record_id=?", (cur["id"],))
            rid = con.execute(
                """INSERT INTO records(company,cust_no,cust_name,phone,cust_type,community,address,
                   check_status,visit_fail,check_time,inspector,plan_name,plan_type,meter_type,
                   meter_brand,gas_nature,meter_no,hazard_raw,photo_count,src_file)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (company, cust, str(r[idx["客户姓名"]] or ""), str(r[idx["客户电话"]] or ""),
                 str(r[idx["客户类型"]] or ""), str(r[idx["小区"]] or ""), str(r[idx["客户地址"]] or ""),
                 status, r[idx["到访不遇次数"]] or 0, t, str(r[idx["安检员"]] or ""),
                 str(r[idx["安检计划名称"]] or ""), str(r[idx["安检计划类型"]] or ""),
                 str(r[idx["表类型"]] or ""), str(r[idx["表品牌表型号"]] or ""),
                 str(r[idx["用气性质"]] or ""), str(r[idx["表号"]] or ""),
                 str(r[idx["隐患明细"]] or ""), 0, os.path.basename(src_file))).lastrowid
            # 照片行（同户同 URL 跨列重复只存一条：某些户
            # 「总体安检照片」与「安检过程中照片」填了同一 URL，
            # 会撞 photos(record_id,url) 唯一约束）
            pc = 0
            seen_urls = set()
            for col in photo_cols:
                for u in clean_urls(r[idx[col]]):
                    if u in seen_urls:
                        continue
                    seen_urls.add(u)
                    ic = items_of(u)
                    if not ic:
                        continue
                    # 表具照片按 Excel 列名判定（A 公司在 items4，E 公司在 items2，
                    # 硬编码 items2 会把 A 公司的「安检过程中照片」误标为表具）
                    is_meter = 1 if "表具" in col else (2 if ic in ("items1", "overall_path") else 0)
                    cur = con.execute(
                        """INSERT OR IGNORE INTO photos(record_id,company,month,category,items_code,seq,
                           is_meter_photo,url) VALUES(?,?,?,?,?,?,?,?)""",
                        (rid, company, month, col, ic, 0, is_meter, u))
                    if cur.rowcount:
                        pc += 1
            con.execute("UPDATE records SET photo_count=? WHERE id=?", (pc, rid))
            n += 1
        con.commit()
        wb.close()
        self.emit("imported", records=n)
        return n

    # ---------- 抽样 ----------
    def sample(self, con):
        """每个安检员查本人入户总数的 ratio，不足 min 户查 min 户"""
        ratio = float(self.cfg.get("sample_ratio", 0.1))
        minp = int(self.cfg.get("min_per_inspector", 1))
        rows = con.execute(
            "SELECT id FROM records WHERE company=? ORDER BY inspector, id", (self.company,)).fetchall()
        by = {}
        for r in rows:
            insp = con.execute("SELECT inspector FROM records WHERE id=?", (r["id"],)).fetchone()["inspector"]
            by.setdefault(insp or "(未填)", []).append(r["id"])
        picked = []
        for insp, ids in by.items():
            n = max(minp, int(len(ids) * ratio + 0.5))
            n = min(n, len(ids))
            picked.extend(ids[:n])
        # 打标记
        con.execute("UPDATE records SET sampled=0 WHERE company=?", (self.company,))
        for i in range(0, len(picked), 500):
            con.executemany("UPDATE records SET sampled=1 WHERE id=?",
                            [(i,) for i in picked[i:i + 500]])
        con.commit()
        # 若库里已有 sampled 列缺失则补
        self._ensure_sampled(con)
        self.emit("sampled", total=len(picked), inspectors=len(by))
        return picked

    def _ensure_sampled(self, con):
        cols = [c[1] for c in con.execute("PRAGMA table_info(records)")]
        if "sampled" not in cols:
            con.execute("ALTER TABLE records ADD COLUMN sampled INTEGER DEFAULT 0")
            con.commit()

    # ---------- checks ----------
    def build_checks(self, con, rec_ids):
        total = 0
        for rid in rec_ids:
            ph = con.execute(
                "SELECT id, items_code, downloaded FROM photos WHERE record_id=?", (rid,)).fetchall()
            # 按 items_code 归组
            groups = {}
            for p in ph:
                groups.setdefault(p["items_code"], []).append(p["id"])
            for ic, ids in groups.items():
                try:
                    con.execute(
                        "INSERT OR IGNORE INTO checks(record_id,company,month,items_code,photo_ids,status)"
                        " VALUES(?,?,?,?,?,'pending')",
                        (rid, self.company, self.month, ic, ",".join(map(str, ids))))
                    total += 1
                except sqlite3.IntegrityError:
                    pass
        con.commit()
        return con.execute("SELECT COUNT(*) c FROM checks WHERE company=? AND month=?",
                           (self.company, self.month)).fetchone()["c"]

    # ---------- 下载 ----------
    def download_all(self, con, rec_ids):
        todo = con.execute(
            """SELECT p.id, p.url, p.items_code, r.cust_no FROM photos p
               JOIN records r ON r.id=p.record_id
               WHERE p.downloaded=0 AND r.id IN (%s)""" % ",".join("?" * len(rec_ids)),
            rec_ids).fetchall()
        self.state["total_photos"] = len(todo)
        self.log(f"待下载照片 {len(todo)} 张")
        if not todo:
            return
        month_dir = os.path.join(D.PHOTOS_DIR, self.company, self.month)
        os.makedirs(month_dir, exist_ok=True)

        def one(row):
            if self.stop:
                return None
            cust = row["cust_no"]
            d = os.path.join(month_dir, cust)
            os.makedirs(d, exist_ok=True)
            name = f"{row['items_code']}_{row['id']}.jpg"
            dst = os.path.join(d, name)
            try:
                blob = E.fetch(row["url"])
                if len(blob) < 512:
                    raise ValueError("响应过小")
                sha = hashlib.sha256(blob).hexdigest()
                with open(dst, "wb") as f:
                    f.write(blob)
                return (row["id"], dst, sha, len(blob))
            except Exception as ex:
                return ("ERR", row["id"], str(ex)[:120])

        done = 0
        with ThreadPoolExecutor(max_workers=int(self.cfg.get("workers", 3)) or 3) as ex:
            futs = {ex.submit(one, r): r for r in todo}
            for f in as_completed(futs):
                res = f.result()
                if not res:
                    continue
                if res[0] == "ERR":
                    self.log(f"下载失败 照片id={res[1]} {res[2]}")
                    continue
                pid, dst, sha, sz = res
                con.execute("UPDATE photos SET local=?, sha256=?, bytes=?, downloaded=1 WHERE id=?",
                            (os.path.relpath(dst, D.BASE).replace("\\", "/"), sha, sz, pid))
                done += 1
                self.state["downloaded"] = done
                if done % 5 == 0 or done == len(todo):
                    con.commit()
                    self.emit("download", done=done, total=len(todo),
                              rel=os.path.relpath(dst, D.BASE).replace("\\", "/"))
        con.commit()
        self.emit("download_done", done=done, total=len(todo))

    # ---------- 检测 ----------
    def check_all(self, con, rec_ids):
        rows = con.execute(
            """SELECT c.id, c.items_code, c.photo_ids, c.status, r.cust_no, r.cust_name,
                      r.inspector, r.check_time, r.meter_no, r.meter_type, r.address, r.photo_count
               FROM checks c JOIN records r ON r.id=c.record_id
               WHERE c.company=? AND c.month=? AND c.status!='done'""",
            (self.company, self.month)).fetchall()
        pend = [r for r in rows if r["status"] == "pending"]
        fail = [r for r in rows if r["status"] == "failed"]
        self.log(f"待检测检查项 {len(pend)} 个（其中重试 {len(fail)} 个）")
        queue = pend + fail
        self.state["total_checks"] = len(queue)
        workers = max(1, int(self.cfg.get("workers", 3)) or 3)

        def work(row):
            if self.stop:
                return None
            con2 = D.connect()
            try:
                con2.execute("UPDATE checks SET status='running', attempts=attempts+1 WHERE id=?",
                             (row["id"],))
                con2.commit()
                # 收集该检查项的照片
                ids = [int(x) for x in (row["photo_ids"] or "").split(",") if x.strip()]
                ph = con2.execute(
                    "SELECT id, local, url FROM photos WHERE id IN (%s) AND downloaded=1"
                    % ",".join("?" * len(ids)), ids).fetchall() if ids else []
                if not ph:
                    # 下载阶段失败的，尝试再下
                    ph = con2.execute(
                        "SELECT id, local, url FROM photos WHERE id IN (%s)"
                        % ",".join("?" * len(ids)), ids).fetchall() if ids else []
                    paths = []
                    for p in ph:
                        try:
                            d = os.path.join(D.PHOTOS_DIR, self.company, self.month, row["cust_no"])
                            os.makedirs(d, exist_ok=True)
                            dst = os.path.join(d, f"{row['items_code']}_{p['id']}.jpg")
                            if not os.path.exists(dst):
                                blob = E.fetch(p["url"])
                                with open(dst, "wb") as f:
                                    f.write(blob)
                            con2.execute("UPDATE photos SET local=?, downloaded=1 WHERE id=?",
                                         (os.path.relpath(dst, D.BASE).replace("\\", "/"), p["id"]))
                            con2.commit()
                            paths.append(dst)
                        except Exception:
                            pass
                else:
                    paths = [os.path.normpath(os.path.join(D.BASE, p["local"])) for p in ph]
                paths = [p for p in paths if os.path.exists(p)][:MAX_PHOTOS_PER_CHECK]
                if not paths:
                    raise RuntimeError("该检查项无可用照片")

                prompt = E.PROMPT_TEMPLATE.format(
                    cust_no=row["cust_no"], cust_name=row["cust_name"], inspector=row["inspector"],
                    check_time=row["check_time"], meter_no=row["meter_no"],
                    meter_type=row["meter_type"], address=row["address"],
                    photo_count=len(paths), items_name=ITEMS_NAME.get(row["items_code"], row["items_code"]))
                r = self.adapter.chat_with_images(paths, prompt, timeout=180)
                parsed = E.parse_json(r["text"])
                probs = E.judge(parsed)
                tin = int(r.get("tokens_in") or 0)
                tout = int(r.get("tokens_out") or 0)
                con2.execute(
                    "UPDATE checks SET status='done', result_json=?, tokens_in=?, tokens_out=?,"
                    " error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (json.dumps(parsed, ensure_ascii=False) if parsed else "", tin, tout, row["id"]))
                # 落问题
                for kind, sev, detail in probs:
                    con2.execute(
                        """INSERT INTO issues(run_id,record_id,company,month,items_code,severity,kind,detail)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (self.run_id, self._rid(con2, row["cust_no"]), self.company, self.month,
                         row["items_code"], sev, kind, detail))
                con2.commit()
                return ("OK", row, parsed, probs, tin, tout)
            except Exception as ex:
                try:
                    con2.execute("UPDATE checks SET status='failed', error=? WHERE id=?",
                                 (str(ex)[:200], row["id"]))
                    con2.commit()
                except Exception:
                    pass
                return ("FAIL", row, str(ex)[:150])
            finally:
                con2.close()

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(work, r): r for r in queue}
            for f in as_completed(futs):
                res = f.result()
                if not res:
                    continue
                if res[0] == "OK":
                    _, row, parsed, probs, tin, tout = res
                    self.state["done_checks"] += 1
                    self.state["fail_checks"] = max(0, self.state["fail_checks"])
                    self.state["tokens_in"] += tin
                    self.state["tokens_out"] += tout
                    self.state["issues"] += len(probs)
                    self.state["current_cust"] = row["cust_no"]
                    self.state["current_items"] = ITEMS_NAME.get(row["items_code"], row["items_code"])
                    lvl = "critical" if any(p[1] == "critical" for p in probs) else (
                        "warning" if probs else "ok")
                    self.emit("check", cust=row["cust_no"], name=row["cust_name"],
                              insp=row["inspector"], items=row["items_code"],
                              items_name=ITEMS_NAME.get(row["items_code"], row["items_code"]),
                              level=lvl, problems=[p[0] for p in probs],
                              done=self.state["done_checks"], total=self.state["total_checks"])
                    # 照片分拣
                    try:
                        self._triage(con, row, probs)
                    except Exception:
                        pass
                else:
                    _, row, err = res
                    self.state["fail_checks"] += 1
                    self.emit("check_fail", cust=row["cust_no"], items=row["items_code"], err=err)
                    self.log(f"检测失败 {row['cust_no']}/{row['items_code']}: {err}")
        # 收尾：把 failed 改回 pending 以便下次续跑
        con.execute("UPDATE checks SET status='pending' WHERE company=? AND month=? AND status='failed'",
                    (self.company, self.month))
        con.commit()

    def _rid(self, con, cust_no):
        r = con.execute("SELECT id FROM records WHERE company=? AND cust_no=? LIMIT 1",
                        (self.company, cust_no)).fetchone()
        return r["id"] if r else None

    def _triage(self, con, row, probs):
        """照片分拣：有 critical → 留存 photos/；否则 → pending_delete/"""
        ids = [int(x) for x in (row["photo_ids"] or "").split(",") if x.strip()]
        if not ids:
            return
        ph = con.execute("SELECT id, local FROM photos WHERE id IN (%s)" % ",".join("?" * len(ids)), ids).fetchall()
        has_critical = any(p[1] == "critical" for p in probs)
        for p in ph:
            if not p["local"]:
                continue
            src = os.path.normpath(os.path.join(D.BASE, p["local"]))
            if not os.path.exists(src):
                continue
            kind = "photos" if has_critical else "pending_delete"
            dst_dir = E.photo_dir(self.company, self.month, row["cust_no"], kind)
            name = os.path.basename(src)
            dst = os.path.join(dst_dir, name)
            try:
                if os.path.abspath(src) != os.path.abspath(dst):
                    if os.path.exists(dst):
                        os.remove(src)
                    else:
                        shutil.move(src, dst)
                con.execute("UPDATE photos SET moved_to=? WHERE id=?",
                            (os.path.relpath(dst, D.BASE).replace("\\", "/"), p["id"]))
            except Exception:
                pass
        con.commit()

    def cost(self):
        return {"tokens_in": self.state["tokens_in"], "tokens_out": self.state["tokens_out"],
                "cost": round(self.state["cost"], 4)}


import shutil  # noqa: E402
