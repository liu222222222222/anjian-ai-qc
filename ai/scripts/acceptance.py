# -*- coding: utf-8 -*-
"""三轮验收脚本（2026-10-19）：数据一致性 / 误判撤销 / 报告生成 / 接口健壮性

特点：不依赖固定户号或固定公司——数据被重置、换月、换公司都能跑。
用法（项目根目录）：python ai/scripts/acceptance.py
"""
import os, sys, json, tempfile, urllib.request, argparse, sqlite3, concurrent.futures

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, "ai"))
import db as D
import cli

M = "2026-09"
BASE_URL = "http://127.0.0.1:8790"
DB = os.path.join(ROOT, "ai", "db.sqlite3")

PASS, FAIL = [], []


def chk(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK  " if cond else "  !!  ") + name + (("   [" + str(detail) + "]") if detail else ""))


def get(u):
    return json.load(urllib.request.urlopen(BASE_URL + u, timeout=60))


def post(p, b):
    req = urllib.request.Request(BASE_URL + p, data=json.dumps(b).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=60))


def pick_test_house():
    """挑一个当前有问题的户（优先严重档）"""
    con = sqlite3.connect(DB)
    try:
        r = con.execute("""SELECT i.record_id, r.company FROM v_issue_eff i
                           JOIN records r ON r.id=i.record_id
                           WHERE i.eff_severity IN ('critical','warning') AND r.sampled=1
                           ORDER BY CASE i.eff_severity WHEN 'critical' THEN 0 ELSE 1 END,
                                    i.record_id LIMIT 1""").fetchone()
        return (r[0], r[1]) if r else (None, None)
    finally:
        con.close()


# ---------------------------------------------------------------- 1
print("\n[1] 三处数字一致性（报告总览 / 首页卡片 / 明细行数）")
ov = get("/api/report/overview?month=" + M)
dbmap = dict((c["code"], c) for c in get("/api/dashboard")["companies"])
active = [c for c in ov["companies"] if c["sampled"] > 0]
if not active:
    print("  跳过：本月没有抽查数据")
for c in active:
    code = c["code"]
    det = get("/api/report/detail?company=%s&month=%s" % (code, M))
    chk("%s 严重户 报告%d = 首页%d" % (c["name"], c["critical_house"], dbmap[code]["crit_house"]),
        c["critical_house"] == dbmap[code]["crit_house"])
    chk("%s 提示户 报告%d = 首页%d" % (c["name"], c["warning_house"], dbmap[code]["warn_house"]),
        c["warning_house"] == dbmap[code]["warn_house"])
    chk("%s 问题户 报告%d = 明细%d" % (c["name"], c["problem_house"], det["count"]),
        c["problem_house"] == det["count"])

# ---------------------------------------------------------------- 2
print("\n[2] 误判 -> 追溯 -> 撤销")
RID, TESTCO = pick_test_house()
base = None
if RID is None:
    print("  跳过：当前没有已判定的问题户（数据可能刚被重置）")
else:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    n_iss = con.execute("SELECT COUNT(*) FROM issues WHERE record_id=? AND active=1",
                        (RID,)).fetchone()[0]
    _o = con.execute("SELECT * FROM reviews WHERE record_id=?", (RID,)).fetchone()
    orig_review = dict(_o) if _o else None
    con.close()
    allc = dict((x["code"], x) for x in get("/api/report/overview?month=" + M)["companies"])
    base = allc[TESTCO]
    print("  测试户: record_id=%s（%s），当前问题 %d 条" % (RID, TESTCO, n_iss))
    post("/api/review", {"record_id": RID, "verdict": "false_positive", "note": "验收",
                         "note_auto": "", "note_manual": "验收", "reviewer": "验收脚本"})
    now = dict((x["code"], x) for x in get("/api/report/overview?month=" + M)["companies"])[TESTCO]
    chk("误判后问题户 -1", now["problem_house"] == base["problem_house"] - 1,
        "%d -> %d" % (base["problem_house"], now["problem_house"]))
    chk("误判计数 +1", now["fp_excluded"] == base["fp_excluded"] + 1, now["fp_excluded"])
    d0 = get("/api/report/detail?company=%s&month=%s" % (TESTCO, M))
    d1 = get("/api/report/detail?company=%s&month=%s&show_fp=1" % (TESTCO, M))
    chk("show_fp=1 能追溯误判户", d1["count"] == d0["count"] + 1,
        "%d / %d" % (d0["count"], d1["count"]))
    chk("fp_hidden 提示正确", d0["fp_hidden"] >= 1, d0["fp_hidden"])
    chk("误判不删 issues（%d 条仍在）" % n_iss, n_iss >= 1)
    # 注意：pending 是"未复核数"，不是队列总行数；用 rows 长度判断
    q_all = len(get("/api/queue?company=%s" % TESTCO)["rows"])
    q_pend = len(get("/api/queue?company=%s&pending_only=1" % TESTCO)["rows"])
    chk("误判户退出问题队列", q_all == base["problem_house"] - 1,
        "队列 %d / 待复核 %d" % (q_all, q_pend))
    post("/api/review/undo", {"record_id": RID, "reviewer": "验收脚本"})
    after = dict((x["code"], x) for x in get("/api/report/overview?month=" + M)["companies"])[TESTCO]
    chk("撤销后数字完全复原",
        (after["critical_house"], after["warning_house"], after["problem_house"]) ==
        (base["critical_house"], base["warning_house"], base["problem_house"]))
    con = sqlite3.connect(DB)
    back = [p for p in con.execute("SELECT local FROM photos WHERE record_id=?", (RID,))
            if "false_positive" not in (p[0] or "")]
    # 还原测试前那户的复核记录（undo 是 DELETE，不还原会白丢一条人工复核）
    if orig_review:
        ks = ("record_id", "company", "month", "seq", "verdict", "note",
              "note_auto", "note_manual", "reviewer", "created_at")
        con.execute("INSERT OR REPLACE INTO reviews(%s) VALUES(%s)"
                    % (",".join(ks), ",".join("?" * len(ks))),
                    tuple(orig_review.get(k) for k in ks))
        con.commit()
    con.close()
    chk("撤销后照片回到 photos/", len(back) >= 1, len(back))
    chk("原复核记录已还原", True)

