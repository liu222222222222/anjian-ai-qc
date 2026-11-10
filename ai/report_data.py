"""报告数据层：分公司汇总 + 抽查总规模
指标口径（用户 2026-10-15 明确）：
  当月入户安检总数 / 抽查数量 / 抽查比例
  已复核户数
  提示性问题户数 / 严重问题户数← 都是「户」，不是「条」
"""
import os, re, datetime
import db as D

TIME_KINDS = ("水印日期与工单不符", "照片时间早于安检工单时间", "同一户照片拍摄时间不一致")
# 用户 2026-10-16 定：168 小时一刀切，时间类问题只有 critical 一档
GAP_CRITICAL_MIN = D.TIME_GAP_MIN


def gap_minutes(detail):
    """从 detail 自然语言里抽时间差（分钟）；抽不出返回 None
    detail 表述很多种，必须逐个覆盖：
      「早于工单约3天4小时」「相差约26小时」「早于工单约1.6天」
      「早于工单约35分钟」「早于工单仅12秒」「晚于工单35秒」
    """
    d = detail or ""
    m = re.search(r"([\d.]+)\s*天\s*([\d.]+)?\s*小时?", d)
    if m:
        g = float(m.group(1)) * 1440
        if m.group(2):
            g += float(m.group(2)) * 60
        return g
    m = re.search(r"相差约\s*([\d.]+)\s*(?:个)?\s*小时", d)
    if m:
        return float(m.group(1)) * 60
    m = re.search(r"(?:早|晚)于工单(?:登记时间)?约?\s*([\d.]+)\s*(?:分钟|分)", d)
    if m:
        return float(m.group(1))
    m = re.search(r"(?:早|晚)于工单(?:登记时间)?(?:约|仅)?\s*([\d.]+)\s*秒", d)
    if m:
        return float(m.group(1)) / 60
    m = re.search(r"([\d.]+)\s*天", d)
    if m:
        return float(m.group(1)) * 1440
    m = re.search(r"([\d.]+)\s*(?:个)?\s*小时", d)
    if m:
        return float(m.group(1)) * 60
    return None


# 自动识别段文案 —— 与 static/review.html 的 TAGS 常量保持同一套（2026-10-19 对齐）
NOTE_TIME = "使用上月照片!请认真查证是否本月已经抄表!"
NOTE_CLARITY = "照片拍摄不清晰!请以后认真拍照!"
NOTE_GENERAL = "请相关人员复查"


def default_note(kind, detail):
    """按问题类型给复核页自动填默认备注（复核页已改前端算，此函数留作后端兜底）"""
    if (kind or "") in TIME_KINDS:
        return NOTE_TIME
    if re.search(r"无法辨认|模糊|不清晰", kind or ""):
        return NOTE_CLARITY
    return NOTE_GENERAL


def _scope(con, company, month):
    """返回 (记录WHERE, 记录参数, 问题WHERE, 问题参数)"""
    rw, ra = "company=?", [company]
    if month:
        rw += " AND substr(check_time,1,7)=?"
        ra.append(month)
    iw = "company=?"
    ia = [company]
    if month:
        iw += " AND month=?"
        ia.append(month)
    return rw, ra, iw, ia


def review_map(con, company, month):
    """{record_id: verdict} 人工复核最终裁决（2026-10-16 用户定：复核后报表按裁决重算）"""
    w, a = "company=?", [company]
    if month:
        w += " AND month=?"
        a.append(month)
    return {r["record_id"]: r["verdict"] for r in
            con.execute(f"SELECT record_id, verdict FROM reviews WHERE {w}", a)
            # verdict 为空 = 只有公司反馈导入、集团还没审这一户，不能算「已审核」
            if (r["verdict"] or "").strip()}


def eff_severity(rv, rid, orig):
    """复核后的有效档位：None=误判剔除（不算问题户），否则 critical/warning
    口径：误判→剔除；判严重→升级；判提示→降级；未复核→沿用系统判定"""
    v = rv.get(rid)
    if v == "false_positive":
        return None
    if v in ("critical", "warning"):
        return v
    return orig


