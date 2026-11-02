#!/usr/bin/env python3
"""Batch OCR driver for in-scope books.

Reads:  /mnt/AI_Library/library/raw/<category>/*.pdf (18 in-scope dirs)
Writes: /mnt/AI_Library/library/ocr/<category>/<slug>/
    page-NNN.txt  ocr_all.txt  ocr_results.json  metadata.json
  plus  /mnt/AI_Library/library/ocr/{status.json, fail.log, progress.log}

Usage:
  python3 batch_ocr.py --dry-run
  python3 batch_ocr.py --limit 3
  python3 batch_ocr.py --first 10
  python3 batch_ocr.py --category 歩行再建
  python3 batch_ocr.py --reset S
  python3 batch_ocr.py --reset-all

A book is skipped if metadata.json has status=done with matching sha256 and page
count; per-book failure is logged and never aborts the batch.
"""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RAW = Path("/mnt/AI_Library/library/raw")
OUT_BASE = Path("/mnt/AI_Library/library/ocr")
STATUS = OUT_BASE / "status.json"
FAILLOG = OUT_BASE / "fail.log"
PROGRESS = OUT_BASE / "progress.log"

INSCOPE = [
    "脳卒中一般", "川平・ボバース・PNF", "歩行再建", "宗田法", "姿勢の教科書",
    "触診", "上肢・胸郭", "内部障害", "理学療法MOOK", "理学療法マネジメント",
    "画像・薬剤・高次脳", "神経難病", "脊髄損傷", "拘縮評価と治療",
    "整形外科ナビゲーション", "テーピング・足底板", "ポジショニング", "辞書",
]

DPI = 300
LANG = "jpn+eng"
PSM = "3"
RENDER_WORKERS = 4
OCR_WORKERS = 4


