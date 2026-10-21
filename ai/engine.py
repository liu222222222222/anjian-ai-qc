# -*- coding: utf-8 -*-
"""检测引擎 - 提示词 + 结果解析 + 照片分拣

核心口径（来自 42 户实测）：
- 表号读条形码下方印刷体数字行，读不出即填 null，禁止猜测（防假阳性）
- items8 若与 items1 高度相似 → 拍的是工作单不是热水器（33% 命中）
- 水印日期 vs 工单日期必须核对，跨月/跨日视为严重
- hidden_trouble 只填照片明确看到的；看不到的进 unverifiable
- blur 不作为判据（实测与可读性反相关）
- 「防霾卡号」不是表号，禁止用于核对
"""
import json, os, re, hashlib, shutil, sqlite3, datetime, urllib.request, ssl

CTX = ssl._create_unverified_context()

PROMPT_TEMPLATE = """你是燃气入户审核员。输入为同一户的安检现场照片 + 系统导出的档案信息。
只输出严格 JSON，不要任何多余文字。

【档案信息】
客户编号：{cust_no}
客户姓名：{cust_name}
安检员：{inspector}
安检时间：{check_time}
登记表号：{meter_no}
登记表型：{meter_type}
客户地址：{address}

【逐项检查】

1. meter_no_read     — 从"表具照片"条形码下方印刷体数字行读取，格式如 "2020.09 182000000002T"
2. meter_readable    — true / false
3. meter_match       — 与登记表号一致? true / false / null（读不出时填 null）
4. meter_reading     — 表盘累计读数，识别不到填 null
5. meter_type_photo  — 照片印刷型号（G2.5 / G1.6T / NB-IoT / JGD-N2.5-1），看不到填 null

6. watermark_date    — 抄写照片左下角水印的日期时间，原样，不推断；看不到填 null
7. time_match        — 水印日期与登记安检时间同一天? true / false / null

8. form_check_date   — 若图中有手写安检工作单，抄写"检查日期"栏，原样；无填 null
9. form_date_valid   — 手写日期不晚于今天、且不早于水印日期? true / false / null

10. item_misfiled    — 数组，指出"照片内容与所属检查项不符"，可选值：
    ["热水器项拍的是工作单","表具项拍的是非燃气表","报警器项拍的是其他物品","其他项内容明显错位"]
    无问题返回空数组。

11. scene_valid      — 照片内容确实是燃气安检现场? true / false
12. meter_visible    — 表具照片里真的看得到燃气表? true / false

13. hidden_trouble   — 数组，只填照片中【明确看到】的重大隐患，可选值：
    ["热水器无烟道","热水器装浴室","软管穿墙或穿吊顶",
     "表被完全封死无法检修","软管明显老化破损","管线私拆乱接"]
    拍不到的项目一律不要填，返回空数组。

14. unverifiable     — 数组，因照片未覆盖而无法判定，可选值：
    ["无燃气报警器","无自闭阀","熄火保护针失效","检漏结果","是否真实入户"]

15. comment          — 一句话说明主要问题；无问题填"正常"

【铁律】
· 读不出就填 null 或 false，绝不猜测补全。错判会冤枉安检员。
· hidden_trouble 只填看到的；看不到的进 unverifiable。混淆两者等于制造假隐患。
· "燃气表在柜子里"是正常安装，不算隐患。只有完全封死、无法开启检修才算。
· 不要评价"安检是否到位"。照片只能证明画面状态，证明不了检查动作。
· 不要把手写"防霾卡号"当表号核对——它与表号是不同编号体系。
· 你的输入是{photo_count}张照片，属于检查项「{items_name}」。"""

# 问题分级：critical=必须整改 / warning=提示 / info=仅记录
def _gap_over_168h(r):
    """水印与工单时间差 >= 168h（用户 2026-10-16 一刀切口径）
    不足 168h 不标记；没有时间字段可算时信任模型的 false 结论。"""
    if r.get("time_match") is not False:
        return False
    wt, ot = r.get("watermark_time"), r.get("check_time")
    if wt and ot:
        try:
            a = datetime.datetime.strptime(str(wt)[:19], "%Y-%m-%d %H:%M:%S")
            b = datetime.datetime.strptime(str(ot)[:19], "%Y-%m-%d %H:%M:%S")
            return abs((b - a).total_seconds()) / 60 >= 168 * 60
        except Exception:
            return False
    return True


