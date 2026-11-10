# -*- coding: utf-8 -*-
"""安检抽查系统 - 数据库层

设计要点：
- 按"检查项(items_code)"原子提交，断点续查只补没跑完的项，不重烧钱
- settings 表存模型配置与运行参数，界面可改
- issues 表存问题明细，直接驱动报告与照片分拣
"""
import os, sqlite3, json, time

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "db.sqlite3")
PHOTOS_DIR = os.path.join(BASE, "photos")
PENDING_DIR = os.path.join(BASE, "pending_delete")
# 人工复核判「误判」的户：照片永久留档，不进待删除（用户 2026-10-15 要求）
FALSE_POS_DIR = os.path.join(BASE, "false_positive")
REPORTS_DIR = os.path.join(BASE, "reports")

# 照片水印时间与安检工单时间的允许偏差（分钟）
# 用户 2026-10-16 定：168 小时（7 天）一刀切——
#   gap >= 168h → critical（严重提示）；不足 168h → 不标记
# 原 8 小时/20 天分档口径作废（人工审核量太大，不再逐户判）
TIME_GAP_MIN = 168 * 60
CONFIG_PATH = os.path.join(BASE, "config.json")

# ★ 接入你自己的公司：只改这一张表即可，其余代码全部按 code 走。
#   格式 (公司代码, 显示名)。代码建议 2-4 位大写字母，会出现在库字段、目录名与 URL 里。
#   下面 C1~C6 是脱敏占位，按你的实际分公司替换（家数可多可少）。
COMPANY_ORDER = [
    ("C1", "A 公司"),
    ("C2", "B 公司"),
    ("C3", "C 公司"),
    ("C4", "D 公司"),
    ("C5", "E 公司"),
    ("C6", "F 公司"),
]
COMPANY_NAME = {k: v for k, v in COMPANY_ORDER}

# 检查项中文名
ITEMS_NAME = {
    "items0": "隐患前照片", "items1": "安检单拍照", "items2": "表具照片",
    "items3": "报警器", "items4": "自闭阀", "items5": "安检圆贴",
    "items6": "灶具全景", "items7": "熄火保护针", "items8": "热水器",
    "items9": "灶下检漏", "items10": "厨房全景", "overall_path": "总体安检照片",
}

# ★ 公司级差异（本系统最重要的一段业务知识，接自己的数据前必读）：
# 同一业务含义的检查项，不同公司的安检系统分配 items 编号**不一样**，不能硬编码 items2。
# 实测六家 Excel 后确认的对应关系（代码见 COMPANY_ORDER）：
#   C2/C4/C5/C6 → 表具 items2（安检单 items1；C4 的安检单是 items0）
#   C3          → 表具 items1（安检单 items7、灶具 items2）
#   C1          → 表具 items4 + items17（两列都叫表具照片，任一图是燃气表即认）
# 表号核对一律取 meter_codes(company)；拿不准时用 photos.category LIKE '%表具%'
# 或 photos.is_meter_photo=1（导入时已按 Excel 列名写入）。
METER_ITEMS = {
    # C1 的表具照片分散在两列（2026-09 改版后新增「燃气表-表具照片」列 → items17）：
    # items4 优先（device8 主通道），items17 兜底（device1 上传）
    "C1": ("items4", "items17"),
    "C3": ("items1",),   # C3：表具照片（该司安检单在 items7）
}
# 安检单拍照所在的 items_code（默认 items1；C4 是 items0、C3 是 items7）
FORM_ITEMS = {
    "C4": "items0",
    "C3": "items7",
}

# ★ 用气性质白名单（导入筛选条件，与「安检状态=入户」同级）：
# 只查「民用」，商用/工业/公福/学校/集体全剔。
# 各家系统对民用的叫法不统一（煤改气、热水器、民用采暖1…），必须按公司配白名单，
# 不能只匹配字符串 "民用"，否则会漏掉整批煤改气居民户（实测单家公司上万户）。
GAS_NATURE_WHITELIST = {
    "C1": ("民用", "乡镇民用"),
    "C2": ("民用", "民用取暖", "煤改气民用取暖", "煤改气民用采暖", "煤改气"),
    "C3": ("民用", "民用采暖1", "民用煤改气1", "煤改气民用取暖", "物联网煤改气民用阶梯"),
    "C4": ("民用", "民用取暖", "热水器"),
    "C5": ("民用", "民用采暖1", "煤改气民用取暖"),
    "C6": ("民用", "煤改气民用炊事", "煤改气民用取暖"),
}


