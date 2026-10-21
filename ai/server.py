# -*- coding: utf-8 -*-
"""安检抽查系统 - HTTP 服务（Python 标准库，零依赖）

路由：
  GET  /                      运行页
  GET  /live                  实时看板（大屏）
  GET  /report                月度报告
  GET  /settings              设置
  GET  /api/state             当前运行状态
  GET  /api/stream            SSE 实时事件
  POST /api/run               开始一次运行
  POST /api/stop              停止
  GET  /api/config            读配置
  POST /api/config            存配置
  POST /api/selfcheck         模型视觉能力自检
  GET  /api/companies         公司列表
  GET  /api/dashboard         各分公司状态总览（首页仪表盘）
  GET  /api/report/export6    一次性生成各分公司独立报告
  GET  /api/records           户列表（分页）
  GET  /api/issues            问题列表
  POST /api/triage/cleanup    一键清理待删除
  GET  /api/report/data       报告数据（JSON）
  GET  /api/report/export     导出 HTML 报告
"""
import os, sys, json, time, datetime, threading, urllib.parse, mimetypes, shutil, html

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db as D
import runner as R
from db import COMPANY_ORDER, COMPANY_NAME

# 2026-10-19 用户定：不再按问题类型限制配图（时间类也把照片摆出来）
RR_NO_PHOTO = set()

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
PORT = int(os.environ.get("AJ_PORT", "8790"))

_current = None       # 当前 Job
_lock = threading.Lock()


