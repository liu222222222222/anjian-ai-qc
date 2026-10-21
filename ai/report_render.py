"""报告渲染：生成自包含 HTML（照片base64 内嵌，可直接转发）
两种：
  render_summary()  月度情况说明（集团月度汇总）
  render_company()  单个分公司详细报告（给分公司安检领导看）
用法见 __main__ / server.py 的 /api/report/download
"""
import os, re, base64, html as H, datetime
import db as D
import report_data as RD

# 2026-10-19 用户定：不再「按口径不留图」——图片改走原图 URL 后体积不是问题，
# 时间类问题把照片摆出来更有说服力。此集合保留为空，方便以后按需启用。
NO_PHOTO_KINDS = set()

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f4f6f8;color:#1f2733;
  font:14px/1.65 "Microsoft YaHei","Microsoft Yahei",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 22px 60px}
header{background:#fff;border:1px solid #e3e8ee;border-radius:12px;padding:22px 26px;margin-bottom:20px}
h1{margin:0 0 6px;font-size:23px;letter-spacing:.5px}
.sub{color:#6b7684;font-size:13px}
h2{font-size:16px;margin:26px 0 12px;padding-left:10px;border-left:4px solid #1668dc}
h3{font-size:14px;margin:18px 0 9px;color:#39424f}
.kpis{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));margin:18px 0}
.kpi{background:#fff;border:1px solid #e3e8ee;border-radius:10px;padding:14px 16px}
.kpi .v{font-size:28px;font-weight:700;line-height:1.15}
.kpi .l{color:#6b7684;font-size:12px;margin-top:3px}
.kpi .n{color:#8c959f;font-size:11px;margin-top:2px}
.v.r{color:#d4380d}.v.y{color:#b7791f}.v.g{color:#237804}.v.b{color:#1668dc}.v.d{color:#8c959f}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;
  border:1px solid #e3e8ee;font-size:12.5px}
th{background:#f0f3f7;color:#39424f;font-weight:600;text-align:left;padding:9px 10px;
  border-bottom:1px solid #e3e8ee;white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid #eef1f5;vertical-align:top}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fafbfc}
.ctr{text-align:center}.mono{font-family:Consolas,"Courier New",monospace}
.dim{color:#8c959f}
.b{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;border:1px solid}
.bc{background:#fff1f0;color:#d4380d;border-color:#ffccc7}
.bw{background:#fffbe6;color:#b7791f;border-color:#ffe58f}
.bg{background:#f6ffed;color:#237804;border-color:#b7eb8f}
.bd{background:#f4f6f8;color:#6b7684;border-color:#e3e8ee}
.sub2{color:#6b7684;font-size:11.5px;margin-top:2px}
.th{width:96px;border-radius:6px;border:1px solid #e3e8ee;display:block;cursor:zoom-in;
  transition:.15s}
.th:hover{transform:scale(1.04);border-color:#1668dc}
/* 行底色按人工审核三选一区分（2026-10-21 用户定）：
   严重提示=淡红，一般提示=淡黄，误判/未审核=无底色 */
tr.marked td{background:#fff8f7}
tr.marked:hover td{background:#fff0ee}
tr.tip td{background:#fffbe6}
tr.tip:hover td{background:#fff7d1}
tr.dropped td{background:#fafbfc;color:#8c959f}
.warnnote{color:#d4380d;font-weight:600;background:#fff1f0;border:1px solid #ffccc7;
  border-radius:4px;padding:1px 6px;display:inline-block;margin-top:3px}
/* 人工审核意见：完整呈现手写批注，领导一眼看到批的内容和人 */
.aunote{margin-top:5px;background:#fff1f0;border:1px solid #ffccc7;border-left:3px solid #d4380d;
  border-radius:5px;padding:5px 8px;color:#a8071a;font-size:12px;line-height:1.75;min-width:150px}
.aunote b{display:inline-block;margin-right:5px;font-size:10.5px;color:#fff;background:#d4380d;
  border-radius:3px;padding:0 5px;font-weight:600;vertical-align:1px}
.aumeta{margin-top:5px;font-size:11.5px;color:#39424f}
.aumeta b{color:#1668dc}
.tipbar{background:#f6ffed;border:1px solid #b7eb8f;border-left:4px solid #237804;
  color:#135200;padding:10px 14px;border-radius:6px;margin:0 0 18px;font-size:13px}
.tipbar.warn{background:#fffbe6;border-color:#ffe58f;border-left-color:#b7791f;color:#874d00}
.para{background:#fff;border:1px solid #e3e8ee;border-radius:10px;padding:16px 20px;
  font-size:14px;line-height:1.9;margin-bottom:18px}
.para strong{color:#1668dc}
.cobox{background:#fff;border:1px solid #e3e8ee;border-radius:10px;padding:14px 18px;margin-bottom:12px}
.cobox .cn{font-size:15px;font-weight:700;margin-bottom:6px}
.cobox .ct{color:#39424f;font-size:13px;line-height:1.85}
.cobox .cm{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px;font-size:12px;color:#6b7684}
.cobox .cm b{color:#1f2733}
.foot{margin-top:34px;padding-top:16px;border-top:1px solid #e3e8ee;color:#8c959f;font-size:11.5px;
  text-align:center;line-height:1.9}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px;font-size:12px;color:#6b7684}
.legend i{display:inline-block;width:13px;height:13px;border-radius:3px;border:1px solid;
  vertical-align:-2px;margin-right:5px}
/* 明细表档位筛选：三档可勾选，默认严重+一般，误判默认收起 */
.lvbar{display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin:-4px 0 11px;font-size:12.5px}
.lvbar label{display:inline-flex;align-items:center;gap:7px;cursor:pointer;background:#fff;
  border:1px solid #e3e8ee;border-radius:20px;padding:4px 13px 4px 10px;user-select:none;
  transition:.15s;color:#39424f}
.lvbar label:hover{border-color:#1668dc;box-shadow:0 1px 6px rgba(22,104,220,.1)}
.lvbar input{margin:0;cursor:pointer;accent-color:#1668dc;width:14px;height:14px}
/* 档位色点：实心小圆，不能做成方块——会被误认成第二个勾选框 */
.lvbar i.dot{width:7px;height:7px;border-radius:50%;display:inline-block;flex:0 0 auto}
.dot-r{background:#d4380d}
.dot-y{background:#e8a70c}
.dot-d{background:#b0b8c2}
.lvbar b{color:#1f2733;font-weight:700;margin-left:1px}
.lvbar .lvhint{color:#8c959f;font-size:11.5px}
/* 手动隐藏的行（配合 JS 筛选） */
table.hit tr[data-lv]{transition:background .12s}
.sw-r{background:#fff8f7;border-color:#ffccc7}
.sw-y{background:#fffbe6;border-color:#ffe58f}
.sw-d{background:#fafbfc;border-color:#e3e8ee}
/* 人工审核三档勾选：报告上直接看到勾了哪一个 */
.vbars{display:flex;gap:4px;margin-bottom:5px;flex-wrap:wrap}
.vbart{font-size:10.5px;line-height:1.6;padding:0 7px;border-radius:9px;
  border:1px dashed #d9dee5;color:#b6bec8;background:#fbfcfd;white-space:nowrap}
.vbart.on{color:#fff;border-style:solid;font-weight:600}
.vbart.on.fp{background:#8c959f;border-color:#8c959f}
.vbart.on.wa{background:#d48806;border-color:#d48806}
.vbart.on.cr{background:#d4380d;border-color:#d4380d}
.lb{position:fixed;inset:0;background:rgba(0,0,0,.86);display:none;align-items:center;
  justify-content:center;z-index:99;cursor:zoom-out}
.lb.on{display:flex}
.lb img{max-width:94vw;max-height:94vh;border-radius:6px}
@media print{body{background:#fff}.wrap{max-width:none;padding:0}
  .th{width:70px}h2{break-after:avoid}table{break-inside:auto}tr{break-inside:avoid}}
"""


def esc(x):
    return H.escape(str(x if x is not None else ""))


def month_label(month):
    if not month:
        return "本月"
    y, mm = month.split("-")
    return f"{y}年{int(mm)}月"


# ---------------------------------------------------------------- 审核意见
_PUNC_RE = re.compile(r"[\s，。；;、！!？?.,:：\"'“”‘’\-—_()（）\[\]【】/]+")


def _norm_text(s):
    return _PUNC_RE.sub("", str(s or ""))


def review_text(note, note_auto, note_manual):
    """返回该户完整的审核意见（单一文本）。

    2026-10-21 用户定：报告里不再区分「系统建议 / 人工批注」，一律按人工审核意见呈现。
    取数以库里的 note 为准（它本就是完整拼合串），保证一字不丢；旧写法
    `if note_manual ... elif note and not note_auto` 会在 note_auto 有值时把人工
    追加的整句吞掉（曾有 600+ 条复核意见踩这个坑），已废弃。
    """
    note = (note or "").strip()
    nm = (note_manual or "").strip()
    na = (note_auto or "").strip()
    if note:
        if nm and _norm_text(nm) not in _norm_text(note):
            return note + "；" + nm      # note 里没含人工段，补上，宁可多不可丢
        return note
    return nm or na


# ---------------------------------------------------------------- 图片
def embed_images(rows, max_side=600, quality=72):
    """把 rows 用到的照片转成 data URI

    2026-10-19：原来直接嵌原图，一份报告 101MB——微信/邮件根本发不出去。
    报告里图片只是缩略图（58px），点开看细节用 720px 足够，压到 75 质量后
    单张约 40-60KB，整份报告降到 20MB 量级。
    """
    import io
    out = {}
    for r in rows:
        for ph in (r["moved_to"] or r["local"] or "",):
            if not ph or ph in out:
                continue
            fp = os.path.normpath(os.path.join(D.BASE, ph))
            if not os.path.exists(fp):
                continue
            try:
                size = os.path.getsize(fp)
                if size <= 40 * 1024:
                    # 本来就小，直接嵌，不损失清晰度
                    with open(fp, "rb") as f:
                        out[ph] = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
                    continue
                from PIL import Image
                im = Image.open(fp)
                im = im.convert("RGB")
                im.thumbnail((max_side, max_side), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
                out[ph] = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
            except Exception:
                try:
                    with open(fp, "rb") as f:
                        out[ph] = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
                except Exception:
                    pass
    return out


def pic_html(ph, embedded, url=""):
    """图片优先级：内嵌 data URI（离线版）→ 安检系统原始 URL（外链版）→ 本地相对路径"""
    if not ph and not url:
        return '<span class="dim">无</span>'
    src = embedded.get(ph) or url or ("../" + (ph or ""))
    # 2026-10-19：源站返回 Content-Disposition: attachment，点链接会被下载；
    # 改成页内灯箱放大，不跳转、不下载
    # 点击放大由模板里的 LIGHTBOX 脚本统一处理（只认 .th 元素）
    return ('<img class="th" src="%s" loading="lazy" title="点击放大" '
            'onerror="this.style.opacity=.2;this.title=\'图片未能加载（需联网访问安检系统）\'">'
            ) % src


# ---------------------------------------------------------------- 公共块
def kpi_block(items):
    cs = []
    for v, l, n, cls in items:
        cs.append(f'<div class="kpi"><div class="v {cls}">{v}</div>'
                  f'<div class="l">{l}</div>'
                  + (f'<div class="n">{n}</div>' if n else "") + '</div>')
    return '<div class="kpis">' + "".join(cs) + '</div>'


def foot(extra=""):
    return (f'<div class="foot">安检抽查 AI 质检系统 · {esc(extra)}<br>'
            '本报告由系统自动生成，照片数据来源于各分公司上报的入户安检影像资料<br>'
            '判定结果已由集团工程部人工复核</div>')


LIGHTBOX = ('<div class="lb" id="lb" onclick="this.classList.remove(\'on\')">'
            '<img id="lbImg" alt=""></div>'
            '<script>document.addEventListener("click",function(e){'
            'if(e.target.classList.contains("th")){lbImg.src=e.target.src;lb.classList.add("on")}'
            'else if(e.target.id==="lbImg"){lb.classList.remove("on")}});'
            'document.addEventListener("keydown",e=>{if(e.key==="Escape")lb.classList.remove("on")});'
            '</script>')

# 明细表档位筛选：勾哪个显示哪个，误判默认不勾（收起）
FILTER_JS = """
<script>
(function(){
  var box=document.getElementById('lvFilter'); if(!box) return;
  var cbs=box.querySelectorAll('input[type=checkbox]');
  function apply(){
    var on={};
    for(var i=0;i<cbs.length;i++) on[cbs[i].getAttribute('data-lv')]=cbs[i].checked;
    var trs=document.querySelectorAll('table.hit tr[data-lv]');
    for(var j=0;j<trs.length;j++){
      var k=trs[j].getAttribute('data-lv');
      trs[j].style.display=(on[k]===false)?'none':'';
    }
  }
  for(var i=0;i<cbs.length;i++) cbs[i].addEventListener('change',apply);
  apply();
})();
</script>"""


# ---------------------------------------------------------------- 月度情况说明
def render_summary(stats, month=None, embed=True):
    """月度情况说明：各分公司汇总 + 结论"""
    con = D.connect()
    try:
        s = RD.summary(con, stats, month)
        active = [d for d in stats if d["sampled"] > 0]

        # 各分公司概览表
        comp_rows = "".join(
            f"""<tr><td><b>{esc(d['name'])}</b></td>
<td class="ctr">{d['total_house']:,}</td><td class="ctr">{d['sampled']:,}</td>
<td class="ctr">{d['rate']}%</td><td class="ctr">{d['reviewed']}</td>
<td class="ctr" style="color:#d4380d"><b>{d['critical_house']}</b></td>
<td class="ctr" style="color:#b7791f"><b>{d['warning_house']}</b></td>
<td class="ctr">{d['problem_house']}</td>
<td class="ctr">{round((1-d['problem_house']/d['sampled'])*100,1) if d['sampled'] else '—'}%</td></tr>"""
            for d in stats)

        # 每家分公司的数据条
        # 2026-10-19 用户定：不再渲染 company_text 那段文字结论（属评语），只留数字
        boxes = ""
        for d in stats:
            if d["sampled"] == 0 and d["total_house"] == 0:
                continue
            kpi = (f'<div class="cm">'
                   f'<span>入户<b>{d["total_house"]:,}</b> 户</span>'
                   f'<span>抽查<b>{d["sampled"]:,}</b> 户</span>'
                   f'<span>覆盖率 <b>{d["rate"]}%</b></span>'
                   f'<span>已人工审核 <b>{d["reviewed"]}</b> 户</span>'
                   f'<span>提示问题 <b style="color:#b7791f">{d["warning_house"]}</b> 户</span>'
                   f'<span>严重问题 <b style="color:#d4380d">{d["critical_house"]}</b> 户</span>'
                   + (f'<span>待人工审核 <b>{d["pending_review"]}</b> 户</span>'
                      if d["pending_review"] else "")
                   + '</div>')
            boxes += (f'<div class="cobox"><div class="cn">{esc(d["name"])}'
                      + ("" if d["sampled"] else '<span class="bd" style="margin-left:8px">'
                         '本月未开展抽查</span>')
                      + f'</div>{kpi}</div>')

        # 问题类型总分布
        agg = {}
        for d in stats:
            for k in d["kinds"]:
                key = (k["kind"], k["severity"])
                agg[key] = agg.get(key, 0) + k["n"]
        tot_n = sum(agg.values()) or 1
        kind_rows = "".join(
            f"""<tr><td>{esc(k)}</td>
<td class="ctr"><span class="b {'bc' if sev=='critical' else 'bw'}">"""
            f"""{'严重' if sev == 'critical' else '提示'}</span></td>"""
            f'<td class="ctr">{n}</td><td class="ctr">{round(n*100/tot_n,1)}%</td></tr>'
            for (k, sev), n in sorted(agg.items(), key=lambda x: -x[1]))

        # 安检员问题排行（只列有问题的）
        insp = []
        for d in stats:
            for i in d["inspector_rows"]:
                if i["crit"] or i["warn"]:
                    insp.append({**i, "company": d["name"]})
        insp.sort(key=lambda x: (-x["crit"], -x["warn"]))
        insp_rows = "".join(
            f"""<tr><td>{esc(i['company'])}</td><td>{esc(i['inspector'] or '（未填）')}</td>
<td class="ctr">{i['households']}</td>
<td class="ctr" style="color:#d4380d"><b>{i['crit']}</b></td>
<td class="ctr" style="color:#b7791f">{i['warn']}</td>
<td class="ctr">{round(i['crit']*100/i['households'],1) if i['households'] else 0}%</td></tr>"""
            for i in insp[:30]) or '<tr><td colspan="6" class="dim ctr" style="padding:20px">暂无问题记录</td></tr>'

        pending = s["pending"]
        tip = (f'<div class="tipbar">本说明汇总本月各分公司入户安检抽查情况，'
               f'各分公司详细问题清单（含照片）见分公司专项报告。'
               f'数据统计截至 {esc(s["generated"])}。</div>'
               if active else '<div class="tipbar warn">本说明暂无抽查数据。</div>')
        if pending:
            tip += (f'<div class="tipbar warn">注意：尚有 <b>{pending}</b> 户问题未完成人工复核，'
                    f'本说明为阶段性数据，复核完成后数据可能下降。</div>')

        return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>燃气入户安检抽查 · 月度情况说明 {esc(month_label(month))}</title><style>{CSS}</style></head><body>
<div class="wrap">
<header>
  <h1>燃气入户安检抽查 · 月度情况说明</h1>
  <div class="sub">{esc(month_label(month))}　|　覆盖 {len(active)} 家分公司
  <div class="sub" style="margin-top:4px">生成时间 {esc(s['generated'])}</div></div>
</header>

{tip}

<div class="kpis">
  <div class="kpi"><div class="v b">{s['total_house']:,}</div><div class="l">当月入户安检总数</div>
    <div class="n">户</div></div>
  <div class="kpi"><div class="v b">{s['sampled']:,}</div><div class="l">本次抽查数量</div>
    <div class="n">覆盖率 {s['rate']}%</div></div>
  <div class="kpi"><div class="v g">{s['reviewed']}</div><div class="l">已人工审核</div>
    <div class="n">{('待人工审核 ' + str(s['pending']) + ' 户') if s['pending'] else '已全部完成'}</div></div>
  <div class="kpi"><div class="v r">{s['critical_house']}</div><div class="l">严重问题户数</div>
    <div class="n">{s['critical_n']} 条记录</div></div>
  <div class="kpi"><div class="v y">{s['warning_house']}</div><div class="l">提示问题户数</div>
    <div class="n">{s['warning_n']} 条记录</div></div>
  <div class="kpi"><div class="v d">{s['problem_house']}</div><div class="l">问题涉及总户数</div>
    <div class="n">占抽查 {s['problem_rate']}%</div></div>
  <div class="kpi"><div class="v g">{s['ok_rate']}%</div><div class="l">抽查符合率</div>
    <div class="n">1 − 问题户/ 抽查户</div></div>
</div>

<!-- 2026-10-19 用户定：不渲染整体评语段落（summary.text），报告只呈现数据库统计数字 -->
<h2>一、各分公司抽查情况</h2>
<table>
<tr><th>分公司</th><th class="ctr">当月入户</th><th class="ctr">抽查</th><th class="ctr">覆盖率</th>
<th class="ctr">已人工审核</th><th class="ctr">严重问题户</th><th class="ctr">提示问题户</th>
<th class="ctr">问题总户</th><th class="ctr">符合率</th></tr>
{comp_rows}
</table>
<div class="sub2" style="margin-top:7px">
说明：抽查比例按每名安检员 10% 分层抽样；「严重/提示问题户」为去重后的户数，一户可能同时存在多类问题。</div>

<h2>二、各分公司结论</h2>
{boxes}

<h2>三、问题类型分布（全部公司合计）</h2>
<table>
<tr><th>问题类型</th><th class="ctr">等级</th><th class="ctr">条数</th><th class="ctr">占比</th></tr>
{kind_rows or '<tr><td colspan="4" class="dim ctr" style="padding:20px">本次未发现问题</td></tr>'}
</table>

<h2>四、安检员问题情况（仅列涉及问题的安检员）</h2>
<table>
<tr><th>分公司</th><th>安检员</th><th class="ctr">抽查户数</th><th class="ctr">严重问题户</th>
<th class="ctr">提示问题户</th><th class="ctr">严重问题占比</th></tr>
{insp_rows}
</table>
<div class="sub2" style="margin-top:7px">严重问题占比 = 涉及严重问题的户数 ÷ 该安检员抽查户数，仅作质量参考。</div>

{foot('月度情况说明')}
</div>
{LIGHTBOX}
</body></html>"""
    finally:
        con.close()


# ---------------------------------------------------------------- 分公司报告
def render_company(con, code, month=None, embed=True):
    """单个分公司的详细报告：统计 + 结论 + 抽查明细（含照片、人工审核）"""
    rw = "company=?" + (" AND substr(check_time,1,7)=?" if month else "")
    ra = [code] + ([month] if month else [])
    iw = "i.company=?" + (" AND i.month=?" if month else "")
    ia = [code] + ([month] if month else [])
    # JOIN records 后 company 列名歧义，记录侧条件加 r. 前缀
    rw2 = (rw.replace("company=?", "r.company=?")
              .replace("substr(check_time,1,7)=?", "substr(r.check_time,1,7)=?"))
    recs = f"SELECT r.id FROM records r WHERE {rw2} AND r.sampled=1"

    stats = RD.company_stats(con, month, code)
    d = stats[0]

    # 表号类问题常有 photo_id 为空（该户没拍表具），回退取该户任意一张照片，
    # 优先表具 → 其他 → 总体，避免整行显示"无照片"
    MC = D.meter_code(code)
    # 2026-10-21 用户定：报告里要能勾选查看「误判已剔除」的户，
    # 所以把误判户也捞出来（它们的 eff_severity 为 NULL），默认隐藏、由前端筛选控制。
    rows = con.execute(
        f"""SELECT i.severity, i.eff_severity, i.kind, i.detail, i.photo_id,
                   r.id AS record_id, r.cust_no, r.cust_name, r.inspector, r.address,
                   r.check_time, r.meter_no, p.local, p.moved_to, p.url,
                   v.verdict, v.note, v.note_auto, v.note_manual, v.reviewer, v.created_at
            FROM v_issue_eff i JOIN records r ON r.id=i.record_id
            LEFT JOIN photos p ON p.id = COALESCE(i.photo_id,
                 (SELECT ph.id FROM photos ph WHERE ph.record_id=i.record_id
                  ORDER BY CASE ph.items_code WHEN '{MC}' THEN 0
                           WHEN 'overall_path' THEN 2 ELSE 1 END, ph.id LIMIT 1))
            LEFT JOIN reviews v ON v.record_id=r.id
            WHERE {iw} AND (i.eff_severity IN ('critical','warning')
                            OR v.verdict='false_positive')
              AND r.id IN ({recs})
            ORDER BY CASE i.eff_severity WHEN 'critical' THEN 0 ELSE 1 END,
                     r.cust_no, i.kind""", ia + ra).fetchall()

    # 复核裁决（与主界面/分公司明细页同口径，2026-10-16）：
    # 误判户单列一档（默认隐藏），判严重/提示的按裁决换档
    rv_map = RD.review_map(con, code, month)
    rows_eff = []
    for r in rows:
        vd0 = (r["verdict"] or "").strip()
        fs = "false_positive" if vd0 == "false_positive" else (r["eff_severity"] or r["severity"])
        rows_eff.append((r, fs))
    embedded = embed_images([r for r, _ in rows_eff]) if embed else {}
    crit_house = len({r["record_id"] for r, fs in rows_eff if fs == "critical"})
    warn_house = len({r["record_id"] for r, fs in rows_eff if fs == "warning"})

    # 误判户以 reviews 为准统计（早期版本判误判时把 issues 删了，
    # 这些户在 v_issue_eff 里查不到，光按行数算会漏）
    fp_missing = con.execute(
        """SELECT r.id AS record_id, r.cust_no, r.cust_name, r.inspector, r.address,
                  r.check_time, r.meter_no, v.verdict, v.note, v.note_auto,
                  v.note_manual, v.reviewer, v.created_at
           FROM reviews v JOIN records r ON r.id=v.record_id
           WHERE v.company=? AND v.verdict='false_positive' AND r.sampled=1"""
        + (" AND substr(r.check_time,1,7)=?" if month else ""), ra).fetchall()
    fp_house = len(fp_missing)

    # 明细表：同一户的问题合并成一行，避免一户占好几行
    by_rec = {}
    for r, fs in rows_eff:
        by_rec.setdefault(r["record_id"], []).append((r, fs))

    # 误判户补全：issues 已被清掉的户，用 reviews + records 拼一行，
    # 保证勾上「误判已剔除」时能看到户号、审核意见和审核人
    for r in fp_missing:
        if r["record_id"] in by_rec:
            continue
        fake = {k: r[k] for k in ("record_id", "cust_no", "cust_name", "inspector",
                                  "address", "check_time", "meter_no", "verdict", "note",
                                  "note_auto", "note_manual", "reviewer", "created_at")}
        fake.update({"severity": "false_positive", "eff_severity": None,
                     "kind": "原判定已归档", "detail": "该户已由人工复核判定为误判，原始问题记录已归档",
                     "photo_id": None, "local": None, "moved_to": None, "url": None})
        by_rec[r["record_id"]] = [(fake, "false_positive")]

    trs = ""
    for rid, items in by_rec.items():
        it0 = items[0][0]
        lv = items[0][1]                     # critical / warning / false_positive
        lvcode = {"critical": "cr", "warning": "wa",
                  "false_positive": "fp"}.get(lv, "wa")
        if lv == "false_positive":
            badge = '<span class="b bd">误判</span>'
        elif lv == "critical":
            badge = '<span class="b bc">严重</span>'
        else:
            badge = '<span class="b bw">提示</span>'
        prob_html = "".join(
            f'<div style="margin-bottom:3px"><b>{esc(i["kind"])}</b>'
            f'<span class="b {"bc" if fs == "critical" else ("bd" if fs == "false_positive" else "bw")}" '
            f'style="margin-left:5px">'
            f'{"严重" if fs == "critical" else ("误判" if fs == "false_positive" else "提示")}</span>'
            + (f'<div class="sub2">{esc(i["detail"])}</div>' if i["detail"] else "")
            + '</div>' for i, fs in items)
        # 照片：只给需要看图的问题类型配图
        pics = []
        seen_ph = set()
        for i, _fs in items:
            if i["kind"] in NO_PHOTO_KINDS:
                continue
            ph = i["moved_to"] or i["local"] or ""
            if not ph or ph in seen_ph:
                continue
            seen_ph.add(ph)
            pics.append(pic_html(ph, embedded, i["url"]))
        photo_cell = ('<div class="sub2">无照片</div>' if not pics
                      else '<div style="display:flex;gap:5px;flex-wrap:wrap;justify-content:center">'
                           + "".join(pics) + '</div>')
        # 人工审核列：三档勾选（勾中的点亮）+ 完整批注 + 审核人/时间
        # 2026-10-21 用户定：报告上要直接看到审核时勾了哪一个
        vd = (it0["verdict"] or "").strip()
        def _vb(code, cls, label):
            on = " on" if vd == code else ""
            return f'<span class="vbart {cls}{on}">{label}</span>'
        rv = ('<div class="vbars">'
              + _vb("false_positive", "fp", "误判")
              + _vb("warning", "wa", "一般提示")
              + _vb("critical", "cr", "严重提示")
              + '</div>')
        if not vd:
            rv += '<div class="sub2">尚未人工审核</div>'
        note = (it0["note"] or "").strip()
        na = (it0["note_auto"] or "").strip()
        nm = (it0["note_manual"] or "").strip()
        opinion = review_text(note, na, nm)
        if opinion:
            rv += f'<div class="aunote"><b>审核意见</b>{esc(opinion)}</div>'
        else:
            rv += '<div class="sub2" style="margin-top:4px">未填写审核意见</div>'
        # 行底色跟着人工审核三选一走：严重=淡红、一般=淡黄、误判=灰、未审核=无色
        rowcls = {"critical": "marked", "warning": "tip",
                  "false_positive": "dropped"}.get((vd or "").strip(), "")
        if not rowcls:
            rowcls = "marked" if lv == "critical" else ("tip" if lv == "warning" else "dropped")
        if it0["reviewer"]:
            ts = (it0["created_at"] or "")[:16]
            rv += (f'<div class="aumeta">审核人 <b>{esc(it0["reviewer"])}</b>'
                   + (f' · {esc(ts)}' if ts else "") + '</div>')
        else:
            rv += '<div class="aumeta" style="color:#8c959f">尚未人工审核</div>'

        tr_cls = f' class="{rowcls}" data-lv="{lvcode}"'
        if lvcode == "fp":
            # 误判档默认收起：写成内联样式，即使 JS 没跑也不会混进正文
            tr_cls += ' style="display:none"'
        trs += f"""<tr{tr_cls}>
<td class="ctr">{badge}</td>
<td class="mono">{esc(it0['cust_no'])}</td>
<td>{esc(it0['cust_name'])}</td>
<td>{esc(it0['inspector'])}</td>
<td class="sub2">{esc(it0['address'])}</td>
<td class="sub2">{esc(it0['check_time'])}</td>
<td class="mono">{esc(it0['meter_no'])}</td>
<td>{prob_html}</td>
<td class="ctr">{photo_cell}</td>
<td>{rv}</td></tr>"""

    if not trs:
        trs = ('<tr><td colspan="10" class="dim ctr" style="padding:32px">'
               '本次抽查未发现严重或提示性问题，全部符合要求。</td></tr>')

    kind_rows = "".join(
        f"""<tr><td>{esc(k['kind'])}</td>
<td class="ctr"><span class="b {'bc' if k['severity'] == 'critical' else 'bw'}">"""
        f"""{'严重' if k['severity'] == 'critical' else '提示'}</span></td>"""
        f'<td class="ctr">{k["n"]}</td></tr>'
        for k in d["kinds"]) or '<tr><td colspan="3" class="dim ctr">—</td></tr>'

    insp_rows = "".join(
        f"""<tr><td>{esc(i['inspector'] or '（未填）')}</td>
<td class="ctr">{i['households']}</td>
<td class="ctr" style="color:#d4380d"><b>{i['crit']}</b></td>
<td class="ctr" style="color:#b7791f">{i['warn']}</td>
<td class="ctr">{round(i['crit']*100/i['households'],1) if i['households'] else 0}%</td>
<td class="ctr">{round((1-(i['crit']+i['warn'])/i['households'])*100,1) if i['households'] else '—'}%</td></tr>"""
        for i in d["inspector_rows"]) or '<tr><td colspan="6" class="dim ctr">—</td></tr>'

    ok = round((1 - d["problem_house"] / d["sampled"]) * 100, 1) if d["sampled"] else 100.0
    # 2026-10-19 用户要求：删掉「明细说明 / 误判剔除 / 标红含义」三条提示，
    # 只保留「尚有 N 户未复核」这条——它提醒报告数据还没定稿
    tip = ""
    if d["pending_review"]:
        tip += (f'<div class="tipbar warn">尚有 <b>{d["pending_review"]}</b> 户未完成人工审核，'
                f'标记为「待人工审核」的档位确认后本报告数据会变化。</div>')

    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>安检抽查月度报告 · {esc(d['name'])} {esc(month_label(month))}</title>
<style>{CSS}</style></head><body>
<div class="wrap">
<header>
  <h1>燃气入户安检抽查报告 · {esc(d['name'])}</h1>
  <div class="sub">{esc(month_label(month))}　|　生成时间 {esc(datetime.datetime.now().strftime('%Y-%m-%d %H:%M'))}</div>
</header>

{tip}

<div class="kpis">
  <div class="kpi"><div class="v b">{d['total_house']:,}</div><div class="l">当月入户安检总数</div>
    <div class="n">户</div></div>
  <div class="kpi"><div class="v b">{d['sampled']:,}</div><div class="l">本次抽查数量</div>
    <div class="n">覆盖率 {d['rate']}%</div></div>
  <div class="kpi"><div class="v g">{d['reviewed']}</div><div class="l">已人工审核</div>
    <div class="n">{('待人工审核 ' + str(d['pending_review']) + ' 户') if d['pending_review'] else '已全部完成'}</div></div>
  <div class="kpi"><div class="v r">{d['critical_house']}</div><div class="l">严重问题户数</div>
    <div class="n">{crit_house} 户涉及</div></div>
  <div class="kpi"><div class="v y">{d['warning_house']}</div><div class="l">提示问题户数</div></div>
  <div class="kpi"><div class="v d">{d['problem_house']}</div><div class="l">问题涉及总户数</div></div>
  <div class="kpi"><div class="v g">{ok}%</div><div class="l">抽查符合率</div></div>
</div>

<!-- 2026-10-19 用户定：不渲染整体评语（company_text），报告只呈现数据库数字 -->
<h2>一、问题类型分布</h2>
<table><tr><th>问题类型</th><th class="ctr">等级</th><th class="ctr">条数</th></tr>{kind_rows}</table>

<h2>二、安检员抽查质量</h2>
<table><tr><th>安检员</th><th class="ctr">抽查户数</th><th class="ctr">严重问题户</th>
<th class="ctr">提示问题户</th><th class="ctr">严重问题占比</th><th class="ctr">抽查符合率</th></tr>
{insp_rows}</table>
<div class="sub2" style="margin-top:7px">安检员抽查户数按其入户总数的 10% 分层抽样得出。</div>

<h2>三、抽查问题明细</h2>
<div class="sub2" style="margin:-4px 0 10px">
「AI 批注」为系统读图自动标注的问题；「人工审核」为集团工程部逐张复看后的结论与手写意见。
三个档位中<b>实心高亮</b>的即本次审核勾选结果，行底色同步区分。可按档位筛选下方明细：</div>
<div class="lvbar" id="lvFilter">
  <label><input type="checkbox" data-lv="cr" checked><i class="dot dot-r"></i>严重提示<b>{crit_house}</b></label>
  <label><input type="checkbox" data-lv="wa" checked><i class="dot dot-y"></i>一般提示<b>{warn_house}</b></label>
  <label><input type="checkbox" data-lv="fp"><i class="dot dot-d"></i>误判已剔除<b>{fp_house}</b></label>
  <span class="lvhint">取消勾选即隐藏该档</span>
</div>
<table class="hit">
<tr><th class="ctr">等级</th><th>客户编号</th><th>客户姓名</th><th>安检员</th><th>地址</th>
<th>安检时间</th><th>登记表号</th><th>AI 批注</th><th class="ctr">照片</th><th>人工审核</th></tr>
{trs}
</table>
<div class="sub2" style="margin-top:7px">
本次问题户 <b>{crit_house + warn_house}</b> 户（严重 {crit_house} 户、一般 {warn_house} 户）
{f'，另有 {fp_house} 户经人工复核为误判、已从统计中剔除' if fp_house else ''}；
{d['total_house']:,} 户中抽查 {d['sampled']:,} 户，符合率 {ok}%。</div>

{foot(d['name'] + '分公司报告')}
</div>
{LIGHTBOX}
{FILTER_JS}
</body></html>"""


if __name__ == "__main__":
    import sys
    month = sys.argv[1] if len(sys.argv) > 1 else "2026-09"
    code = sys.argv[2] if len(sys.argv) > 2 else None
    con = D.connect()
    stats = RD.company_stats(con, month)
    if code:
        print(render_company(con, code, month)[:400])
    else:
        print(render_summary(stats, month)[:400])
