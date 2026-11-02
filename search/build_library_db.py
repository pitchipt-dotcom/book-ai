#!/usr/bin/env python3
"""Build a disposable SQLite/FTS search index from read-only OCR results.

This script never modifies OCR, source PDFs, books.json, or index.json.  It
writes search/library.sqlite atomically, so rebuilding is safe while the old
database continues to be readable.
"""
import json
import os
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

SEARCH_DIR = Path(__file__).resolve().parent
OCR_DIR = Path("/mnt/AI_Library/library/ocr")
DB_PATH = SEARCH_DIR / "library.sqlite"
TMP_PATH = SEARCH_DIR / "library.sqlite.tmp"
PAGE_RE = re.compile(r"^page-(\d+)\.txt$")
# Only a terminal numeric range is considered a split-PDF marker.
SPLIT_RE = re.compile(r"^(?P<title>.+?)\s*(?P<start>\d+)-(?P<end>\d+)\.pdf$")


def split_name(slug):
    match = SPLIT_RE.match(slug)
    if not match:
        return None
    start, end = int(match["start"]), int(match["end"])
    if start < 1 or end < start:
        return None
    title = " ".join(match["title"].split())
    return title, start, end


def book_key(title):
    """Stable, readable key; title is metadata-derived and never guessed."""
    return "book:" + title


def load_corrections():
    path = SEARCH_DIR / "corrections.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [(re.compile(rule["re"]), rule["to"]) for rule in data.get("rules", [])]


def apply_corrections(text, rules):
    for pattern, replacement in rules:
        text = pattern.sub(replacement, text)
    return text


def read_parts():
    parts = []
    for meta_path in sorted(OCR_DIR.glob("*/*.pdf/metadata.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("status") != "done":
            continue
        slug = meta.get("slug")
        source = meta.get("source")
        if not slug or not source:
            continue
        pages = []
        try:
            for text_path in sorted(meta_path.parent.glob("page-*.txt")):
                match = PAGE_RE.match(text_path.name)
                if not match:
                    continue
                text = text_path.read_text(encoding="utf-8")
                if text.strip():
                    pages.append((int(match[1]), text))
        except OSError:
            continue
        parts.append({
            "slug": slug,
            "source": source,
            "category": meta.get("category", ""),
            "pdf_pages": int(meta.get("pages") or 0),
            "sha256": meta.get("sha256", ""),
            "pages": pages,
            "split": split_name(slug),
        })
    return parts


def assign_books(parts):
    """Merge only complete, continuous filename-range chains.

    A group with a gap, overlap, or only one part remains separate.  No title
    similarity or OCR content is used for merging.
    """
    candidates = defaultdict(list)
    for part in parts:
        if part["split"]:
            title, start, end = part["split"]
            candidates[title].append((start, end, part))

    merged_part_ids = set()
    groups = []
    for title, members in candidates.items():
        members.sort(key=lambda item: (item[0], item[1], item[2]["slug"]))
        continuous = len(members) > 1 and all(
            members[i][0] == members[i - 1][1] + 1
            for i in range(1, len(members))
        )
        if not continuous:
            continue
        group_parts = []
        for order, (start, end, part) in enumerate(members, start=1):
            part["declared_start"], part["declared_end"] = start, end
            part["part_order"] = order
            group_parts.append(part)
            merged_part_ids.add(id(part))
        groups.append({"book_id": book_key(title), "title": title,
                       "merged": 1, "parts": group_parts})

    for part in parts:
        if id(part) in merged_part_ids:
            continue
        title = part["slug"][:-4] if part["slug"].endswith(".pdf") else part["slug"]
        if part["split"]:
            _, start, end = part["split"]
            part["declared_start"], part["declared_end"] = start, end
        else:
            part["declared_start"], part["declared_end"] = None, None
        part["part_order"] = 1
        groups.append({"book_id": "part:" + part["slug"], "title": title,
                       "merged": 0, "parts": [part]})
    return sorted(groups, key=lambda group: (group["title"], group["book_id"]))


def bigrams(text):
    normalized = text.casefold()
    return " ".join(normalized[i:i + 2] for i in range(len(normalized) - 1)
                    if not normalized[i].isspace() and not normalized[i + 1].isspace())


def create_schema(connection):
    connection.executescript("""
    PRAGMA journal_mode=OFF;
    PRAGMA synchronous=OFF;
    CREATE TABLE books (
      book_id TEXT PRIMARY KEY, title TEXT NOT NULL, merged INTEGER NOT NULL,
      part_count INTEGER NOT NULL, first_pdf_file TEXT NOT NULL
    );
    CREATE TABLE parts (
      part_id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(book_id),
      part_order INTEGER NOT NULL, pdf_file TEXT NOT NULL, source_path TEXT NOT NULL,
      category TEXT, pdf_pages INTEGER NOT NULL, declared_start INTEGER,
      declared_end INTEGER, sha256 TEXT
    );
    CREATE TABLE pages (
      id INTEGER PRIMARY KEY, book_id TEXT NOT NULL, part_id TEXT NOT NULL,
      pdf_file TEXT NOT NULL, source_path TEXT NOT NULL, pdf_page INTEGER NOT NULL,
      book_pdf_page INTEGER NOT NULL, part_order INTEGER NOT NULL,
      text TEXT NOT NULL, citation_id TEXT NOT NULL UNIQUE
    );
    CREATE VIRTUAL TABLE pages_fts USING fts5(tokens, tokenize='unicode61');
    CREATE INDEX pages_book_order ON pages(book_id, part_order, pdf_page);
    """)


def build():
    parts = read_parts()
    groups = assign_books(parts)
    corrections = load_corrections()
    if TMP_PATH.exists():
        TMP_PATH.unlink()
    connection = sqlite3.connect(TMP_PATH)
    try:
        create_schema(connection)
        page_count = 0
        for group in groups:
            group_parts = group["parts"]
            connection.execute("INSERT INTO books VALUES (?, ?, ?, ?, ?)", (
                group["book_id"], group["title"], group["merged"],
                len(group_parts), group_parts[0]["slug"]))
            book_page_base = 0
            for part in group_parts:
                connection.execute("INSERT INTO parts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                    part["slug"], group["book_id"], part["part_order"], part["slug"],
                    part["source"], part["category"], part["pdf_pages"],
                    part["declared_start"], part["declared_end"], part["sha256"]))
                for pdf_page, text in part["pages"]:
                    citation_id = f"{group['book_id']}:{part['slug']}:pdf-p{pdf_page}"
                    search_text = apply_corrections(text, corrections)
                    cursor = connection.execute(
                        "INSERT INTO pages(book_id,part_id,pdf_file,source_path,pdf_page,book_pdf_page,part_order,text,citation_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (group["book_id"], part["slug"], part["slug"], part["source"],
                         pdf_page, book_page_base + pdf_page, part["part_order"], text, citation_id))
                    connection.execute("INSERT INTO pages_fts(rowid, tokens) VALUES (?, ?)",
                                       (cursor.lastrowid, bigrams(search_text)))
                    page_count += 1
                book_page_base += part["pdf_pages"]
        connection.commit()
        # Merge FTS segments before publishing the derived database.
        connection.execute("INSERT INTO pages_fts(pages_fts) VALUES ('optimize')")
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    os.replace(TMP_PATH, DB_PATH)
    merged = sum(1 for group in groups if group["merged"])
    print(f"written: {DB_PATH} (books: {len(groups)}, merged: {merged}, pages: {page_count})")


if __name__ == "__main__":
    build()
