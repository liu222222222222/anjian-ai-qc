# -*- coding: utf-8 -*-
"""专项任务报告渲染（外链版 HTML）

2026-11-10 改版：
  * 每户只显示一张图（优先问题图），点开浮窗看全部，带左右箭头 + 缩略图条
  * 筛选：安检员下拉 + 档位多选（严重/一般/正常/剔除）
  * 分组排序：严重 → 一般 → 正常 → 剔除，后两组默认折叠
  * 三段判定并列：AI 判别 / 第一次人工复核（月度）/ 第二次人工复核（本次专项）
  * 顶部：月度 vs 专项对比表（抽样户、合格率、三个问题率）
"""
import os, json, html, datetime

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f4f6f8;color:#1f2733;
 font:14px/1.6 "Microsoft YaHei","PingFang SC",sans-serif}
.wrap{max-width:1520px;margin:0 auto;padding:26px 26px 80px}
h1{font-size:24px;margin:0 0 6px;color:#0d1520}
.sub{color:#6b7684;font-size:13px;margin:0 0 18px}
.card{background:#fff;border:1px solid #e3e8ee;border-radius:10px;padding:16px 18px;margin-bottom:16px}
.card h2{font-size:14px;margin:0 0 12px;color:#0d1520;font-weight:700}
.card h2 span{font-weight:400;color:#8b95a3;font-size:12px;margin-left:8px}
.kpis{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin-bottom:16px}
.kpi{background:#fff;border:1px solid #e3e8ee;border-radius:10px;padding:14px 16px}
.kpi .n{font-size:25px;font-weight:700;color:#0d1520;line-height:1.2}
.kpi .l{font-size:12px;color:#6b7684;margin-top:3px}
.kpi .d{font-size:11.5px;color:#8b95a3;margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{background:#f8fafc;color:#6b7684;font-size:12px;font-weight:600;text-align:left;
 padding:8px 9px;border-bottom:1px solid #e3e8ee;white-space:nowrap}
td{padding:8px 9px;border-bottom:1px solid #eef2f6;white-space:nowrap}
.num{text-align:right;font-variant-numeric:tabular-nums}
.low{color:#d4380d}.mid{color:#b7791f}.hi{color:#237804}
.toolbar{display:flex;gap:10px;align-items:center;margin:20px 0 12px;flex-wrap:wrap;
 background:#fff;border:1px solid #e3e8ee;border-radius:10px;padding:12px 14px}
select{padding:7px 11px;border:1px solid #e3e8ee;border-radius:7px;font-size:13.5px;
 font-family:inherit;background:#fff}
.chk{display:inline-flex;align-items:center;gap:6px;padding:5px 11px;border-radius:999px;
 border:1px solid #e3e8ee;cursor:pointer;font-size:13px;user-select:none;background:#fff}
.chk input{margin:0;cursor:pointer}
.chk.on{border-color:#1668dc;background:#f0f6ff;color:#1668dc;font-weight:600}
.chk.e-cr.on{border-color:#d4380d;background:#fff1ee;color:#d4380d}
.chk.e-wa.on{border-color:#b7791f;background:#fffbe6;color:#b7791f}
.chk.e-no.on{border-color:#237804;background:#f6ffed;color:#237804}
.chk.e-fp.on{border-color:#8b95a3;background:#f4f6f8;color:#5b6472}
.hint{font-size:12.5px;color:#6b7684}
.btn{padding:7px 13px;border:1px solid #e3e8ee;border-radius:7px;background:#fff;cursor:pointer;
 font-size:13px;font-family:inherit;color:#1f2733}
.btn:hover{border-color:#1668dc;color:#1668dc}
details.grp{background:#fff;border:1px solid #e3e8ee;border-radius:10px;margin-bottom:14px;overflow:hidden}
details.grp>summary{cursor:pointer;padding:13px 16px;font-weight:700;font-size:14px;
 list-style:none;display:flex;align-items:center;gap:9px;user-select:none}
details.grp>summary::-webkit-details-marker{display:none}
details.grp>summary:before{content:"▸";color:#8b95a3;font-size:12px;transition:.15s}
details.grp[open]>summary:before{transform:rotate(90deg)}
details.grp>summary .cnt{font-weight:400;color:#8b95a3;font-size:12.5px}
.g-cr>summary{border-left:3px solid #d4380d}
.g-wa>summary{border-left:3px solid #e8c46a}
.g-no>summary{border-left:3px solid #52c41a}
.g-fp>summary{border-left:3px solid #8b95a3}
.grpbody{padding:0 14px 6px}
.house{background:#fff;border:1px solid #e3e8ee;border-radius:9px;margin-bottom:11px;overflow:hidden}
.house.cr{border-left:3px solid #d4380d}
.house.wa{border-left:3px solid #e8c46a}
.house.fp{opacity:.72}
.hd{display:flex;gap:9px;align-items:center;flex-wrap:wrap;padding:9px 13px;
 border-bottom:1px solid #eef2f6;font-size:13px;background:#fbfcfd}
.hd .cn{font-weight:700;font-size:14.5px;color:#0d1520;font-family:Consolas,monospace}
.hd .m{color:#6b7684;font-size:12.5px}
.hd .m b{color:#1f2733}
.badge{margin-left:auto;padding:2px 9px;border-radius:999px;font-size:11.5px;font-weight:600}
.badge.cr{background:#fff1ee;color:#d4380d;border:1px solid #ffc9bd}
.badge.wa{background:#fffbe6;color:#b7791f;border:1px solid #ffe58f}
.badge.ok{background:#f6ffed;color:#237804;border:1px solid #b7eb8f}
.badge.fp{background:#f4f6f8;color:#5b6472;border:1px solid #d3d9e0}
.body{display:flex;gap:0;align-items:stretch}
.thumb{flex:0 0 300px;position:relative;cursor:zoom-in;background:#0d1520}
.thumb img{width:300px;height:200px;object-fit:cover;display:block}
.thumb .more{position:absolute;right:0;bottom:0;background:rgba(13,21,32,.78);color:#fff;
 font-size:11.5px;padding:3px 9px;border-top-left-radius:8px}
.thumb .flag{position:absolute;left:0;top:0;background:rgba(212,56,13,.92);color:#fff;
 font-size:11px;padding:2px 8px;border-bottom-right-radius:8px;max-width:290px;
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.thumb.nopic{cursor:default;display:flex;align-items:center;justify-content:center;
 background:#f4f6f8;color:#9aa5b1;font-size:12.5px;width:300px;height:200px}
.side{flex:1;padding:11px 14px;min-width:0}
.blk{margin-bottom:8px;padding:8px 11px;border-radius:7px;font-size:12.8px;line-height:1.65}
.blk:last-child{margin-bottom:0}
.blk .t{font-size:11.5px;font-weight:700;letter-spacing:.4px;margin-bottom:3px;opacity:.85}
.blk-ai{background:#f5f8fc;border:1px solid #e3ebf5}
.blk-ai .t{color:#1668dc}
.blk-rv1{background:#fffaf0;border:1px solid #ffe9c2}
.blk-rv1 .t{color:#b7791f}
.blk-rv2{background:#f6f0ff;border:1px solid #e4d4ff}
.blk-rv2 .t{color:#7048c4}
.blk-none{background:#f8fafc;border:1px solid #eef2f6;color:#8b95a3}
.dts{color:#6b7684;font-size:12px;margin-top:4px;line-height:1.7}
.dts div{margin-top:2px}
/* 浮窗 */
.lb{position:fixed;inset:0;background:rgba(8,12,18,.94);z-index:99;display:none;
 flex-direction:column;padding:18px}
.lb.on{display:flex}
.lbtop{display:flex;align-items:center;gap:12px;color:#dfe6ee;font-size:13.5px;padding:0 4px 10px}
.lbtop .x{margin-left:auto;cursor:pointer;font-size:22px;line-height:1;padding:2px 8px;color:#aab4c0}
.lbtop .x:hover{color:#fff}
.lbmain{flex:1;position:relative;display:flex;align-items:center;justify-content:center;min-height:0}
.lbmain img{max-width:100%;max-height:100%;object-fit:contain;border-radius:6px}
.lbmain .nav{position:absolute;top:50%;transform:translateY(-50%);background:rgba(35,46,60,.85);
 color:#fff;border:0;width:46px;height:70px;border-radius:8px;cursor:pointer;font-size:22px}
.lbmain .nav:hover{background:rgba(22,104,220,.9)}
.lbmain .prev{left:8px}.lbmain .next{right:8px}
.lbmain .nav:disabled{opacity:.25;cursor:default}
.lbstrip{display:flex;gap:7px;justify-content:center;padding:12px 0 4px;overflow-x:auto}
.lbstrip img{width:88px;height:60px;object-fit:cover;border-radius:5px;cursor:pointer;
 border:2px solid transparent;opacity:.55;flex:none}
.lbstrip img.on{border-color:#1668dc;opacity:1}
.lbcap{text-align:center;color:#cdd6e0;font-size:12.5px;padding-top:6px}
.foot{text-align:center;color:#9aa5b1;font-size:12px;margin-top:30px}
@media print{.toolbar,.lb{display:none!important}details.grp{page-break-inside:avoid}
 body{background:#fff}.house{page-break-inside:avoid}}
"""


def _esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def _src(p):
    """照片地址：优先原始外链（换机器也能看），否则用本地服务地址"""
    u = (p.get("url") or "").strip()
    if u.startswith("http"):
        return u
    path = (p.get("path") or "").strip()
    if not path:
        return ""
    if path.startswith("http"):
        return path
    return "http://127.0.0.1:8790/" + path.lstrip("/")


def _cls(sev):
    return {"critical": "cr", "warning": "wa", "none": "ok"}.get(sev, "wa")


def _sev_txt(sev):
    return {"critical": "严重提示", "warning": "一般提示", "none": "正常",
            "false_positive": "剔除", "pending": "待判定"}.get(sev, "—")


def _rate_cls(v):
    if v is None:
        return ""
    return "low" if v < 60 else ("mid" if v < 80 else "hi")


def _pick_thumb(h):
    """挑一张代表图：优先挂了 critical/warning 的判定用图，其次第一张判定用图"""
    ph = h.get("photos") or []
    judge = [p for p in ph if not p.get("ext_only") and not p.get("empty")]
    if not judge:
        judge = [p for p in ph if not p.get("empty")]
    if not judge:
        return None
    bad_codes = {i.get("items_code") for i in (h.get("issues") or [])
                 if i.get("severity") in ("critical", "warning")}
    for p in judge:
        if p.get("items_code") in bad_codes:
            return p
    return judge[0]


def _blocks(h):
    """三段判定：AI / 第一次人工复核 / 第二次人工复核"""
    issues = h.get("issues") or []
    kinds, details = [], []
    for i in issues:
        k = i.get("kind") or ""
        if k and k not in kinds:
            kinds.append(k)
        if i.get("detail"):
            details.append("<div>%s：%s</div>" % (_esc(i.get("kind")), _esc(i["detail"])))

    ai_v = h.get("ai_verdict") or "pending"
    out = ['<div class="blk blk-ai"><div class="t">AI 判别 · %s</div>' % _esc(_sev_txt(ai_v))]
    if kinds:
        out.append("<div>%s</div>" % _esc("、".join(kinds)))
    else:
        out.append('<div style="color:#8b95a3">未发现问题</div>')
    if details:
        out.append('<div class="dts">%s</div>' % "".join(details))
    out.append("</div>")

    rv1 = h.get("rv1") or ""
    if rv1:
        out.append('<div class="blk blk-rv1"><div class="t">第一次人工复核（月度报告） · %s</div>%s</div>'
                   % (_esc(_sev_txt(rv1)),
                      _esc(h.get("rv1_note") or "（未写意见）")))
    rv2 = h.get("review") or ""
    note2 = (h.get("note") or "").strip()
    if rv2:
        out.append('<div class="blk blk-rv2"><div class="t">第二次人工复核（本次专项） · %s</div>%s</div>'
                   % (_esc(_sev_txt(rv2)), _esc(note2 or "（未写意见）")))
    if not rv1 and not rv2:
        out.append('<div class="blk blk-none">尚未人工复核</div>')
    return "".join(out)


def _house(h, seq):
    ph = h.get("photos") or []
    th = _pick_thumb(h)
    eff = h.get("review") or h.get("eff") or "none"
    cls = "fp" if h.get("review") == "false_positive" else (
        "cr" if h.get("eff") == "critical" else ("wa" if h.get("eff") == "warning" else ""))
    badge = ('<span class="badge cr">严重提示</span>' if eff == "critical"
             else '<span class="badge wa">一般提示</span>' if eff == "warning"
             else '<span class="badge fp">已剔除</span>' if eff == "false_positive"
             else '<span class="badge ok">正常</span>')

    if th:
        src = _src(th)
        flag = ""
        for i in (h.get("issues") or []):
            if i.get("items_code") == th.get("items_code"):
                flag = '<div class="flag">%s</div>' % _esc(i.get("kind") or "")
                break
        thumb = ('<div class="thumb" data-rid="%s" data-i="0" onclick="lbOpen(this)">'
                 '<img src="%s" loading="lazy" alt=""><div class="more">共 %d 张 · 点开看全部</div>%s</div>'
                 % (h["rid"], _esc(src), len(ph), flag))
    else:
        thumb = '<div class="thumb nopic">无照片</div>'

    return """<div class="house %(cls)s" data-insp="%(insp)s" data-eff="%(eff)s" data-rid="%(rid)s">
  <div class="hd">
    <span class="cn">%(cust)s</span><span>%(name)s</span>
    <span class="m">安检员 <b>%(insp)s</b></span>
    <span class="m">%(ctime)s</span>
    <span class="m">档案表号 %(meter)s</span>
    %(badge)s
  </div>
  <div class="body">%(thumb)s<div class="side">%(blocks)s</div></div>
</div>""" % dict(cls=cls, insp=_esc(h.get("inspector")), rid=h["rid"],
                eff=_esc(eff), cust=_esc(h.get("cust_no")), name=_esc(h.get("cust_name") or ""),
                ctime=_esc(h.get("check_time") or ""), meter=_esc(h.get("meter_no") or "—"),
                badge=badge, thumb=thumb, blocks=_blocks(h))


def build(summary, houses, out_path):
    t = summary["task"]
    tt = summary["totals"]
    insp = summary["inspectors"]

    # ---- 分组 ----
    G = {"critical": [], "warning": [], "none": [], "false_positive": []}
    for h in houses:
        rv = h.get("review")
        if rv == "false_positive":
            G["false_positive"].append(h)
        elif h.get("eff") == "critical":
            G["critical"].append(h)
        elif h.get("eff") == "warning":
            G["warning"].append(h)
        else:
            G["none"].append(h)
    for k in G:
        G[k].sort(key=lambda x: (x.get("inspector") or "", x.get("cust_no") or ""))

    seq = 0
    grp_html = []
    for key, cls, label in (("critical", "g-cr", "严重提示"),
                            ("warning", "g-wa", "一般提示"),
                            ("none", "g-no", "正常"),
                            ("false_positive", "g-fp", "剔除（误判）")):
        items = G[key]
        opened = " open" if key in ("critical", "warning") else ""
        if not items:
            continue
        body = []
        for h in items:
            seq += 1
            body.append(_house(h, seq))
        grp_html.append(
            '<details class="grp %s"%s data-eff="%s"><summary>%s'
            '<span class="cnt">%d 户</span></summary><div class="grpbody">%s</div></details>'
            % (cls, opened, key, label, len(items), "".join(body)))

    # ---- 对比表 ----
    rows = []
    for a in insp:
        mrate = a.get("ms_rate")
        rows.append("""<tr>
      <td><b>%s</b></td>
      <td class="num">%d</td>
      <td class="num">%s</td>
      <td class="num"><b>%d</b></td>
      <td class="num"><span class="rate %s">%s%%</span></td>
      <td class="num">%s</td>
      <td class="num">%s%%</td><td class="num">%s%%</td><td class="num">%s%%</td>
    </tr>""" % (_esc(a["name"]), a.get("ms_sample", 0),
                ('<span class="rate %s">%s%%</span>' % (_rate_cls(mrate), mrate)) if mrate is not None else "—",
                a["total"], _rate_cls(a["rate"]), a["rate"],
                ("<b>+%.1f</b>" % (a["rate"] - mrate)) if mrate is not None else "—",
                a["p_crit"], a["p_warn"], a["p_issue"]))
    rows.append("""<tr style="background:#f8fafc;font-weight:700">
      <td>合计</td><td class="num">%d</td><td class="num">%s</td>
      <td class="num">%d</td><td class="num">%s%%</td><td class="num">—</td>
      <td class="num">%s%%</td><td class="num">%s%%</td><td class="num">%s%%</td>
    </tr>""" % (tt.get("ms_sample", 0),
                ('%s%%' % tt["ms_rate"]) if tt.get("ms_rate") is not None else "—",
                tt["total"], tt["rate"], tt["p_crit"], tt["p_warn"], tt["p_issue"]))

    # ---- 照片数据（供浮窗） ----
    hdata = {}
    for h in houses:
        arr = []
        for p in (h.get("photos") or []):
            if p.get("empty"):
                continue
            arr.append({"n": p.get("name") or p.get("items_code") or "",
                        "s": _src(p),
                        "e": 1 if p.get("ext_only") else 0,
                        "m": 1 if p.get("is_meter") else 0})
        hdata[str(h["rid"])] = arr

    insp_opts = "".join('<option value="%s">%s（%d）</option>' % (
        _esc(a["name"]), _esc(a["name"]), a["total"]) for a in insp)

    gen = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    mlab = t["month"]
    p_rate = tt["rate"]
    p_m = tt.get("ms_rate")

    html_doc = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(name)s · 专项复核报告</title><style>%(css)s</style></head><body><div class="wrap">
<h1>%(name)s　%(mlab)s 专项复核报告</h1>
<p class="sub">任务：%(tname)s　·　全量 %(total)d 户　·　生成 %(gen)s　·　生成后如有新复核请重新导出</p>

<div class="kpis">
  <div class="kpi"><div class="n">%(total)d</div><div class="l">纳入户数（全量）</div><div class="d">已判定 %(judged)d</div></div>
  <div class="kpi"><div class="n low">%(crit)d</div><div class="l">严重提示</div><div class="d">占 %(p_crit)s%%</div></div>
  <div class="kpi"><div class="n mid">%(warn)d</div><div class="l">一般提示</div><div class="d">占 %(p_warn)s%%</div></div>
  <div class="kpi"><div class="n hi">%(passn)d</div><div class="l">合格</div><div class="d">占 %(p_pass)s%%</div></div>
  <div class="kpi"><div class="n">%(excl)d</div><div class="l">剔除（误判）</div><div class="d">计入合格</div></div>
  <div class="kpi"><div class="n">%(rate)s%%</div><div class="l">全量合格率</div><div class="d">月度同口径 %(p_m)s</div></div>
</div>

<div class="card">
  <h2>月度报告 vs 专项复核<span>同一个人、同一月份，抽样与全量的差距</span></h2>
  <table>
    <thead><tr>
      <th>安检员</th><th class="num">月度抽样户</th><th class="num">月度合格率</th>
      <th class="num">专项户数</th><th class="num">专项合格率</th><th class="num">差异</th>
      <th class="num">严重率</th><th class="num">一般率</th><th class="num">问题率</th>
    </tr></thead>
    <tbody>%(rows)s</tbody>
  </table>
  <p class="hint" style="margin:10px 0 0">
    「严重率 / 一般率 / 问题率」= 该类问题户数 ÷ 专项户数，越低越好。
    正常户与剔除户都不计入任何问题率。
  </p>
</div>

<div class="toolbar">
  <span class="hint">筛选</span>
  <select id="fInsp"><option value="">全部安检员</option>%(insp_opts)s</select>
  <label class="chk e-cr on"><input type="checkbox" value="critical" checked>严重提示</label>
  <label class="chk e-wa on"><input type="checkbox" value="warning" checked>一般提示</label>
  <label class="chk e-no"><input type="checkbox" value="none">正常</label>
  <label class="chk e-fp"><input type="checkbox" value="false_positive">剔除</label>
  <button class="btn" id="btnReset">重置</button>
  <span class="hint" id="cntTxt"></span>
</div>

%(groups)s

<div class="foot">安检照片 AI 质检 · 专项复核报告　|　%(gen)s</div>
</div>

<div class="lb" id="lb">
  <div class="lbtop"><span id="lbTitle"></span><span class="x" onclick="lbClose()">×</span></div>
  <div class="lbmain">
    <button class="nav prev" id="lbPrev" onclick="lbGo(-1)">‹</button>
    <img id="lbImg" src="" alt="">
    <button class="nav next" id="lbNext" onclick="lbGo(1)">›</button>
  </div>
  <div class="lbstrip" id="lbStrip"></div>
  <div class="lbcap" id="lbCap"></div>
</div>

<script>
const HDATA = %(hdata)s;
let LB_RID = null, LB_I = 0;

function lbOpen(el){
  const rid = el.dataset.rid;
  const arr = HDATA[rid] || [];
  if(!arr.length) return;
  LB_RID = rid; LB_I = parseInt(el.dataset.i||'0',10) || 0;
  buildStrip();
  lbShow();
  document.getElementById('lb').classList.add('on');
  document.body.style.overflow='hidden';
}
function buildStrip(){
  const arr = HDATA[LB_RID]||[];
  document.getElementById('lbStrip').innerHTML = arr.map((p,i)=>
    `<img src="${p.s}" class="${i===LB_I?'on':''}" onclick="lbPick(${i})" alt="">`).join('');
}
function lbShow(){
  const arr = HDATA[LB_RID]||[];
  const p = arr[LB_I]; if(!p) return;
  document.getElementById('lbImg').src = p.s;
  document.getElementById('lbCap').textContent = `${LB_I+1} / ${arr.length}　${p.n}${p.m?'（表具）':''}${p.e?'　仅展示':''}`;
  document.getElementById('lbPrev').disabled = LB_I<=0;
  document.getElementById('lbNext').disabled = LB_I>=arr.length-1;
  [...document.getElementById('lbStrip').children].forEach((im,i)=>
    im.classList.toggle('on', i===LB_I));
  const card = document.querySelector(`.house[data-rid="${LB_RID}"]`);
  if(card){
    const hd = card.querySelector('.hd');
    if(hd) document.getElementById('lbTitle').textContent =
      hd.textContent.replace(/\\s+/g,' ').trim().slice(0,80);
  }
}
function lbPick(i){ LB_I=i; lbShow(); }
function lbGo(d){
  const arr = HDATA[LB_RID]||[];
  const n = LB_I+d;
  if(n<0||n>=arr.length) return;
  LB_I=n; lbShow();
}
function lbClose(){
  document.getElementById('lb').classList.remove('on');
  document.body.style.overflow='';
  LB_RID=null;
}
document.addEventListener('keydown',e=>{
  if(!LB_RID) return;
  if(e.key==='Escape') lbClose();
  if(e.key==='ArrowLeft') lbGo(-1);
  if(e.key==='ArrowRight') lbGo(1);
});

function applyFilter(){
  const insp = document.getElementById('fInsp').value;
  const on = new Set([...document.querySelectorAll('.toolbar .chk input:checked')].map(x=>x.value));
  let shown=0;
  document.querySelectorAll('details.grp').forEach(g=>{
    const eff = g.dataset.eff;
    const vis = on.has(eff);
    g.style.display = vis?'':'none';
    if(!vis) return;
    let n=0;
    g.querySelectorAll('.house').forEach(c=>{
      const ok = !insp || c.dataset.insp===insp;
      c.style.display = ok?'':'none';
      if(ok) n++;
    });
    g.querySelector('.cnt').textContent = n + ' 户';
    g.style.display = n?'':'none';
    shown += n;
  });
  document.getElementById('cntTxt').textContent = `当前显示 ${shown} 户`;
}
document.querySelectorAll('.toolbar .chk').forEach(l=>{
  const cb = l.querySelector('input');
  const sync = ()=>l.classList.toggle('on', cb.checked);
  cb.addEventListener('change', ()=>{ sync(); applyFilter(); });
  sync();
});
document.getElementById('fInsp').addEventListener('change', applyFilter);
document.getElementById('btnReset').addEventListener('click', ()=>{
  document.getElementById('fInsp').value='';
  document.querySelectorAll('.toolbar .chk input').forEach((cb,i)=>{
    cb.checked = i<2;
    cb.closest('.chk').classList.toggle('on', cb.checked);
  });
  applyFilter();
});
applyFilter();
</script></body></html>""" % dict(
        name=_esc(t["company_name"]), mlab=_esc(mlab), tname=_esc(t["name"]),
        total=tt["total"], judged=tt["judged"], crit=tt["crit"], warn=tt["warn"],
        passn=tt["pass_"], excl=tt["excluded"], rate=tt["rate"],
        p_crit=tt["p_crit"], p_warn=tt["p_warn"],
        p_pass=round(tt["pass_"] * 100.0 / tt["denom"], 1) if tt["denom"] else 0,
        p_m=("%s%%" % p_m) if p_m is not None else "—",
        rows="".join(rows), groups="".join(grp_html), insp_opts=insp_opts,
        hdata=json.dumps(hdata, ensure_ascii=False), css=CSS, gen=gen)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    return out_path