def gas_allowed(company, gas_nature):
    """该户用气性质是否属于本公司可抽查范围（民用）"""
    wl = GAS_NATURE_WHITELIST.get((company or "").upper())
    if not wl:                      # 未配置的公司不拦，避免静默丢掉整家公司
        return True
    return (gas_nature or "").strip() in wl


def gas_nature_cond(company, alias="r"):
    """与 gas_allowed() 等价的 SQL 条件 + 参数（供报告分母等聚合查询用）

    未配置白名单的公司返回 (\"1=1\", [])，不拦。
    """
    wl = GAS_NATURE_WHITELIST.get((company or "").upper())
    if not wl:
        return "1=1", []
    ph = ",".join("?" * len(wl))
    return f"TRIM(COALESCE({alias}.gas_nature,'')) IN ({ph})", list(wl)


def checkable_cond(company, alias="r"):
    """「可抽查户」条件 = 民用 + 有表具照片 + 未被标记查不了（与抽样池完全同口径）

    meter_skip 是 2026-10-22 加的持久标记：撤销抽样时置 1。
    不能只靠 `sampled=0` —— 撤销后重新补抽会把同一户又选回来（A 公司实测 8 户死循环）。
    """
    g, ga = gas_nature_cond(company, alias)
    m, ma = meter_photo_cond(company, alias)
    return (f"({g}) AND ({m}) AND COALESCE({alias}.meter_skip,0)=0"), ga + ma


def pool_cond(company, alias="r"):
    """抽样池条件（checkable_cond 的别名，语义更直白）"""
    return checkable_cond(company, alias)


def meter_codes(company):
    """该公司表具照片可能所在的全部 items_code（A 公司有两个）"""
    v = METER_ITEMS.get((company or "").upper())
    if not v:
        return ("items2",)
    return v if isinstance(v, tuple) else (v,)


def meter_code(company):
    """该公司表具照片的主 items_code（默认 items2）；多值公司取第一个"""
    return meter_codes(company)[0]


def meter_photo_paths(con, record_id, company):
    """该户全部表具照片的本地绝对路径，按 meter_codes 顺序（A 公司 items4 → items17）

    OCR / 条码解码等辅助脚本用它做多图交叉验证：单张解不出表号时换另一张试。
    """
    out = []
    for mc in meter_codes(company):
        for row in con.execute(
                "SELECT local FROM photos WHERE record_id=? AND items_code=? "
                "AND downloaded=1 ORDER BY id", (record_id, mc)).fetchall():
            loc = row["local"] if isinstance(row, sqlite3.Row) else row[0]
            if not loc:
                continue
            p = os.path.join(BASE, loc)
            if os.path.exists(p) and p not in out:
                out.append(p)
    return out


def form_code(company):
    """该公司安检单照片所在的 items_code（默认 items1）"""
    return FORM_ITEMS.get((company or "").upper(), "items1")


def meter_photo_cond(company, alias="r"):
    """「该户有表具照片」的 SQL 条件 + 参数（2026-10-22 用户定）

    没有表具照片的户**不进抽样池** —— 抽查核心是核对表号，
    这户连表都没拍到，抽出来只能产出一条「查不了」的噪声，白占人工复核工时。

    判定条件（二者满足其一）：items_code ∈ meter_codes(公司)，或 category 含「表具」。
    后者兜住 B 公司「表具整体照片(表具全景及防盗卡)」这类独立命名的整表全景列。
    """
    mcs = meter_codes(company)
    ph = ",".join("?" * len(mcs))
    cond = (f"EXISTS(SELECT 1 FROM photos p WHERE p.record_id={alias}.id "
            f"AND (p.items_code IN ({ph}) OR p.category LIKE '%表具%'))")
    return cond, list(mcs)


def default_items(company):
    """默认要看的检查项（表具+安检单+热水器+总体），实际按该户是否有图过滤"""
    return list(meter_codes(company)) + [form_code(company), "items8", "overall_path"]