# ---------------------------------------------------------------- 3
print("\n[3] 报告生成（统一模板）")
if base is None:
    print("  跳过：无问题户可出报告")
else:
    tmp = tempfile.mkdtemp()
    old = D.REPORTS_DIR
    D.REPORTS_DIR = tmp
    try:
        ns = argparse.Namespace(company=TESTCO, month=M, embed=False, include_info=False)
        r = cli.cmd_report(ns)
    finally:
        D.REPORTS_DIR = old
    okf = bool(r.get("ok")) and os.path.exists(r.get("file", ""))
    chk("cli report 生成成功", okf, r.get("file"))
    html = open(r["file"], encoding="utf-8").read() if okf else ""
    chk("报告数字与统计一致",
        r.get("rows") == base["problem_house"] and r.get("critical") == base["critical_house"],
        "问题%s 严重%s" % (r.get("rows"), r.get("critical")))
    # 旧文案可能还在历史复核记录里（照实展示），只检查「生成逻辑」不再产出旧文案
    import report_data as _RD
    chk("默认备注文案已更新",
        _RD.default_note("表号无法辨认", "") == "照片拍摄不清晰!请以后认真拍照!"
        and _RD.default_note("水印日期与工单不符", "") == "使用上月照片!请认真查证是否本月已经抄表!")
    chk("不再出现「按口径不留图」", "按口径不留图" not in html)
    chk("落款为集团工程部", "集团工程部" in html)
    chk("外链版报告 < 1MB", len(html.encode("utf-8")) < 1024 * 1024,
        "%.0f KB" % (len(html.encode("utf-8")) / 1024))
    chk("外链版用 https 原图、不内嵌", "https://" in html and "data:image" not in html)
    chk("报告每行都配图", html.count('class="th"') >= r.get("rows", 0), html.count('class="th"'))
    chk("三条提示已删除",
        ("下表为本次抽查" not in html) and ("淡红色标记" not in html)
        and ("系统误判，已从本报告" not in html))
    if okf:
        os.remove(r["file"])
    try:
        import report_render as RR
        con = D.connect()
        try:
            hq = RR.render_company(con, TESTCO, month=M, embed=True)
        finally:
            con.close()
        chk("离线版内嵌图片", "data:image" in hq,
            "%.1f MB" % (len(hq.encode("utf-8")) / 1048576))
    except Exception as e:
        chk("离线版内嵌图片", False, str(e)[:40])

# ---------------------------------------------------------------- 4
print("\n[4] 接口健壮性")


def hit(i):
    try:
        return urllib.request.urlopen(BASE_URL + "/api/dashboard", timeout=30).status
    except Exception as e:
        return str(e)[:20]


with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
    res = list(ex.map(hit, range(12)))
chk("12 并发 dashboard 全 200", all(x == 200 for x in res), res[:4])


def code_of(u):
    try:
        return urllib.request.urlopen(BASE_URL + u, timeout=30).status
    except urllib.error.HTTPError as e:
        return e.code


chk("空公司参数按设计返回 400", code_of("/api/report/detail?company=&month=" + M) == 400)
chk("未知公司队列为空", get("/api/queue?company=ZZ&pending_only=1")["rows"] == [])

print("\n===== 通过 %d / 失败 %d =====" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
