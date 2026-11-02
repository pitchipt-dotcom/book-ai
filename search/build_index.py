#!/usr/bin/env python3
"""Build search/index.json from the real OCR data in
/mnt/AI_Library/library/ocr.

Layout:
    <OCR_DIR>/<source category>/<part>.pdf/
        page-NNN.txt    OCR text of PDF page N
        metadata.json   {status, slug, category, pages, ...}

One book == one part directory (split PDFs are NOT merged).
Part directories without metadata.json or with status != "done" are
skipped (failed / incomplete OCR). Raw data is never modified.

Usage: python3 search/build_index.py
"""
import json
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent  # <repo>
OCR_DIR = Path("/mnt/AI_Library/library/ocr")
BOOKS_OUT = BASE / "books.json"
SEARCH_DIR = Path(__file__).resolve().parent
INDEX_OUT = SEARCH_DIR / "index.json"

PAGE_RE = re.compile(r"^page-(\d+).txt$")
PDF_SUFFIX = ".pdf"
DONE_STATUS = "done"


def load_rules():
    path = SEARCH_DIR / "corrections.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [(re.compile(r["re"]), r["to"]) for r in data.get("rules", [])]


def apply_rules(text, rules):
    for pattern, replacement in rules:
        text = pattern.sub(replacement, text)
    return text


def read_pages(part_dir):
    pages = []
    for f in sorted(part_dir.glob("page-*.txt")):
        m = PAGE_RE.match(f.name)
        if not m:
            continue
        n = int(m.group(1))
        text = f.read_text(encoding="utf-8")
        if not text.strip():
            continue
        pages.append((n, text))
    pages.sort(key=lambda t: t[0])
    return pages


def build():
    rules = load_rules()
    books = {}  # book_id -> {"pdfs": [...], "pages": [...]}
    books_list = []
    pages_total = 0
    skipped_parts = 0
    errors = []

    for source in sorted(OCR_DIR.iterdir()):
        if not source.is_dir():
            continue  # fail.log, ocr_run.log, progress.log, ...
        for part_dir in sorted(source.iterdir()):
            if not part_dir.is_dir():
                continue
            if not part_dir.name.endswith(PDF_SUFFIX):
                continue
            meta_path = part_dir / "metadata.json"
            if not meta_path.exists():
                skipped_parts += 1
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                skipped_parts += 1
                errors.append(f"unreadable metadata: {part_dir.name}: {e}")
                continue
            if not isinstance(meta, dict) or meta.get("status") != DONE_STATUS:
                skipped_parts += 1
                continue
            book_id = meta.get("slug") or part_dir.name
            pdf_file = meta.get("pdf_file") or book_id
            book_title = pdf_file[:-len(PDF_SUFFIX)] if pdf_file.endswith(PDF_SUFFIX) else pdf_file
            page_items = []
            try:
                for pdf_page, text in read_pages(part_dir):
                    page_items.append({
                        "book_id": book_id,
                        "book_title": book_title,
                        "pdf_file": pdf_file,
                        "pdf_page": pdf_page,
                        "text": apply_rules(text, rules),
                    })
                    pages_total += 1
            except OSError as e:
                errors.append(f"page read error: {part_dir.name}: {e}")
                continue
            books[book_id] = {"pdfs": [pdf_file], "pages": page_items}
            books_list.append({
                "book_id": book_id,
                "title": book_title,
                "pdfs": [{"file": pdf_file}],
            })

    INDEX_OUT.write_text(
        json.dumps({"books": books}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    BOOKS_OUT.write_text(
        json.dumps({"books": books_list}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for e in errors:
        print(f"error: {e}")
    print(f"written: {INDEX_OUT}  (books: {len(books)}  pages: {pages_total})")
    print(f"written: {BOOKS_OUT}  (books: {len(books_list)})")
    print(f"skipped parts (no metadata or status != {DONE_STATUS}): {skipped_parts}")


if __name__ == "__main__":
    build()