class Handler(__import__("http.server", fromlist=["x"]).BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AnJian/1.0"

    def log_message(self, *a):
        pass

    # ---------- 工具 ----------
    def _send(self, code, body=b"", ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _static(self, path):
        p = os.path.normpath(os.path.join(STATIC, path.lstrip("/")))
        if not p.startswith(STATIC) or not os.path.exists(p):
            return self._send(404, "not found")
        ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
        if p.endswith(".js"):
            ctype = "text/javascript; charset=utf-8"
        elif p.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        with open(p, "rb") as f:
            self._send(200, f.read(), ctype)

    def _photo(self, rel):
        """读本地照片（给前端 <img> 用）"""
        p = os.path.normpath(os.path.join(BASE, rel))
        if not p.startswith(BASE) or not os.path.exists(p):
            return self._send(404, "not found")
        with open(p, "rb") as f:
            self._send(200, f.read(), "image/jpeg")

    # ---------- 路由 ----------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        q = urllib.parse.parse_qs(u.query)
        try:
            if path == "/" or path == "/index.html":
                return self._static("index.html")
            if path == "/live":
                return self._static("live.html")
            if path == "/report":
                return self._static("report.html")
            if path == "/report/detail":
                return self._static("report_detail.html")
            if path == "/review":
                return self._static("review.html")
            if path == "/settings":
                return self._static("settings.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if (path.startswith("/photos/") or path.startswith("/pending_delete/")
                    or path.startswith("/false_positive/")):
                return self._photo(path.lstrip("/"))
            if path == "/api/state":
                return self._json(self._state())
            if path == "/api/stream":
                return self._stream()
            if path == "/api/config":
                return self._json(D.load_config())
            if path == "/api/companies":
                return self._json([{"code": c, "name": n} for c, n in COMPANY_ORDER])
            if path == "/api/records":
                return self._records(q)
            if path == "/api/issues":
                return self._issues(q)
            if path == "/api/queue":
                return self._queue(q)
            if path == "/api/reviews":
                return self._reviews(q)
            if path == "/api/stats":
                return self._json(D.stats_overview())
            if path == "/api/dashboard":
                return self._json(self._dashboard(q))
            if path == "/api/resume":
                return self._json(self._resume(q))
            if path == "/api/report/export6":
                return self._json(self._export6(q))
            if path == "/api/report/data":
                return self._json(self._report_data(q))
            if path == "/api/report/overview":
                return self._json(self._report_overview(q))
            if path == "/api/report/detail":
                return self._json(self._report_detail(q))
            if path == "/api/report/download":
                return self._report_download(q)
            if path == "/api/report/view":
                return self._report_download(q, inline=True)
            if path == "/api/pending":
                return self._json(self._pending_info())
            return self._send(404, "not found")
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/api/run":
                return self._start_run()
            if u.path == "/api/stop":
                return self._stop_run()
            if u.path == "/api/config":
                return self._save_config()
            if u.path == "/api/selfcheck":
                return self._selfcheck()
            if u.path == "/api/triage/cleanup":
                return self._cleanup()
            if u.path == "/api/review":
                return self._save_review()
            if u.path == "/api/review/undo":
                return self._undo_review()
            return self._send(404, "not found")
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    # ---------- 状态 ----------
    def _state(self):
        """有 runner.Job 用Job 的事件；Agent 通道（cli.py）没有 Job，
        直接从数据库实时算进度——两种通道大屏都能用。"""
        global _current
        cfg = D.load_config()
        if not _current:
            return {"running": False, "phase": "idle", "state": self._db_state(), "config": cfg}
        st = dict(_current.state)
        st["company_name"] = COMPANY_NAME.get(_current.company, _current.company)
        st["run_id"] = _current.run_id
        return {"running": _current.state["phase"] in ("import", "sample", "download", "check"),
                "phase": _current.state["phase"], "state": st, "config": _current.cfg}

    def _db_state(self):
        """从库里实时汇总——Agent 通道的进度来源。
        多公司：优先显示「当前有未判定户的公司+月」，避免与已跑完的公司混算。"""
        import cli
        con = D.connect()
        try:
            act = con.execute(
                """SELECT company, month, MAX(id) AS mid FROM checks
                   WHERE status!='done' GROUP BY company, month
                   ORDER BY mid DESC LIMIT 1""").fetchone()
            if act:
                st = cli.summary_from_db(con, act["company"], act["month"])
                st["company_name"] = COMPANY_NAME.get(act["company"], act["company"])
                st["month"] = act["month"]
                return st
            return cli.summary_from_db(con)
        except Exception as e:
            return {"error": str(e)}
        finally:
            con.close()

    def _stream(self):
        """SSE：优先推 Job 事件；没有 Job 时按数据库进度轮询推送，
        这样 Agent 通道（cli.py）也能让大屏动起来。"""
        global _current
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        if not _current:
            # 数据库轮询模式：进度一变就推
            last_sig = None
            deadline = time.time() + 3600
            while time.time() < deadline:
                try:
                    st = self._db_state()
                    sig = json.dumps({k: st.get(k) for k in
                                      ("done_checks", "total_checks", "done_records",
                                       "total_records", "current_cust", "issues")},
                                     ensure_ascii=False)
                    if sig != last_sig:
                        last_sig = sig
                        ev = {"t": "progress", "ts": time.time(), **st}
                        self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode())
                        self.wfile.flush()
                    else:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except Exception:
                    pass
                time.sleep(1.5)
            return
        q = _current.subscribe()
        try:
            last = 0
            evs = list(_current.events)
            for ev in evs[-30:]:
                self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode())
            self.wfile.flush()
            deadline = time.time() + 600
            while time.time() < deadline:
                try:
                    ev = q.pop(0)
                except IndexError:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    time.sleep(2)
                    continue
                self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
        except Exception:
            pass
        finally:
            _current.unsubscribe(q)

    # ---------- 运行 ----------
    def _start_run(self):
        global _current, _lock
        body = self._body()
        if _current and _current.state["phase"] in ("import", "sample", "download", "check"):
            return self._json({"error": "已有任务在运行"}, 409)
        src = body.get("src_file", "")
        company = body.get("company", "")
        month = body.get("month", "")
        if not src or not os.path.exists(src):
            return self._json({"error": f"源文件不存在: {src}"}, 400)
        cfg = D.load_config()
        for k in ("model_source", "model_id", "api_base", "api_key", "thinking",
                  "sample_ratio", "min_per_inspector", "workers", "max_attempts", "cost_limit"):
            if k in body:
                cfg[k] = body[k]
        D.save_config(cfg)
        if not month:
            month = datetime.datetime.now().strftime("%Y-%m")
        job = R.Job(company, month, cfg)
        _current = job
        threading.Thread(target=job.run, args=(src,), daemon=True).start()
        return self._json({"ok": True, "company": company, "month": month})

    def _stop_run(self):
        global _current
        if _current:
            _current.stop = True
            return self._json({"ok": True})
        return self._json({"ok": False, "error": "无任务"})

    # ---------- 配置 ----------
    def _save_config(self):
        cfg = D.load_config()
        cfg.update(self._body())
        D.save_config(cfg)
        return self._json({"ok": True, "config": cfg})

    def _selfcheck(self):
        from models import build_adapter, OpenAICompatAdapter
        cfg = D.load_config()
        cfg.update(self._body())
        a = build_adapter(cfg)
        if isinstance(a, OpenAICompatAdapter):
            return self._json(a.self_check())
        return self._json({"ok": False, "msg": "当前模型来源不可用，请切换"})

    # ---------- 查询 ----------
    def _records(self, q):
        con = D.connect()
        try:
            company = q.get("company", [""])[0]
            month = q.get("month", [""])[0]
            page = int(q.get("page", ["1"])[0])
            size = min(100, int(q.get("size", ["50"])[0]))
            w, args = "1=1", []
            if company:
                w += " AND r.company=?"; args.append(company)
            if month:
                w += " AND p.month=?"; args.append(month)
            total = con.execute(f"SELECT COUNT(*) c FROM records r LEFT JOIN photos p ON p.record_id=r.id WHERE {w}", args).fetchone()["c"]
            rows = con.execute(
                f"""SELECT r.id, r.company, r.cust_no, r.cust_name, r.inspector, r.check_time,
                           r.meter_no, r.meter_type, r.address,
                           (SELECT COUNT(*) FROM photos p WHERE p.record_id=r.id AND p.downloaded=1) photos,
                           (SELECT COUNT(*) FROM checks c WHERE c.record_id=r.id AND c.status='done') done,
                           (SELECT COUNT(*) FROM issues i WHERE i.record_id=r.id AND i.active=1) issues
                    FROM records r WHERE {w} ORDER BY r.id DESC LIMIT ? OFFSET ?""",
                args + [size, (page - 1) * size]).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["company_name"] = COMPANY_NAME.get(d["company"], d["company"])
                d["critical"] = con.execute(
                    "SELECT COUNT(*) c FROM issues WHERE record_id=? AND active=1 AND severity='critical'",
                    (r["id"],)).fetchone()["c"]
                out.append(d)
            return self._json({"total": total, "page": page, "size": size, "rows": out})
        finally:
            con.close()

    def _issues(self, q):
        con = D.connect()
        try:
            company = q.get("company", [""])[0]
            month = q.get("month", [""])[0]
            sev = q.get("severity", [""])[0]
            w, args = "i.active=1", []
            if company:
                w += " AND i.company=?"; args.append(company)
            if month:
                w += " AND i.month=?"; args.append(month)
            if sev:
                w += " AND i.severity=?"; args.append(sev)
            rows = con.execute(
                f"""SELECT i.*, r.cust_no, r.cust_name, r.inspector, r.meter_no, r.address, r.check_time
                    FROM issues i JOIN records r ON r.id=i.record_id
                    WHERE {w} ORDER BY i.id DESC LIMIT 500""", args).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["company_name"] = COMPANY_NAME.get(d["company"], d["company"])
                # 关联照片路径
                p = con.execute("SELECT local, moved_to FROM photos WHERE record_id=? AND items_code=? LIMIT 1",
                                (r["record_id"], r["items_code"])).fetchone()
                d["photo"] = (p and (p["moved_to"] or p["local"])) or ""
                rv = con.execute("SELECT verdict, note, reviewer, created_at FROM reviews WHERE record_id=?",
                                 (r["record_id"],)).fetchone()
                d["review"] = dict(rv) if rv else None
                out.append(d)
            return self._json({"rows": out})
        finally:
            con.close()

    def _pending_info(self):
        """待删除文件夹统计"""
        info = {"total_files": 0, "total_bytes": 0, "cutoff": None,
                "days": int(D.load_config().get("pending_keep_days", 30))}
        if os.path.isdir(D.PENDING_DIR):
            for root, _, files in os.walk(D.PENDING_DIR):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        info["total_files"] += 1
                        info["total_bytes"] += os.path.getsize(fp)
                    except OSError:
                        pass
        info["total_mb"] = round(info["total_bytes"] / 1048576, 1)
        return info

    # ---------- 人工复核 ----------
    def _queue(self, q):
        """待人工复核的户队列（2026-10-19 改：统一走 v_issue_eff 有效档位视图）

        - 误判户：视图里 eff_severity=NULL → 自动消失
        - 已人工定档的户：按裁决后的档位进/出队列
        - info 级（热水器项拍工作单）不算问题，不进队列
        - pending_only=1 只返回未复核户
        """
        con = D.connect()
        try:
            company = q.get("company", [""])[0]
            month = q.get("month", [""])[0]
            pending_only = q.get("pending_only", ["0"])[0] in ("1", "true", "yes")
            if q.get("summary", [""])[0] in ("1", "true", "yes"):
                w, args = "i.eff_severity IN ('critical','warning')", []
                if month:
                    w += " AND i.month=?"; args.append(month)
                out = []
                for r in con.execute(
                        f"""SELECT r.company,
                                   COUNT(DISTINCT r.id) AS total,
                                   COUNT(DISTINCT CASE WHEN v.record_id IS NOT NULL
                                         THEN r.id END) AS done
                            FROM records r
                            JOIN v_issue_eff i ON i.record_id=r.id
                            LEFT JOIN reviews v ON v.record_id=r.id
                            WHERE {w}
                            GROUP BY r.company
                            ORDER BY r.company""", args):
                    t, dd = r["total"], r["done"]
                    out.append({"code": r["company"],
                                "name": COMPANY_NAME.get(r["company"], r["company"]),
                                "total": t, "done": dd, "pending": max(0, t - dd)})
                return self._json({"groups": out, "month": month})
            w, args = "i.eff_severity IN ('critical','warning')", []
            if company:
                w += " AND r.company=?"; args.append(company)
            if month:
                w += " AND i.month=?"; args.append(month)
            if pending_only:
                w += " AND r.id NOT IN (SELECT record_id FROM reviews)"
            rows = con.execute(
                f"""SELECT r.id AS record_id, r.company, i.month, r.cust_no, r.cust_name,
                           r.inspector, r.meter_no, r.meter_type, r.check_time, r.address,
                           GROUP_CONCAT(i.kind, '||') AS kinds,
                           SUM(CASE WHEN i.eff_severity='critical' THEN 1 ELSE 0 END) critical,
                           SUM(CASE WHEN i.eff_severity='warning' THEN 1 ELSE 0 END) warning
                    FROM records r JOIN v_issue_eff i ON i.record_id=r.id
                    WHERE {w} GROUP BY r.id ORDER BY critical DESC, r.id""", args).fetchall()
            out = []
            for i, r in enumerate(rows, 1):
                d = dict(r)
                d["company_name"] = COMPANY_NAME.get(d["company"], d["company"])
                d["seq"] = i
                d["issues"] = [dict(x) for x in con.execute(
                    "SELECT items_code, severity, eff_severity, kind, detail "
                    "FROM v_issue_eff WHERE record_id=? ORDER BY severity",
                    (r["record_id"],)).fetchall()]
                for x in d["issues"]:
                    # 各公司 items 编号含义不同（A 公司表具在 items4、C 公司表具在 items1）
                    x["name"] = D.item_display(x.get("items_code"), None, r["company"])
                d["photos"] = [{"items_code": p["items_code"],
                                "name": D.item_display(p["items_code"], p["category"], r["company"]),
                                "path": p["moved_to"] or p["local"]}
                               for p in con.execute(
                    "SELECT items_code, local, moved_to, category FROM photos WHERE record_id=? AND COALESCE(moved_to,local)!=''"
                    " ORDER BY CASE items_code WHEN ? THEN 0 WHEN ? THEN 1 ELSE 2 END, id",
                    (r["record_id"], D.meter_code(r["company"]), D.form_code(r["company"]))).fetchall()]
                rv = con.execute(
                    "SELECT verdict, note, note_auto, note_manual, reviewer, created_at "
                    "FROM reviews WHERE record_id=?", (r["record_id"],)).fetchone()
                d["review"] = dict(rv) if rv else None
                out.append(d)
            return self._json({"rows": out,
                                "pending": sum(1 for x in out if not x["review"]),
                                "done": sum(1 for x in out if x["review"]),
                                "pending_only": pending_only})
        finally:
            con.close()

    def _reviews(self, q):
        """已复核记录列表"""
        con = D.connect()
        try:
            w, args = "1=1", []
            if q.get("company", [""])[0]:
                w += " AND v.company=?"; args.append(q["company"][0])
            if q.get("month", [""])[0]:
                w += " AND v.month=?"; args.append(q["month"][0])
            rows = con.execute(
                f"""SELECT v.*, r.cust_no, r.cust_name, r.inspector, r.meter_no
                    FROM reviews v JOIN records r ON r.id=v.record_id
                    WHERE {w} ORDER BY v.created_at DESC""", args).fetchall()
            return self._json({"rows": [{
                **dict(r), "company_name": COMPANY_NAME.get(r["company"], r["company"])}
                for r in rows]})
        finally:
            con.close()

    def _save_review(self):
        """保存一条人工复核意见（2026-10-19 重构：只写 reviews，不动 issues）

        三个档位：
          false_positive  误判      → 报表按裁决剔除，但 issues 原始判定永久留档
          warning         一般提示  → 报表按裁决降级
          critical        严重提示  → 报表按裁决升级
        取数统一走视图 v_issue_eff，写库只写一处，避免"双写打架"。

        note 拆两段存：note_auto（系统自动识别）/ note_manual（人工真写），
        报表判断"是否人工补充了意见"只看 note_manual。
        """
        b = self._body()
        rid = b.get("record_id")
        if not rid:
            return self._json({"error": "缺少 record_id"}, 400)
        verdict = b.get("verdict", "warning")
        note = (b.get("note") or "").strip()
        note_auto = (b.get("note_auto") or "").strip()
        note_manual = (b.get("note_manual") or "").strip()
        reviewer = (b.get("reviewer") or "").strip()
        if verdict not in ("false_positive", "warning", "critical"):
            return self._json({"error": f"verdict 只能是 "
                                         f"false_positive/warning/critical，收到 {verdict}"}, 400)
        if verdict in ("warning", "critical") and not note:
            note = note_manual or note_auto or "待复查"
        con = D.connect()
        try:
            r = con.execute("SELECT company, cust_no, check_time FROM records WHERE id=?",
                            (rid,)).fetchone()
            if not r:
                return self._json({"error": "户不存在"}, 404)
            old = con.execute("SELECT verdict, note FROM reviews WHERE record_id=?",
                              (rid,)).fetchone()
            m = con.execute("SELECT month FROM issues WHERE record_id=? ORDER BY id LIMIT 1",
                            (rid,)).fetchone()
            month = (r["check_time"] or "")[:7] if r["check_time"] else (
                m["month"] if m else datetime.datetime.now().strftime("%Y-%m"))
            con.execute(
                """INSERT OR REPLACE INTO reviews(record_id,company,month,seq,verdict,note,
                                                  note_auto,note_manual,reviewer,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (rid, r["company"], month, b.get("seq", 0), verdict, note,
                 note_auto, note_manual, reviewer))
            # 操作日志：改判 / 首次判定都留痕
            con.execute(
                """INSERT INTO review_log(record_id,company,month,action,
                                          verdict_from,verdict_to,note,reviewer)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (rid, r["company"], month,
                 "change" if old else "save",
                 old["verdict"] if old else "", verdict, note, reviewer))
            # 误判：照片挪到 false_positive/ 永久留档
            # issues 不再删除——撤销误判时照片要能移回来，判定也要能追溯
            if verdict == "false_positive":
                self._move_photos(con, rid, r["company"], r["cust_no"], month, to_fp=True)
            con.commit()
            return self._json({"ok": True, "record_id": rid, "verdict": verdict})
        finally:
            con.close()

    def _move_photos(self, con, rid, company, cust_no, month, to_fp=True):
        """误判 → false_positive/ 永久留档；撤销误判 → 移回 photos/"""
        # to_fp=True 是「挪进 false_positive」，False 是「挪回 photos」
        dst_dir = D.FALSE_POS_DIR if to_fp else D.PHOTOS_DIR
        n = 0
        for p in con.execute("SELECT id, local, moved_to FROM photos WHERE record_id=?",
                             (rid,)).fetchall():
            cur = p["local"] or p["moved_to"] or ""
            if not cur:
                continue
            is_fp = "false_positive" in cur.replace("\\", "/")
            if is_fp == bool(to_fp):
                continue                      # 已经在目标目录
            src = os.path.normpath(os.path.join(D.BASE, cur))
            if not os.path.exists(src):
                continue
            d = os.path.join(dst_dir, company, month, cust_no)
            os.makedirs(d, exist_ok=True)
            dst = os.path.join(d, os.path.basename(src))
            # 同一路径绝不能 remove：否则等于把原图删了（2026-10-19 踩过）
            if os.path.abspath(src) == os.path.abspath(dst):
                continue
            try:
                if os.path.exists(dst):
                    os.remove(src)
                else:
                    shutil.move(src, dst)
                rel = os.path.relpath(dst, D.BASE).replace("\\", "/")
                con.execute("UPDATE photos SET moved_to=?, local=? WHERE id=?",
                            (rel, rel, p["id"]))
                n += 1
            except Exception:
                pass
        return n

    def _undo_review(self):
        """撤销某户的人工复核（2026-10-19 加）：删 reviews 行 + 写日志 + 照片移回
        撤销后该户回到「待复核」队列，系统判定原样生效。"""
        b = self._body()
        rid = b.get("record_id")
        if not rid:
            return self._json({"error": "缺少 record_id"}, 400)
        reviewer = (b.get("reviewer") or "").strip()
        con = D.connect()
        try:
            r = con.execute("SELECT company, cust_no, check_time FROM records WHERE id=?",
                            (rid,)).fetchone()
            if not r:
                return self._json({"error": "户不存在"}, 404)
            old = con.execute("SELECT verdict, note, month FROM reviews WHERE record_id=?",
                              (rid,)).fetchone()
            if not old:
                return self._json({"error": "该户没有复核记录"}, 404)
            con.execute("DELETE FROM reviews WHERE record_id=?", (rid,))
            con.execute(
                """INSERT INTO review_log(record_id,company,month,action,
                                          verdict_from,verdict_to,note,reviewer)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (rid, r["company"], old["month"] or (r["check_time"] or "")[:7], "undo",
                 old["verdict"], "", old["note"], reviewer))
            moved = 0
            if old["verdict"] == "false_positive":
                moved = self._move_photos(con, rid, r["company"], r["cust_no"],
                                          old["month"] or (r["check_time"] or "")[:7],
                                          to_fp=False)
            con.commit()
            has_issues = con.execute(
                "SELECT COUNT(*) c FROM issues WHERE record_id=? AND active=1",
                (rid,)).fetchone()["c"]
            return self._json({"ok": True, "record_id": rid,
                               "verdict_from": old["verdict"], "photos_moved": moved,
                               "issues": has_issues,
                               "warn": "" if has_issues else
                               "该户问题数据在旧版本中被删除，撤销后无系统问题可恢复"})
        finally:
            con.close()

    def _cleanup(self):
        """一键清理待删除"""
        body = self._body()
        keep = body.get("keep_days", D.load_config().get("pending_keep_days", 30))
        n = 0
        freed = 0
        if os.path.isdir(D.PENDING_DIR):
            cutoff = time.time() - keep * 86400
            for root, dirs, files in os.walk(D.PENDING_DIR, topdown=False):
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        if os.path.getmtime(fp) < cutoff:
                            freed += os.path.getsize(fp)
                            os.remove(fp)
                            n += 1
                    except OSError:
                        pass
                if root != D.PENDING_DIR and not os.listdir(root):
                    try:
                        os.rmdir(root)
                    except OSError:
                        pass
        return self._json({"ok": True, "removed": n, "freed_mb": round(freed / 1048576, 1)})

    # ---------- 报告 ----------
    def _dashboard(self, q):
        """首页仪表盘：各分公司状态一览 + 断点续查信息"""
        month = q.get("month", [""])[0]
        con = D.connect()
        try:
            out = []
            for code, name in COMPANY_ORDER:
                # 该公司所有月合计的进度（断点续查：已判的不再重判）
                tot = con.execute(
                    "SELECT COUNT(*) c FROM checks WHERE company=?", (code,)).fetchone()["c"]
                done = con.execute(
                    "SELECT COUNT(*) c FROM checks WHERE company=? AND status='done'",
                    (code,)).fetchone()["c"]
                recs = con.execute(
                    "SELECT COUNT(DISTINCT record_id) c FROM checks WHERE company=?",
                    (code,)).fetchone()["c"]
                recs_done = con.execute(
                    "SELECT COUNT(DISTINCT record_id) c FROM checks WHERE company=? AND status='done'",
                    (code,)).fetchone()["c"]
                # 2026-10-19：档位一律读有效档位视图（与报告同口径，误判自动剔除）
                sev = {r["eff_severity"]: r["c"] for r in con.execute(
                    "SELECT eff_severity, COUNT(*) c FROM v_issue_eff"
                    " WHERE company=? AND eff_severity IS NOT NULL GROUP BY eff_severity",
                    (code,))}
                # 户级档位：一户一档、critical 优先（与 report_data.company_stats 同口径）。
                # 不能直接 COUNT(DISTINCT record_id) 分档统计——同时命中 critical 与 warning
                # 的户会被两边各算一次，导致 crit_house+warn_house > 问题户总数，
                # 且 ok_house = recs_done - hcrit - hwarn 会把这户重复减掉。
                # 注意用 MIN 不是 MAX：字符串序 'critical' < 'warning'，MIN 才是 critical 优先。
                # 必须 join records 限定 sampled=1——否则未抽样的遗留问题户会被算进首页，
                # 导致 crit_house+warn_house 比报告侧多（实测多 1 户）。
                rows = con.execute(
                    "SELECT v.record_id, MIN(v.eff_severity) s FROM v_issue_eff v"
                    " JOIN records r ON r.id=v.record_id"
                    " WHERE v.company=? AND v.eff_severity IS NOT NULL AND r.sampled=1"
                    " GROUP BY v.record_id", (code,)).fetchall()
                hcrit = sum(1 for r in rows if r["s"] == "critical")
                hwarn = sum(1 for r in rows if r["s"] == "warning")
                mrow = con.execute(
                    "SELECT MAX(month) m FROM checks WHERE company=?", (code,)).fetchone()["m"]
                # 最新一次运行时间
                rtime = con.execute(
                    "SELECT MAX(c.updated_at) t FROM checks c JOIN records r ON r.id=c.record_id"
                    " WHERE c.company=? AND c.status='done'", (code,)).fetchone()["t"]
                if tot == 0:
                    tag, tagcls = "未开始", ""
                elif done >= tot:
                    tag, tagcls = "已完成", "done"
                else:
                    tag, tagcls = f"进行中 {done}/{tot}", "doing"
                out.append({
                    "code": code, "name": name, "month": mrow or "",
                    "total_checks": tot, "done_checks": done,
                    "total_records": recs, "done_records": recs_done,
                    "critical": sev.get("critical", 0), "warning": sev.get("warning", 0),
                    "crit_house": hcrit, "warn_house": hwarn,
                    "ok_house": max(0, recs_done - hcrit - hwarn),
                    "percent": round(done * 100.0 / tot, 1) if tot else 0,
                    "tag": tag, "tagcls": tagcls, "last_time": rtime or "",
                })
            return {"companies": out, "order": [c for c, _ in COMPANY_ORDER]}
        finally:
            con.close()

    def _resume(self, q):
        """每公司独立断点续传状态：告诉你这家上次跑到第几户、下一户该从哪领"""
        con = D.connect()
        try:
            out = []
            for code, name in COMPANY_ORDER:
                row = con.execute(
                    """SELECT COUNT(DISTINCT record_id) c FROM checks WHERE company=?""",
                    (code,)).fetchone()
                total = row["c"] if row else 0
                if not total:
                    out.append({"code": code, "name": name, "state": "未开始",
                                "total": 0, "done": 0, "percent": 0,
                                "next": None, "msg": "尚未导入数据"})
                    continue
                done = con.execute(
                    """SELECT COUNT(DISTINCT record_id) c FROM checks
                       WHERE company=? AND status='done'""",
                    (code,)).fetchone()["c"]
                # 下一户待判（该户还有 status!='done' 的检查项 = 还没提交）
                nxt = con.execute(
                    """SELECT r.id, r.cust_no, r.cust_name, r.inspector
                       FROM records r JOIN checks c ON c.record_id=r.id
                       WHERE c.company=? AND c.status!='done'
                       GROUP BY r.id ORDER BY r.id LIMIT 1""", (code,)).fetchone()
                pct = round(done * 100.0 / total, 1) if total else 0
                if done >= total:
                    state, msg = "已完成", f"{done}/{total} 户已全部判定"
                else:
                    state = "进行中"
                    msg = (f"{done}/{total} 户已判（{pct}%），剩 {total-done} 户"
                           + (f"；下一户：{nxt['cust_no']} {nxt['cust_name']}（{nxt['inspector']}）"
                              if nxt else ""))
                out.append({"code": code, "name": name, "state": state,
                            "total": total, "done": done, "percent": pct,
                            "next": dict(nxt) if nxt else None, "msg": msg})
            return {"companies": out, "order": [c for c, _ in COMPANY_ORDER]}
        finally:
            con.close()

    def _export6(self, q):
        """一次性生成各分公司独立报告（照片内嵌，可直接转发）"""
        import cli
        month = q.get("month", [""])[0]
        embed = q.get("embed", ["0"])[0] != "0"
        files, errs = [], []
        for code, name in COMPANY_ORDER:
            has = D.connect()
            try:
                n = has.execute("SELECT COUNT(*) c FROM checks WHERE company=?",
                                (code,)).fetchone()["c"]
            finally:
                has.close()
            if n == 0:
                errs.append(f"{name}：无数据，跳过")
                continue
            import argparse
            ns = argparse.Namespace(company=code, month=month, embed=embed)
            try:
                r = cli.cmd_report(ns)
                files.append({"company": code, "name": name, "file": r["file"],
                              "rows": r["rows"], "critical": r["critical"],
                              "warning": r["warning"], "sampled": r["sampled"]})
            except Exception as e:
                errs.append(f"{name}：{e}")
        return {"ok": True, "files": files, "skipped": errs,
                "report_dir": D.REPORTS_DIR}

    def _report_data(self, q):
        con = D.connect()
        try:
            company = q.get("company", [""])[0]
            month = q.get("month", [""])[0]
            # issues 条件（带 i. 前缀）
            iw, iargs = "1=1", []
            if company:
                iw += " AND i.company=?"; iargs.append(company)
            if month:
                iw += " AND i.month=?"; iargs.append(month)
            # records 条件（带 r. 前缀，月份由 check_time 派生）
            rw, rargs = "1=1", []
            if company:
                rw += " AND r.company=?"; rargs.append(company)
            if month:
                rw += " AND substr(r.check_time,1,7)=?"; rargs.append(month)

            # 分公司问题汇总
            comp = con.execute(
                f"""SELECT i.company,
                       COUNT(DISTINCT i.record_id) households,
                       SUM(CASE WHEN i.eff_severity='critical' THEN 1 ELSE 0 END) critical,
                       SUM(CASE WHEN i.eff_severity='warning' THEN 1 ELSE 0 END) warning,
                       SUM(CASE WHEN i.eff_severity='info' THEN 1 ELSE 0 END) info
                    FROM v_issue_eff i WHERE {iw} GROUP BY i.company""", iargs).fetchall()
            # 分公司抽查规模
            recs = con.execute(
                f"""SELECT r.company, COUNT(*) c, COUNT(DISTINCT r.inspector) ins
                    FROM records r WHERE {rw} GROUP BY r.company""", rargs).fetchall()
            rec_map = {r["company"]: dict(r) for r in recs}

            # 安检员排行（同一 where 同时约束 records 与 issues）
            insp = con.execute(
                f"""SELECT r.inspector, r.company,
                       COUNT(DISTINCT r.id) households,
                       SUM(CASE WHEN i.severity='critical' THEN 1 ELSE 0 END) critical,
                       SUM(CASE WHEN i.severity='warning' THEN 1 ELSE 0 END) warning
                    FROM records r LEFT JOIN issues i ON i.record_id=r.id
                    WHERE {rw}
                    GROUP BY r.inspector, r.company ORDER BY critical DESC""", rargs).fetchall()

            # 问题类型分布
            kinds = con.execute(
                f"""SELECT kind, eff_severity AS severity, COUNT(*) c FROM v_issue_eff i WHERE {iw}
                    GROUP BY kind, eff_severity ORDER BY c DESC""", iargs).fetchall()
            # 人工复核统计
            rv_w, rv_args = "1=1", []
            if company:
                rv_w += " AND v.company=?"; rv_args.append(company)
            if month:
                rv_w += " AND v.month=?"; rv_args.append(month)
            review = con.execute(
                f"""SELECT v.verdict, COUNT(*) c FROM reviews v WHERE {rv_w}
                    GROUP BY v.verdict""", rv_args).fetchall()
            review_map = {r["verdict"]: r["c"] for r in review}
            return {
                "review": {"false_positive": review_map.get("false_positive", 0),
                           "warning": review_map.get("warning", 0),
                           "critical": review_map.get("critical", 0)},
                "companies": [{
                    "code": r["company"], "name": COMPANY_NAME.get(r["company"], r["company"]),
                    "households": rec_map.get(r["company"], {}).get("c", 0),
                    "inspectors": rec_map.get(r["company"], {}).get("ins", 0),
                    "critical": r["critical"], "warning": r["warning"], "info": r["info"],
                } for r in comp],
                "inspectors": [dict(r) for r in insp],
                "kinds": [dict(r) for r in kinds],
            }
        finally:
            con.close()

    def _report_export(self):
        pass

    # ---------- 新版报告（两级结构）----------
    def _report_overview(self, q):
        """整体简报 + 各分公司卡片数据（报告页首屏用）"""
        import report_data as RD
        month = q.get("month", [""])[0].strip()
        con = D.connect()
        try:
            stats = RD.company_stats(con, month)
            summ = RD.summary(con, stats, month)
            # 2026-10-19 用户定：报告只呈现数据库数字，不渲染整体评语/文字结论段。
            # summary.text 与 company_text 均不再下发（前端也不再显示），
            # 避免每次请求白算一遍文案。
            for d in stats:
                d.pop("text", None)
                d["active"] = d["sampled"] > 0
            summ.pop("text", None)
            return {"month": month, "summary": summ, "companies": stats}
        finally:
            con.close()

    def _report_detail(self, q):
        """单个分公司的问题明细（按户合并，含照片相对路径 + 复核裁决）
        口径与主界面一致（2026-10-16）：误判户从统计中剔除，判严重/提示的按裁决换档；
        需要追溯误判户时用 ?show_fp=1 显示（带「误判已剔除」标签，不计入统计）"""
        import report_data as RD
        month = q.get("month", [""])[0].strip()
        code = (q.get("company", [""])[0] or "").upper()
        if not code:
            return self._json({"rows": [], "msg": "缺少 company"}, 400)
        show_fp = (q.get("show_fp", ["0"])[0] or "").lower() in ("1", "true", "yes")
        rw = "company=?" + (" AND substr(check_time,1,7)=?" if month else "")
        ra = [code] + ([month] if month else [])
        iw = "i.company=?" + (" AND i.month=?" if month else "")
        ia = [code] + ([month] if month else [])
        rw2 = (rw.replace("company=?", "r.company=?")
                  .replace("substr(check_time,1,7)=?", "substr(r.check_time,1,7)=?"))
        # show_fp=1 时把误判户也捞出来（它们的 eff_severity 为 NULL，必须显式放行）
        sev_cond = ("(i.eff_severity IN ('critical','warning') OR v.verdict='false_positive')"
                    if show_fp else "i.eff_severity IN ('critical','warning')")
        con = D.connect()
        try:
            rows = con.execute(
                f"""SELECT i.severity, i.eff_severity, i.kind, i.detail, i.photo_id,
                           r.id AS record_id, r.cust_no, r.cust_name, r.inspector,
                           r.address, r.check_time, r.meter_no, p.local, p.moved_to,
                           v.verdict, v.note, v.note_auto, v.note_manual, v.reviewer
                    FROM v_issue_eff i JOIN records r ON r.id=i.record_id
                    LEFT JOIN photos p ON p.id = COALESCE(i.photo_id,
                         (SELECT ph.id FROM photos ph WHERE ph.record_id=i.record_id
                          ORDER BY CASE ph.items_code WHEN ? THEN 0
                                   WHEN 'overall_path' THEN 2 ELSE 1 END, ph.id LIMIT 1))
                    LEFT JOIN reviews v ON v.record_id=r.id
                    WHERE {iw} AND {sev_cond}
                      AND r.id IN (SELECT r.id FROM records r WHERE {rw2} AND r.sampled=1)
                    ORDER BY CASE i.eff_severity WHEN 'critical' THEN 0 ELSE 1 END,
                             r.cust_no, i.kind""", [D.meter_code(code)] + ia + ra).fetchall()
            by = {}
            for r in rows:
                by.setdefault(r["record_id"], []).append(r)
            rv = RD.review_map(con, code, month)
            out = []
            # 误判户数单独统计：它们被 SQL 排除了，不能靠循环计数
            fp_hidden = 0 if show_fp else sum(1 for v in rv.values() if v == "false_positive")
            for rid, items in by.items():
                fin = rv.get(rid)
                it0 = items[0]
                pics, seen = [], set()
                for i in items:
                    if i["kind"] in RR_NO_PHOTO:
                        continue
                    ph = i["moved_to"] or i["local"] or ""
                    if not ph or ph in seen:
                        continue
                    seen.add(ph)
                    pics.append({"path": ph})
                note = (it0["note"] or "").strip()
                na = (it0["note_auto"] or "").strip()
                nm = (it0["note_manual"] or "").strip()
                vd = it0["verdict"]
                marked = bool(vd and nm)      # 只有人工真写了才算「已填写复核意见」
                if vd == "critical":
                    vh = '<span class="tag2 bc">严重提示</span>'
                elif vd == "warning":
                    vh = '<span class="tag2 bw">一般提示</span>'
                elif vd == "false_positive":
                    vh = '<span class="tag2 bd">误判（已剔除）</span>'
                else:
                    vh = '<span class="tag2 bd">待复核</span>'
                if nm:
                    vh += f'<br><span class="warnnote">{html.escape(nm)}</span>'
                elif note and note != "待复查" and not na:
                    vh += f'<br><span class="warnnote">{html.escape(note)}</span>'
                if na:
                    vh += f'<div class="sub2">{html.escape(na)}</div>'
                if it0["reviewer"]:
                    vh += f'<div class="sub2">复核人 {html.escape(it0["reviewer"])}</div>'
                ilist = [{"kind": i["kind"], "detail": i["detail"],
                          "severity": i["eff_severity"] or i["severity"]} for i in items]
                out.append({
                    "record_id": rid, "cust_no": it0["cust_no"],
                    "cust_name": it0["cust_name"], "inspector": it0["inspector"],
                    "address": it0["address"], "check_time": it0["check_time"],
                    "meter_no": it0["meter_no"],
                    "has_critical": any(x["severity"] == "critical" for x in ilist),
                    "issues": ilist,
                    "photos": pics, "verdict": vd, "verdict_html": vh, "marked": marked,
                })
            return {"company": code, "month": month, "rows": out,
                    "count": len(out), "problems": len(rows),
                    "fp_hidden": fp_hidden, "show_fp": show_fp}
        finally:
            con.close()

    def _report_download(self, q, inline=False):
        """生成报告并返回文件流
        inline=True（/api/report/view）：Content-Disposition 省略，让浏览器直接显示
        → 详情页用 iframe 内嵌它，看到的和下载的完全是同一份
        ?type=summary  整体简报
        ?type=company&code=C5   单个分公司
        """
        import report_data as RD
        import report_render as RR
        month = q.get("month", [""])[0].strip()
        typ = q.get("type", ["summary"])[0]
        code = (q.get("code", [""])[0] or "").upper()
        # 2026-10-19：默认不内嵌（图片用安检系统原始 URL，报告只有几百 KB）；
        # embed=1 才生成离线单文件版（体积大，但没网也能看）
        embed = (q.get("embed", ["0"])[0] or "0") not in ("0", "", "false")
        try:
            if typ == "company" and code:
                con = D.connect()
                try:
                    html = RR.render_company(con, code, month, embed=embed)
                    name = D.COMPANY_NAME.get(code, code)
                finally:
                    con.close()
                fname = self._fname(f"安检抽查报告_{name}_{RR.month_label(month)}")
            else:
                con = D.connect()
                try:
                    stats = RD.company_stats(con, month)
                    html = RR.render_summary(stats, month, embed=embed)
                finally:
                    con.close()
                fname = self._fname(f"安检抽查月度报告_整体简报_{RR.month_label(month)}")
        except Exception as e:
            return self._json({"ok": False, "msg": f"{type(e).__name__}: {e}"}, 500)

        body = html.encode("utf-8")
        extra = {}
        if not inline:
            from urllib.parse import quote
            extra["Content-Disposition"] = (
                f"attachment; filename=\"report.html\"; filename*=UTF-8''{quote(fname)}")
        self._send(200, body, "text/html; charset=utf-8", extra)

    @staticmethod
    def _fname(base):
        return f"{base}_{datetime.date.today():%Y%m%d}.html"


def serve():
    from http.server import ThreadingHTTPServer
    # 每日首次启动自动备份（保留 7 天），避免误操作把库弄坏后无处回滚
    try:
        bk = D.auto_backup(7)
        if bk:
            print("数据库备份:", bk)
    except Exception:
        pass
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"安检抽查系统已启动: http://127.0.0.1:{PORT}")
    print("页面：/ (运行)  /live (大屏看板)  /report (报告)  /settings (设置)")
    httpd.serve_forever()


if __name__ == "__main__":
    serve()