def item_display(code, category, company):
    """检查项显示名：表具/安检单按公司口径给准；其余优先用 Excel 原始列名（去掉括号说明），
    最后才回退 ITEMS_NAME —— 因为同一 items_code 在不同公司含义不同
    （如 items2 在 E 公司是表具、在 A 公司是安检过程中照片）"""
    c = (company or "").upper()
    if code and code in meter_codes(c):
        return "表具照片"
    if code and code == form_code(c):
        return "安检单拍照"
    if category:
        import re as _re
        short = _re.sub(r"[（(].*$", "", str(category)).strip()
        if short:
            return short
    return ITEMS_NAME.get(code) or (category or code)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY, v TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT, month TEXT, total_records INTEGER, total_checks INTEGER,
    done_checks INTEGER, fail_checks INTEGER, status TEXT,
    model_used TEXT, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
    cost REAL DEFAULT 0, started_at TEXT, finished_at TEXT
);
-- 每户一条
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL, cust_no TEXT NOT NULL, cust_name TEXT, phone TEXT,
    cust_type TEXT, community TEXT, address TEXT, check_status TEXT,
    visit_fail INTEGER, check_time TEXT, inspector TEXT, plan_name TEXT,
    plan_type TEXT, meter_type TEXT, meter_brand TEXT, gas_nature TEXT,
    meter_no TEXT, hazard_raw TEXT, photo_count INTEGER,
    sampled INTEGER DEFAULT 0,          -- 是否被本次抽样命中（报告口径按 sampled=1 过滤）
    meter_skip INTEGER DEFAULT 0,       -- 1=已判定「查不了表号」，永久排除出抽样池
    src_file TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(company, cust_no, src_file)
);
CREATE INDEX IF NOT EXISTS idx_records_company ON records(company);
CREATE INDEX IF NOT EXISTS idx_records_inspector ON records(inspector);
-- 每张照片一条
CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL, company TEXT, month TEXT,
    category TEXT, items_code TEXT, seq INTEGER, is_meter_photo INTEGER DEFAULT 0,
    url TEXT, local TEXT, sha256 TEXT, bytes INTEGER, width INTEGER, height INTEGER,
    downloaded INTEGER DEFAULT 0, verdict TEXT DEFAULT '',
    moved_to TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_photos_record ON photos(record_id);