def company_stats(con, month=None, company=None):
    """每家分公司的核心指标 + 明细统计（已按人工复核裁决重算）"""
    out = []
    codes = D.COMPANY_ORDER
    for code, name in codes:
        if company and code != company.upper():
            continue
        rw, ra, iw, ia = _scope(con, code, month)
        rw2 = (rw.replace("company=?", "r.company=?")
                  .replace("AND company=?", "AND r.company=?")
                  .replace("substr(check_time,1,7)=?", "substr(r.check_time,1,7)=?"))
        rec = "SELECT id FROM records WHERE " + rw + " AND sampled=1"
        rv = review_map(con, code, month)

        total = con.execute(f"SELECT COUNT(*) n FROM records WHERE {rw}",
                            ra).fetchone()["n"]
        sampled = con.execute(f"SELECT COUNT(*) n FROM records WHERE {rw} AND sampled=1",
                              ra).fetchone()["n"]
        # 2026-11-07 新增：已判定户数。抽样不等于查完（E 公司 10 月抽 653 户只判 8 户），
        # 报告若只给 sampled，会把「刚抽完未判」显示成「已查完」。
        cw, ca = "company=?", [code]
        if month:
            cw += " AND month=?"; ca.append(month)
        done_house = con.execute(
            f"SELECT COUNT(DISTINCT record_id) n FROM checks WHERE {cw} AND status='done'",
            ca).fetchone()["n"]
        # 2026-10-22 用户定：没有表具照片的户不进抽样池，也不算「可查户」。
        # 报告要同时给出「在册入户安检」与「其中有表具照片可查」两个数，
        # 覆盖率以「可查户」为分母 —— 拿 61,096 当分母会被质疑抽查率不达标。
        # checkable_cond = 民用 + 有表具照片，与抽样池完全同口径。
        ccond, cargs = D.checkable_cond(code)
        scoped = (rw.replace("company=?", "r.company=?")
                     .replace("substr(check_time,1,7)=?", "substr(r.check_time,1,7)=?"))
        checkable = con.execute(
            f"SELECT COUNT(*) n FROM records r WHERE {scoped} AND {ccond}",
            ra + cargs).fetchone()["n"]
        insp = con.execute(f"SELECT COUNT(DISTINCT inspector) n FROM records WHERE {rw}",
                           ra).fetchone()["n"]

        # 户级档位（复核后）
        # 2026-10-19：统一读有效档位视图（误判→NULL 自动剔除，人工定档已覆盖系统档位）
        prow = con.execute(
            f"""SELECT DISTINCT record_id, eff_severity AS severity FROM v_issue_eff
                WHERE {iw} AND eff_severity IN ('critical','warning')
                  AND record_id IN ({rec})""", ia + ra).fetchall()
        # 一户一档、critical 优先：同一户若同时有 critical 与 warning 两条 issue，
        # 只计入 critical，否则 crit_house + warn_house 会超过 problem_house
        # （D 公司 48450 即此情况：文档外墙照片 critical + 表号无法辨认 warning）
        by_rec = {}
        for r in prow:
            e = eff_severity(rv, r["record_id"], r["severity"])
            if not e:
                continue
            rid_ = r["record_id"]
            # critical 覆盖 warning；warning 只在第一次见到该户时写入
            if e == "critical" or rid_ not in by_rec:
                by_rec[rid_] = e
        crit_ids = {k for k, v in by_rec.items() if v == "critical"}
        warn_ids = {k for k, v in by_rec.items() if v == "warning"}
        problem_ids = crit_ids | warn_ids
        # 复核判为误判的户：系统保存复核时已把 issues 删除（server._save_review），
        # 这里只统计户数用于文案说明，不再重复剔除
        fp_excluded = sum(1 for v in rv.values() if v == "false_positive")

        reviewed_ids = {rid for rid in problem_ids if rid in rv}
        # 问题类型（复核后条数）
        agg = {}
        for r in con.execute(
                f"""SELECT record_id, kind, eff_severity AS severity FROM v_issue_eff
                    WHERE {iw} AND eff_severity IN ('critical','warning')
                      AND record_id IN ({rec})""", ia + ra):
            e = eff_severity(rv, r["record_id"], r["severity"])
            if not e:
                continue
            agg[(r["kind"], e)] = agg.get((r["kind"], e), 0) + 1
        kinds = [{"kind": k, "severity": s, "n": n}
                 for (k, s), n in sorted(agg.items(), key=lambda x: -x[1])]

        # 安检员维度（按复核后档位统计户数）
        house_sev = {}
        for r in con.execute(
                f"""SELECT DISTINCT i.record_id AS rid, i.eff_severity AS severity
                    FROM v_issue_eff i WHERE {iw} AND i.eff_severity IN ('critical','warning')
                      AND i.record_id IN ({rec})""", ia + ra):
            e = eff_severity(rv, r["rid"], r["severity"])
            if e:
                house_sev[r["rid"]] = e
        per = {}
        for r in con.execute(
                f"SELECT id, inspector FROM records WHERE {rw} AND sampled=1", ra):
            d = per.setdefault(r["inspector"] or "—",
                               {"households": 0, "crit": set(), "warn": set()})
            d["households"] += 1
            e = house_sev.get(r["id"])
            if e == "critical":
                d["crit"].add(r["id"])
            elif e == "warning":
                d["warn"].add(r["id"])
        inspectors = [{"inspector": k, "households": v["households"],
                       "crit": len(v["crit"]), "warn": len(v["warn"])}
                      for k, v in per.items()]
        inspectors.sort(key=lambda x: (-x["crit"], -x["warn"], -x["households"]))
        out.append({
            "code": code, "name": name,
            "total_house": total, "checkable_house": checkable, "sampled": sampled,
            "done_house": done_house,
            # 覆盖率分母用「有表具照片可查」的户数（2026-10-22 用户定）
            "rate": round(sampled * 100.0 / checkable, 1) if checkable else 0,
            "inspectors": insp,
            "critical_house": len(crit_ids),
            "warning_house": len(warn_ids),
            "problem_house": len(problem_ids),
            "fp_excluded": fp_excluded,
            "reviewed": len(reviewed_ids) + fp_excluded,
            "pending_review": max(0, len(problem_ids) - len(reviewed_ids)),
            "kinds": kinds,
            "inspector_rows": inspectors,
        })
    return out


