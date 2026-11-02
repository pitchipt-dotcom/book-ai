#!/usr/bin/env python3
"""
OCR pipeline for scanned PDF books.

Steps:
 1. pdfinfo -> page count
 2. pdftoppm -png -gray -r <dpi> : render pages in parallel chunks
    (output: img/page-NNN.png, NNN zero-padded)
 3. tesseract -l jpn (default PSM 3) per page in parallel
    (output: out/page-NNN.txt)
 4. Persist:
    out/ocr_results.json  : {"source":..., "pages":N, "items":[{page,text}...]}
    out/ocr_all.txt       : all pages concatenated with page headers
"""
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

LANG = "jpn"


def page_count(pdf: Path) -> int:
    out = subprocess.check_output(["pdfinfo", str(pdf)], stderr=subprocess.DEVNULL)
    for line in out.decode().splitlines():
        if line.startswith("Pages:"):
            return int(line.split()[1])
    raise SystemExit(f"page count not found:\n{out.decode()}")


def render(pdf: Path, tmp: Path, dpi: int, workers: int, n_pages: int):
    size = (n_pages + workers - 1) // workers
    jobs = [(i * size + 1, min(i * size + size, n_pages)) for i in range((n_pages + workers - 1) // size)]
    t0 = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {}
        for s, e in jobs:
            f = ex.submit(
                subprocess.run,
                ["pdftoppm", "-png", "-gray", "-r", str(dpi), "-f", str(s), "-l", str(e), str(pdf), str(tmp / "page")],
                capture_output=True,
            )
            futs[f] = (s, e)
    for fut in as_completed(futs):
        rc = fut.result().returncode
        s, e = futs[fut]
        results.append((s, e, rc))
        if rc != 0:
            raise SystemExit(f"pdftoppm failed pages {s}-{e}: {fut.result().stderr.decode()}")
    print(f"[render] {n_pages} pages in {workers} chunks: {time.monotonic() - t0:.1f}s")
    return sorted(results)


def ocr_page(path: Path, out_dir: Path) -> tuple[Path, int, float]:
    t0 = time.monotonic()
    text = subprocess.check_output(
        ["tesseract", str(path), "stdout", "-l", LANG, "--psm", "3"],
        stderr=subprocess.DEVNULL,
    ).decode("utf-8", errors="replace").strip()
    out_file = out_dir / (path.stem + ".txt")
    out_file.write_text(text, encoding="utf-8")
    return (out_file, len(text), time.monotonic() - t0)


def main():
    args = list(sys.argv[1:])
    if len(args) < 1:
        sys.exit("usage: ocr_pipeline.py <pdf> [out_dir] [dpi] [workers]")
    pdf = Path(args[0])
    if not pdf.is_file():
        sys.exit(f"pdf not found: {pdf}")
    out_dir = Path(args[1] if len(args) > 1 else "ocr")
    dpi = int(args[2]) if len(args) > 2 else 300
    workers = int(args[3]) if len(args) > 3 else 8
    out_dir.mkdir(parents=True, exist_ok=True)
    img_dir = out_dir / "_imgs"
    img_dir.mkdir(parents=True, exist_ok=True)

    n = page_count(pdf)
    print(f"[info] source={pdf} pages={n} dpi={dpi} workers={workers}")

    render(pdf, img_dir, dpi, workers, n)

    missing = [img_dir / f"page-{i:03d}.png" for i in range(1, n + 1) if not (img_dir / f"page-{i:03d}.png").is_file()]
    if missing:
        raise SystemExit(f"missing rendered images: {[f.name for f in missing][:10]}...")

    t0 = time.monotonic()
    pages = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(ocr_page, img_dir / f"page-{i:03d}.png", out_dir) for i in range(1, n + 1)]
        for fut in as_completed(futs):
            path, length, dt = fut.result()
            pages[path.stem] = (length, dt)
    print(f"[ocr] done: {n} pages in {time.monotonic() - t0:.1f}s")

    blank = [k for k, (l, _) in pages.items() if l == 0]
    print(f"[ocr] blank pages: {len(blank)} {blank[:20]}")

    items = [
        {"page": int(k.split("-")[1]), "text": (out_dir / f"{k}.txt").read_text()}
        for k in sorted(pages.keys())
    ]
    json_out = out_dir / "ocr_results.json"
    with open(json_out, "w", encoding="utf-8") as f:
        json.dump(
            {"source": str(pdf.resolve()), "pages": n, "items": items},
            f,
            ensure_ascii=False,
            indent=2,
        )
    with open(out_dir / "ocr_all.txt", "w", encoding="utf-8") as f:
        for it in items:
            f.write(f"=== page {it['page']:03d} ===\n{it['text']}\n\n")
    total_chars = sum(len(it["text"]) for it in items)
    print(f"[done] {n} pages, {total_chars} chars")
    print(f"       {json_out}")
    print(f"       {out_dir / 'ocr_all.txt'}")


if __name__ == "__main__":
    main()