def log(msg):
    line = "[{0}] {1}".format(time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(PROGRESS, "a", encoding="utf-8") as f:
            f.write(line + chr(10))
    except OSError:
        pass


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def slug_of(name):
    s = name.replace("/", "_").replace("\\", "_").replace(":", "_")
    s = s.replace("\x00", "").replace("\x01", "").replace("\x02", "")
    s = s.replace(" ", "").replace("\u3000", "").replace("\u00a0", "")
    s = s.lstrip(".").rstrip(".")
    return s.lstrip("_").rstrip("_") or "book"


def page_count(pdf):
    out = subprocess.check_output(["pdfinfo", str(pdf)], stderr=subprocess.DEVNULL)
    for line in out.decode().splitlines():
        if line.startswith("Pages:"):
            return int(line.split()[1])
    raise SystemExit("pdfinfo no Pages: " + str(pdf))


def render(pdf, img_dir, n):
    size = (n + RENDER_WORKERS - 1) // RENDER_WORKERS
    jobs = [(i * size + 1, min(i * size + size, n))
            for i in range((n + RENDER_WORKERS - 1) // size)]
    with ThreadPoolExecutor(max_workers=RENDER_WORKERS) as ex:
        futs = [ex.submit(subprocess.run,
            ["pdftoppm", "-png", "-gray", "-r", str(DPI),
             "-f", str(s), "-l", str(e), str(pdf), str(img_dir / "page")],
            capture_output=True, text=True) for s, e in jobs]
        for fut in futs:
            r = fut.result()
            if r.returncode != 0:
                raise RuntimeError("pdftoppm rc=%d: %s" % (r.returncode,
                    (r.stderr or b"")[-500].decode(errors="replace")))
    for p in sorted(img_dir.glob("page*.png")):
        m = re.fullmatch(r"page-(\d+)(\.png)?", p.name)
        if not m:
            continue
        i = int(m.group(1))
        dest = img_dir / ("page-%03d.png" % i)
        if p != dest:
            p.rename(dest)
    for i in range(1, n + 1):
        if not (img_dir / ("page-%03d.png" % i)).exists():
            raise RuntimeError("missing render page-%03d.png" % i)


def ocr_page(img):
    out_dir = OUT_BASE / "tmp"
    t = subprocess.run(
        ["tesseract", str(img), "stdout", "-l", LANG, "--psm", PSM],
        capture_output=True, text=True)
    return t.stdout


def process_book(pdf, cat, out_dir, sha, n_pages):
    img_dir = out_dir / "_imgs"
    img_dir.mkdir(parents=True, exist_ok=True)
    render(pdf, img_dir, n_pages)
    for i in range(1, n_pages + 1):
        if not (img_dir / ("page-%03d.png" % i)).exists():
            raise RuntimeError("missing render page-%03d.png" % i)
    t0 = time.monotonic()
    items = []
    total = 0
    with ThreadPoolExecutor(max_workers=OCR_WORKERS) as ex:
        futs = [ex.submit(
            ocr_page,
            img_dir / ("page-%03d.png" % i)) for i in range(1, n_pages + 1)]
        order = []
        for i, fut in zip(range(1, n_pages + 1), futs):
            order.append((i, fut))
        texts = {}
        for i, fut in order:
            texts[i] = fut.result()
    for i in range(1, n_pages + 1):
        t = texts[i].strip() + chr(10)
        total += len(t)
        (out_dir / ("page-%03d.txt" % i)).write_text(t, encoding="utf-8")
        items.append({"page": i, "text": t.strip()})
    (out_dir / "ocr_results.json").write_text(
        json.dumps({"source": str(pdf.resolve()), "pages": n_pages,
                    "items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    with open(out_dir / "ocr_all.txt", "w", encoding="utf-8") as f:
        for it in items:
            f.write("=== page %03d ===\n" % it["page"])
            f.write(it["text"])
            f.write("\n\n")
    meta = {
        "source": str(pdf), "category": cat, "slug": slug_of(pdf.name),
        "pages": n_pages, "sha256": sha, "dpi": DPI, "lang": LANG,
        "psm": PSM, "chars": total, "status": "done",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (out_dir / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False,
        indent=2) + chr(10), encoding="utf-8")
    log("done %s/%s pages=%d chars=%d t=%.0fs" % (cat, pdf.name, n_pages,
        total, time.monotonic() - t0))
    try:
        shutil.rmtree(img_dir, ignore_errors=True)
    except Exception:
        pass


def build_manifest():
    rows = []
    for cat in INSCOPE:
        d = RAW / cat
        if not d.is_dir():
            log("warn missing category " + cat)
            continue
        for pdf in sorted(d.glob("*.pdf"), key=lambda p: p.name):
            try:
                rows.append({"category": cat, "slug": slug_of(pdf.name),
                    "file": pdf.name, "pdf": str(pdf),
                    "sha": sha256_of(pdf), "pages": page_count(pdf)})
            except Exception as e:
                log("warn manifest %s: %s" % (pdf, e))
    rows.sort(key=lambda r: (r["category"], r["file"]))
    return rows


def main():
    ap = argparse.ArgumentParser(prog="batch_ocr.py")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--first", type=int, default=0)
    ap.add_argument("--category", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset", default=None, help="slug")
    ap.add_argument("--reset-all", action="store_true")
    args = ap.parse_args()

    OUT_BASE.mkdir(parents=True, exist_ok=True)
    status = {}
    if STATUS.exists():
        status = json.loads(STATUS.read_text(encoding="utf-8"))

    if args.reset_all:
        for p in OUT_BASE.iterdir():
            if p.name.startswith("_"):
                continue
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
        STATUS.unlink(missing_ok=True)
        FAILLOG.unlink(missing_ok=True)
        PROGRESS.unlink(missing_ok=True)
        log("reset: all")
        return

    if args.reset:
        for cat in INSCOPE:
            p = OUT_BASE / cat / args.reset
            if p.exists():
                shutil.rmtree(p)
                log("reset " + args.reset)
        return

    rows = build_manifest()
    if args.category:
        rows = [r for r in rows if r["category"] == args.category]
    log("manifest: %d pdfs" % len(rows))
    if args.dry_run:
        for r in rows:
            print("  %s  %s  %dp  %s" % (r["category"], r["slug"],
                r["pages"], r["sha"][:8]))
        return

    selected = rows[args.first:][: args.limit] if args.limit else rows[args.first:]
    done_count = skipped = failed = 0
    for r in selected:
        slug = r["slug"]
        out_dir = OUT_BASE / r["category"] / slug
        meta_path = out_dir / "metadata.json"
        try:
            if meta_path.exists():
                m = json.loads(meta_path.read_text(encoding="utf-8"))
                if (m.get("status") == "done" and m.get("sha256") == r["sha"]
                        and m.get("pages") == r["pages"]):
                    skipped += 1
                    log("skip %s/%s (done)" % (r["category"], r["file"]))
                    continue
            process_book(Path(r["pdf"]), r["category"], out_dir, r["sha"],
                         r["pages"])
            done_count += 1
        except Exception as e:
            failed += 1
            log("FAIL %s/%s: %s" % (r["category"], r["file"], e))
            with open(FAILLOG, "a", encoding="utf-8") as f:
                f.write("failed=%s category=%s error=%s\n" % (
                    r["file"], r["category"], str(e)))
    log("summary done=%d skipped=%d failed=%d of %d" %
        (done_count, skipped, failed, len(selected)))


if __name__ == "__main__":
    main()
