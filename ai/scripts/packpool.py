"""按 rid 池切分打包：解决 pack 重复领同一批户的问题。
用法: python scripts/packpool.py --start 75 --n 32 --pools 8
先取 256 户待判池，再按 32 户一片生成 b75..b82（--rids 显式指定，互不重叠）。
"""
import argparse, json, os, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--n", type=int, default=32)
    ap.add_argument("--pools", type=int, default=8)
    ap.add_argument("--company", default="C4")
    ap.add_argument("--month", default="2026-09")
    ap.add_argument("--items", default="items2")
    a = ap.parse_args()

    total = a.n * a.pools
    p = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "cli.py"), "next", "--n", str(total),
        "--company", a.company, "--month", a.month, "--items", a.items],
        capture_output=True, text=True, encoding="utf-8")
    raw = (p.stdout or "").strip()
    d = json.loads(raw[raw.find("{"):])
    if d.get("empty"):
        print("没有待判定的户了")
        return
    recs = d["records"]
    print("池内共 %d 户: %d ~ %d" % (len(recs), recs[0]["record_id"], recs[-1]["record_id"]))

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for i in range(a.pools):
        chunk = recs[i * a.n:(i + 1) * a.n]
        if not chunk:
            break
        b = a.start + i
        rids = ",".join(str(r["record_id"]) for r in chunk)
        r = subprocess.run([sys.executable, os.path.join(base, "scripts", "batch.py"),
                            "pack", "--batch", str(b), "--rids", rids,
                            "--company", a.company, "--month", a.month, "--no-sheets"],
                           capture_output=True, text=True, encoding="utf-8")
        print("b%d: %d 户 (%d~%d) %s" % (b, len(chunk),
              chunk[0]["record_id"], chunk[-1]["record_id"],
              "OK" if r.returncode == 0 else "FAIL " + (r.stderr or "")[:80]))


if __name__ == "__main__":
    main()