-- 检查项原子单元：断点续查的关键
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL, company TEXT, month TEXT,
    items_code TEXT, photo_ids TEXT, status TEXT DEFAULT 'pending',
    result_json TEXT, error TEXT, tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0, cost REAL DEFAULT 0, attempts INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(record_id, items_code)
);
CREATE INDEX IF NOT EXISTS idx_checks_status ON checks(status);
-- 问题明细
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER, record_id INTEGER NOT NULL, company TEXT, month TEXT,
    items_code TEXT, photo_id INTEGER, severity TEXT, kind TEXT,
    detail TEXT, evidence TEXT DEFAULT '',
    active INTEGER DEFAULT 1,           -- 0=历史版本（重判后留档），1=当前生效
    rev INTEGER DEFAULT 1,              -- 该户第几次判定
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_issues_run ON issues(run_id);
-- 人工基准（ground truth，用于持续对模型准确率）
CREATE TABLE IF NOT EXISTS truth (
    record_id INTEGER PRIMARY KEY, meter_no TEXT, readable INTEGER,
    note TEXT, by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
-- 人工复核意见（逐户过，带进报告）
CREATE TABLE IF NOT EXISTS reviews (
    record_id INTEGER NOT NULL, company TEXT, month TEXT, seq INTEGER,
    verdict TEXT,            -- false_positive=误判 / warning=一般提示 / critical=严重提示
    note TEXT,               -- 合并后的完整备注（报表直接展示）
    note_auto TEXT DEFAULT '',   -- 系统自动识别那一段
    note_manual TEXT DEFAULT '', -- 人工真写的那部分（判断"是否人填"只看这个）
    reviewer TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(record_id)
);
-- 复核操作日志（2026-10-19 加）：改判、撤销都留痕，申诉时可举证
CREATE TABLE IF NOT EXISTS review_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL, company TEXT, month TEXT,
    action TEXT,                 -- save / change / undo
    verdict_from TEXT, verdict_to TEXT,
    note TEXT, reviewer TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_rlog_record ON review_log(record_id);
"""

# 视图定义（版本变化时 _migrate 会重建）
SCHEMA_VERSION = 4
VIEW_SQL = """
CREATE VIEW v_issue_eff AS
SELECT i.*,
       CASE WHEN r.verdict = 'false_positive' THEN NULL
            WHEN r.verdict IN ('critical','warning') THEN r.verdict
            ELSE i.severity END AS eff_severity
FROM issues i
LEFT JOIN reviews r ON r.record_id = i.record_id
WHERE COALESCE(i.active, 1) = 1
"""


def _has_col(con, table, col):
    return any(r[1] == col for r in con.execute(f"PRAGMA table_info({table})"))


def _unique_cols(con, table):
    """返回 table 的 UNIQUE 约束列（取第一个唯一索引）。"""
    try:
        for row in con.execute(f"PRAGMA index_list({table})"):
            # row: seq, name, unique, origin('u'=UNIQUE 约束), partial
            if row[2] == 1 and (row[3] or "") == "u":
                return [r[2] for r in con.execute(f'PRAGMA index_info("{row[1]}")')]
    except Exception:
        pass
    return []


def _fix_records_unique(con):
    """把 records 的唯一约束升到 (company,cust_no,src_file)（2026-11-04）。

    老库实际是 UNIQUE(company,cust_no)，与 SCHEMA 声明不一致 —— 这是早期建表留下的，
    当时还没做多月并存。后果：10 月导入时只要某客户 9 月也检过，就撞
    `IntegrityError: UNIQUE constraint failed: records.company, records.cust_no`，
    整家公司建不了档（2026-11-04 A 公司实测 2 户重叠即全表中断）。

    重建表必须**原样搬 id**：checks/issues/reviews/photos 全部按 record_id 关联，
    id 一变，9 月的人工复核意见和问题明细就全成孤儿。
    """
    cols = _unique_cols(con, "records")
    if not cols or "src_file" in cols:
        return False
    info = list(con.execute("PRAGMA table_info(records)"))
    names = [r[1] for r in info]
    defs = []
    for r in info:
        name, typ, notnull, dflt, pk = r[1], (r[2] or ""), r[3], r[4], r[5]
        if pk and name == "id":
            defs.append(f"{name} {typ} PRIMARY KEY AUTOINCREMENT".strip())
            continue
        d = f"{name} {typ}".strip()
        if notnull:
            d += " NOT NULL"
        if dflt is not None:
            d += f" DEFAULT {dflt}"
        defs.append(d)
    defs.append("UNIQUE(company, cust_no, src_file)")
    collist = ", ".join(f'"{c}"' for c in names)
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(f"CREATE TABLE records__new ({', '.join(defs)})")
        con.execute(f"INSERT INTO records__new ({collist}) SELECT {collist} FROM records")
        con.execute("DROP TABLE records")
        con.execute("ALTER TABLE records__new RENAME TO records")
        con.execute("CREATE INDEX IF NOT EXISTS idx_records_company ON records(company)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_records_inspector ON records(inspector)")
        con.commit()
    except Exception:
        con.rollback()
        raise
    return True


def _migrate(con):
    """幂等迁移：给老库补字段（2026-10-19 数据链路改造）
    issues.active  —— 0=历史版本（重判后留档），1=当前生效
    issues.rev     —— 该户第几次判定
    reviews.note_auto / note_manual —— 区分"系统自动识别"与"人工真写"
    records.meter_skip —— 1=已判定「查不了表号」（无表具照片/拍的不是表），
                          永久排除出抽样池。不能只靠 sampled=0 —— 撤销后重新补抽
                          会把它又选回来（实测某公司 8 户死循环）
    records.sampled —— 是否被抽样命中（报告/看板口径都按 sampled=1 过滤）
    """
    changed = False
    # records 唯一约束升级（2026-11-04）：老库是 (company,cust_no)，换月重导必撞。
    # 放在最前：后面的补列都依赖 records 存在且可用。
    if _fix_records_unique(con):
        changed = True
    if not _has_col(con, "records", "sampled"):
        con.execute("ALTER TABLE records ADD COLUMN sampled INTEGER DEFAULT 0")
        changed = True
    if not _has_col(con, "records", "meter_skip"):
        con.execute("ALTER TABLE records ADD COLUMN meter_skip INTEGER DEFAULT 0")
        changed = True
    if not _has_col(con, "issues", "active"):
        con.execute("ALTER TABLE issues ADD COLUMN active INTEGER DEFAULT 1")
        changed = True
    if not _has_col(con, "issues", "rev"):
        con.execute("ALTER TABLE issues ADD COLUMN rev INTEGER DEFAULT 1")
        changed = True
    if not _has_col(con, "reviews", "note_auto"):
        con.execute("ALTER TABLE reviews ADD COLUMN note_auto TEXT DEFAULT ''")
        changed = True
    if not _has_col(con, "reviews", "note_manual"):
        con.execute("ALTER TABLE reviews ADD COLUMN note_manual TEXT DEFAULT ''")
        changed = True
    # 视图重建（按 schema 版本号，避免每次连接都动 schema）
    ver = 0
    try:
        row = con.execute("SELECT v FROM settings WHERE k='schema_ver'").fetchone()
        ver = int(row[0]) if row else 0
    except Exception:
        ver = 0

    # 只有「补了字段」或「版本落后」时才写库。
    # 每次连接都 UPDATE 会在并发请求下抢写锁（实测 database is locked）
    if changed or ver < SCHEMA_VERSION:
        if changed:
            # 老库的 issues 可能是 NULL（ADD COLUMN 前插入的行）
            con.execute("UPDATE issues SET active=1 WHERE active IS NULL")
            con.execute("UPDATE issues SET rev=1 WHERE rev IS NULL")
        con.execute("CREATE INDEX IF NOT EXISTS idx_issues_rec_active ON issues(record_id, active)")
        if ver < SCHEMA_VERSION:
            con.execute("DROP VIEW IF EXISTS v_issue_eff")
            con.execute(VIEW_SQL)
            con.execute("INSERT OR REPLACE INTO settings(k,v) VALUES('schema_ver',?)",
                        (str(SCHEMA_VERSION),))
        con.commit()



def connect(path=None):
    os.makedirs(os.path.dirname(path or DB_PATH), exist_ok=True)
    con = sqlite3.connect(path or DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    # journal_mode 只在不是 WAL 时切换：每次连接都执行会申请独占锁，
    # 与其他连接/Python 侧并发时会互相等待（2026-10-19 实测接口偶发超时）
    try:
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() != "wal":
            con.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    con.execute("PRAGMA busy_timeout=30000")
    # 建表脚本只在库版本落后时跑一次：executescript 里的 CREATE ... IF NOT EXISTS
    # 每次请求都执行会反复申请写锁，并发时报 database is locked（2026-10-19 实测）
    ver = 0
    try:
        row = con.execute("SELECT v FROM settings WHERE k='schema_ver'").fetchone()
        ver = int(row[0]) if row else 0
    except Exception:
        ver = 0
    if ver < SCHEMA_VERSION:
        con.executescript(SCHEMA)
    _migrate(con)
    return con


DEFAULT_CONFIG = {
    # 模型来源：deepseek / openai_compat / workbuddy
    "model_source": "deepseek",
    "model_id": "deepseek-flash",
    "api_base": "https://api.deepseek.com",
    "api_key": "",
    "thinking": "disabled",
    # 运行参数
    "sample_ratio": 0.1,
    "min_per_inspector": 1,
    "workers": 3,
    "max_attempts": 6,
    "cost_limit": 0,
    # 照片处理
    "pending_keep_days": 30,
    "similarity_threshold": 0.70,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def get_setting(key, default=None):
    con = connect()
    try:
        r = con.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
        return r["v"] if r else default
    finally:
        con.close()


def set_setting(key, value):
    con = connect()
    try:
        con.execute("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (key, str(value)))
        con.commit()
    finally:
        con.close()


def auto_backup(keep_days=7):
    """每日首次启动自动备份数据库（2026-10-19 加）
    用 SQLite backup API 而不是 cp：库在 WAL 模式下直接复制主文件可能拿到半截数据。
    """
    import datetime as _dt
    bk_dir = os.path.join(BASE, "backups")
    try:
        os.makedirs(bk_dir, exist_ok=True)
        today = _dt.date.today().strftime("%Y%m%d")
        dst_path = os.path.join(bk_dir, "db_%s.sqlite3" % today)
        if not os.path.exists(dst_path) and os.path.exists(DB_PATH):
            src = sqlite3.connect(DB_PATH, timeout=30)
            dst = sqlite3.connect(dst_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        # 清理超期备份
        cutoff = time.time() - keep_days * 86400
        for f in os.listdir(bk_dir):
            p = os.path.join(bk_dir, f)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except Exception:
                pass
        return dst_path
    except Exception:
        return ""


def stats_overview():
    """总览数字"""
    con = connect()
    try:
        return {
            "records": con.execute("SELECT COUNT(*) c FROM records").fetchone()["c"],
            "photos": con.execute("SELECT COUNT(*) c FROM photos WHERE downloaded=1").fetchone()["c"],
            "checks_done": con.execute("SELECT COUNT(*) c FROM checks WHERE status='done'").fetchone()["c"],
            "checks_pending": con.execute("SELECT COUNT(*) c FROM checks WHERE status='pending'").fetchone()["c"],
            "issues": con.execute("SELECT COUNT(*) c FROM issues").fetchone()["c"],
            "truth": con.execute("SELECT COUNT(*) c FROM truth").fetchone()["c"],
        }
    finally:
        con.close()