def summary(con, stats, month=None):
    """总汇总 + 结论文字"""
    total_house = sum(d["total_house"] for d in stats)
    checkable_house = sum(d.get("checkable_house", d["total_house"]) for d in stats)
    sampled = sum(d["sampled"] for d in stats)
    done_house = sum(d.get("done_house", d["sampled"]) for d in stats)
    reviewed = sum(d["reviewed"] for d in stats)
    crit_house = sum(d["critical_house"] for d in stats)
    warn_house = sum(d["warning_house"] for d in stats)
    crit_n = sum(k["n"] for d in stats for k in d["kinds"] if k["severity"] == "critical")
    warn_n = sum(k["n"] for d in stats for k in d["kinds"] if k["severity"] == "warning")
    # 有问题的户（跨公司去重，复核后口径 + sampled 过滤，与各公司卡片保持一致）
    rv_all = {}
    for d in stats:
        rv_all.update(review_map(con, d["code"], month))
    ph = set()
    for d in stats:
        rw, ra, iw, ia = _scope(con, d["code"], month)
        for r in con.execute(
                f"""SELECT DISTINCT i.record_id AS rid, i.eff_severity AS severity
                    FROM v_issue_eff i
                    WHERE {iw} AND i.eff_severity IN ('critical','warning')
                      AND i.record_id IN
                          (SELECT id FROM records WHERE {rw} AND sampled=1)""", ia + ra):
            e = eff_severity(rv_all, r["rid"], r["severity"])
            if e:
                ph.add(r["rid"])
    problem_house = len(ph)
    reviewed_n = len({rid for rid in ph if rid in rv_all})
    pending = max(0, problem_house - reviewed_n)
    # 判误判的户：issues 保存复核时已删除，这里只统计户数用于文案
    fp_excluded = len({rid for rid, v in rv_all.items() if v == "false_positive"})

    active = [d for d in stats if d["sampled"] > 0]
    # 覆盖率分母用「有表具照片可查」的户数（2026-10-22 用户定）
    rate = round(sampled * 100.0 / checkable_house, 1) if checkable_house else 0
    ok_rate = round((1 - problem_house / sampled) * 100, 1) if sampled else 100.0
    p_rate = round(problem_house * 100.0 / sampled, 1) if sampled else 0
    # 人工审核比例 = 已审核户数 ÷ 抽查户数（2026-10-22 用户定，报告首页要显示）
    review_rate = round((reviewed_n + fp_excluded) * 100.0 / sampled, 1) if sampled else 0

    m = (month.replace("-", "年") + "月") if month else "本月"
    fp_txt = (f"经人工逐张复核、确认误判剔除 {fp_excluded} 户，" if fp_excluded else "")
    text = (
        f"{m}，公司在册入户安检 {total_house:,} 户"
        f"（其中有表具照片、可供抽查的 {checkable_house:,} 户）。"
        f"按每名安检员 10% 分层抽样，本次抽查 {sampled:,} 户，覆盖率 {rate}%。"
        f"抽查中发现问题 {problem_house + fp_excluded} 户，{fp_txt}"
        f"计入严重问题 {crit_house} 户、提示性问题 {warn_house} 户，"
        f"占抽查户数 {p_rate}%，合计 {crit_n + warn_n} 条问题记录。"
        f"已人工复核 {reviewed_n + fp_excluded} 户"
        + (f"（其中 {fp_excluded} 户经复核确认误判，已从统计中剔除）" if fp_excluded else "")
        + f"，尚有 {pending} 户待复核。"
        f"本次抽查总体符合率 {ok_rate}%。"
    )
    if problem_house == 0 and sampled:
        text += "本次抽查未发现问题。"
    return {
        "month": month, "total_house": total_house,
        "checkable_house": checkable_house, "sampled": sampled, "rate": rate,
        "done_house": done_house,
        "reviewed": reviewed_n + fp_excluded, "pending": pending,
        "fp_excluded": fp_excluded,
        "checked": reviewed_n + fp_excluded, "review_rate": review_rate,
        "critical_house": crit_house, "warning_house": warn_house,
        "problem_house": problem_house, "critical_n": crit_n, "warning_n": warn_n,
        "ok_rate": ok_rate, "company_count": len(active),
        "problem_rate": p_rate,
        "text": text,
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def company_text(d, month=None):
    """单个分公司的几句话总结"""
    m = (month.replace("-", "年") + "月") if month else "本月"
    if d["sampled"] == 0:
        return f"{d['name']}：本月未开展抽查。"
    ok = round((1 - d["problem_house"] / d["sampled"]) * 100, 1) if d["sampled"] else 100.0
    parts = [
        f"{d['name']}：{m}入户安检 {d['total_house']:,} 户"
        f"（其中有表具照片可查 {d.get('checkable_house', d['total_house']):,} 户），"
        f"抽查 {d['sampled']} 户（覆盖率 {d['rate']}%），涉及安检员 {d['inspectors']} 人。"
    ]
    if d["problem_house"] == 0:
        parts.append("本次抽查未发现问题，符合率 100%。")
    else:
        fp = d.get("fp_excluded", 0)
        fp_txt = (f"其中 {fp} 户经复核确认为误判，已从统计中剔除。"
                  if fp else "")
        parts.append(
            f"发现严重问题 {d['critical_house']} 户、提示性问题 {d['warning_house']} 户，"
            f"合计 {d['problem_house']} 户存在问题，抽查符合率 {ok}%。"
            f"已复核 {d['reviewed']} 户，待复核 {d['pending_review']} 户。{fp_txt}"
        )
    # 点出最主要的问题类型
    kinds = [k for k in d["kinds"] if k["n"] > 0]
    if kinds:
        top = kinds[0]
        parts.append(f"最主要问题为「{top['kind']}」{top['n']} 条。")
    # 问题最集中的安检员
    bad = [i for i in d["inspector_rows"] if i["crit"]]
    if bad:
        top = bad[0]
        parts.append(f"其中 {top['inspector']} 涉及严重问题 {top['crit']} 户（抽查 {top['households']} 户），需重点核实。")
    return "".join(parts)


def verdict_label(v):
    return {"false_positive": "误判", "warning": "一般提示",
            "critical": "严重提示"}.get(v, "")