ISSUE_RULES = [
    # (kind, severity, 判定函数)
    # 2026-10-19 改：表号读不出属"拍照不清晰"，是提示不是严重
    # （旧路线这里是 critical，与 Agent 路线口径不一致，会让同一张照片判出两种结果）
    ("表号读不出", "warning", lambda r: r.get("meter_readable") is False),
    ("表号与档案不一致", "critical",
     lambda r: r.get("meter_match") is False),
    ("热水器项拍的是工作单", "warning",
     lambda r: "热水器项拍的是工作单" in (r.get("item_misfiled") or [])),
    ("表具项拍的不是燃气表", "warning",
     lambda r: "表具项拍的是非燃气表" in (r.get("item_misfiled") or [])),
    ("照片内容与检查项错位", "warning",
     lambda r: bool([x for x in (r.get("item_misfiled") or [])
                     if x.startswith("其他项内容明显错位")])),
    ("水印日期与工单不符", "critical", _gap_over_168h),
    ("手写检查日期异常", "critical",
     lambda r: r.get("form_date_valid") is False),
    ("照片非安检现场", "critical",
     lambda r: r.get("scene_valid") is False),
    ("表具照片看不到燃气表", "warning",
     lambda r: r.get("meter_visible") is False),
    ("重大隐患", "critical", None),  # hidden_trouble 非空时逐条生成
    ("照片拍不到无法判定", "info", None),  # unverifiable 非空时逐条生成
]


def parse_json(text):
    """模型输出容错解析"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"\{.*\}", t, flags=re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


def judge(parsed):
    """把模型输出转成问题列表"""
    if not parsed or not isinstance(parsed, dict):
        return [("解析失败", "warning", "模型未返回有效 JSON，需人工复核")]
    out = []
    for kind, sev, fn in ISSUE_RULES:
        if fn is None:
            continue
        try:
            if fn(parsed):
                out.append((kind, sev, ""))
        except Exception:
            pass
    for h in (parsed.get("hidden_trouble") or []):
        out.append((f"重大隐患：{h}", "critical", ""))
    for u in (parsed.get("unverifiable") or []):
        out.append((f"无法判定：{u}", "info", ""))
    if not out and parsed.get("comment") and parsed["comment"] not in ("正常", ""):
        out.append(("其他", "info", str(parsed.get("comment"))[:200]))
    return out


def fetch(url, retries=3, timeout=90):
    """免认证直连下载照片"""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
                return resp.read()
        except Exception as e:
            last = e
            if i < retries - 1:
                import time
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"下载失败 {url[:80]}: {last}")


def photo_dir(company, month, cust_no, kind="photos", base=None):
    """kind: photos=有问题留存 / pending_delete=待删除 / false_positive=人工判误判永久留档"""
    from db import PHOTOS_DIR, PENDING_DIR, FALSE_POS_DIR
    root = {"pending_delete": PENDING_DIR,
            "false_positive": FALSE_POS_DIR}.get(kind, PHOTOS_DIR)
    d = os.path.join(root, company, month, cust_no)
    os.makedirs(d, exist_ok=True)
    return d


def classify_photo(company, month, cust_no, local, problems):
    """把照片移动到 留存/待删除 目录。

    口径（用户 2026-10-15 确认）：有任何问题（critical 或 warning）就留存，
    只有完全正常的户才进待删除。
    """
    base = "pending_delete"
    if problems:
        base = "photos"
    src = local
    if not os.path.exists(src):
        return None
    name = os.path.basename(src)
    dst_dir = photo_dir(company, month, cust_no, base)
    dst = os.path.join(dst_dir, name)
    if os.path.abspath(src) == os.path.abspath(dst):
        return dst
    if os.path.exists(dst):
        os.remove(src)
        return dst
    shutil.move(src, dst)
    return dst
