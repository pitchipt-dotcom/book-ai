#!/usr/bin/env python3
"""Search app backed by a derived SQLite FTS index.  OCR and PDFs are read-only.

Usage: python3 search/app.py [port]
"""
import json
import os
import re
import sqlite3
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, quote, unquote

BASE = os.path.dirname(os.path.abspath(__file__))   # <repo>/search
ROOT = os.path.dirname(BASE)                        # <repo>
BOOKS = os.path.join(ROOT, "books.json")
INDEX = os.path.join(BASE, "index.json")  # legacy static download; never loaded for search
DATABASE = os.path.join(BASE, "library.sqlite")

# Production source PDFs: /mnt/AI_Library/library/ocr/<category>/<part>.pdf/metadata.json
# slug -> {"source": <absolute path to original PDF>, "pages": int}
OCR_BASE = "/mnt/AI_Library/library/ocr"


def build_slug_map():
    slug_map = {}
    try:
        for cat in os.listdir(OCR_BASE):
            catdir = os.path.join(OCR_BASE, cat)
            if not os.path.isdir(catdir):
                continue
            for entry in os.listdir(catdir):
                dirpath = os.path.join(catdir, entry)
                meta_path = os.path.join(dirpath, "metadata.json")
                if not os.path.isfile(meta_path):
                    continue
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except (OSError, json.JSONDecodeError) as e:
                    print(f"warning: bad metadata {meta_path}: {e}")
                    continue
                slug = meta.get("slug")
                source = meta.get("source")
                if slug and source and os.path.isfile(source):
                    slug_map[slug] = {
                        "source": source,
                        "pages": meta.get("pages", 0),
                    }
    except OSError as e:
        print(f"warning: cannot read OCR base {OCR_BASE}: {e}")
    return slug_map


SLUG_MAP = build_slug_map()
print(f"slug map loaded: {len(SLUG_MAP)} source PDFs")

SNIP_BEFORE = 30
SNIP_AFTER = 30
DEFAULT_LIMIT = 50
MAX_LIMIT = 100


def load_corrections():
    path = os.path.join(BASE, "corrections.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            rules = json.load(handle).get("rules", [])
        return [(re.compile(rule["re"]), rule["to"]) for rule in rules]
    except (OSError, json.JSONDecodeError, KeyError):
        return []


CORRECTION_RULES = load_corrections()


def search_text(text):
    """Apply the same non-destructive OCR corrections used to build the FTS index."""
    for pattern, replacement in CORRECTION_RULES:
        text = pattern.sub(replacement, text)
    return text


def _respond(handler, code, ctype, data):
    handler.send_response(code)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def _redirect(handler, location):
    handler.send_response(302)
    handler.send_header("Location", location)
    handler.send_header("Cache-Control", "no-store")
    # HTTP/1.1 keeps connections alive.  An explicit empty body prevents
    # clients from waiting for the server to close the connection after a
    # page-anchor redirect.
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def parse_byte_range(header, size):
    """Return one requested byte range, or None when it is absent/invalid."""
    if not header:
        return 0, size - 1
    if not header.startswith("bytes=") or "," in header:
        return None
    start_text, sep, end_text = header[6:].strip().partition("-")
    if not sep:
        return None
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                return None
            return max(0, size - suffix), size - 1
        start = int(start_text)
        end = size - 1 if not end_text else int(end_text)
    except ValueError:
        return None
    if start < 0 or end < start or start >= size:
        return None
    return start, min(end, size - 1)


def content_disposition(filename):
    """Inline RFC 5987 filename supports Japanese names in mobile browsers."""
    encoded = quote(os.path.basename(filename), safe="")
    return f"inline; filename=\"document.pdf\"; filename*=UTF-8''{encoded}"


def stream_pdf(handler, path, filename, page_url, send_body=True):
    """Serve a source PDF with byte-range support; source data stays read-only."""
    if page_url is not None:
        _redirect(handler, page_url)
        return
    size = os.path.getsize(path)
    byte_range = parse_byte_range(handler.headers.get("Range"), size)
    if byte_range is None:
        handler.send_response(416)
        handler.send_header("Content-Range", f"bytes */{size}")
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
        return
    start, end = byte_range
    length = end - start + 1
    is_partial = handler.headers.get("Range") is not None
    handler.send_response(206 if is_partial else 200)
    handler.send_header("Content-Type", "application/pdf")
    handler.send_header("Content-Length", str(length))
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Disposition", content_disposition(filename))
    handler.send_header("X-Content-Type-Options", "nosniff")
    if is_partial:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    if not send_body:
        return
    with open(path, "rb") as f:
        f.seek(start)
        remaining = length
        while remaining:
            chunk = f.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            handler.wfile.write(chunk)
            remaining -= len(chunk)


def make_snippet(text, match_pos, match_len):
    before = max(0, match_pos - SNIP_BEFORE)
    after = min(len(text), match_pos + match_len + SNIP_AFTER)
    snippet = text[before:after]
    if before > 0:
        snippet = "\u2026" + snippet
    if after < len(text):
        snippet += "\u2026"
    return snippet


def result_item(row, query, match_pos, normalized_text):
    """Create a citation-ready search result without changing OCR text."""
    pdf_file = row["pdf_file"]
    pdf_page = row["pdf_page"]
    source_url = "/pdf/" + quote(str(pdf_file), safe="")
    # A fragment is client-side only, so Android Chrome requests the PDF
    # directly instead of first navigating through a 302 response.
    source_url += "#page=" + quote(str(pdf_page), safe="")
    viewer_url = "/viewer.html?file=" + quote(source_url.split("#", 1)[0], safe="")
    viewer_url += "&page=" + quote(str(pdf_page), safe="")
    title = row["book_title"]
    return {
        "book_id": row["book_id"],
        "book_title": title,
        "part_count": row["part_count"],
        "part_order": row["part_order"],
        "category": row["category"],
        "source_file": pdf_file,
        "pdf_file": pdf_file,
        "pdf_page": pdf_page,
        "book_pdf_page": row["book_pdf_page"],
        "page_type": "pdf",
        "citation_id": row["citation_id"],
        "citation": f"{title}, PDF p.{pdf_page} (book PDF p.{row['book_pdf_page']})",
        "source_url": source_url,
        "viewer_url": viewer_url,
        "source_pdf": row["source_path"],
        "ocr_text": row["text"],
        "search_text": normalized_text,
        "snippet": make_snippet(normalized_text, match_pos, len(query)),
    }


def db_connection():
    """Open the derived index read-only; no request can create or modify it."""
    return sqlite3.connect("file:" + DATABASE + "?mode=ro", uri=True)


def query_grams(query):
    """Terms used by the two-character FTS candidate index.

    Punctuation and whitespace use the conservative full scan fallback so the
    old literal-substring semantics remain exact.
    """
    normalized = query.casefold()
    if len(normalized) < 2 or not all(char.isalnum() for char in normalized):
        return None
    return [normalized[i:i + 2] for i in range(len(normalized) - 1)]


def search_pages(query, book_id, offset=0, limit=DEFAULT_LIMIT):
    """Return a stable page of exact substring matches from SQLite.

    FTS reduces candidates for 2+ character alphanumeric terms.  Python then
    confirms against the untouched OCR text, preserving case-insensitive
    literal substring behaviour exactly.
    """
    q = query.strip()
    if not q:
        return 0, []
    q_lower = q.casefold()
    results = []
    hit_count = 0
    grams = query_grams(q)
    columns = """p.book_id, b.title AS book_title, b.part_count, p.part_order,
                 pt.category, p.pdf_file, p.source_path, p.pdf_page,
                 p.book_pdf_page, p.text, p.citation_id"""
    if grams:
        fts_query = " AND ".join('"' + gram.replace('"', '""') + '"' for gram in grams)
        sql = f"""SELECT {columns} FROM pages_fts f JOIN pages p ON p.id=f.rowid
                  JOIN books b ON b.book_id=p.book_id JOIN parts pt ON pt.part_id=p.part_id
                  WHERE pages_fts MATCH ?"""
        params = [fts_query]
    else:
        sql = f"""SELECT {columns} FROM pages p JOIN books b ON b.book_id=p.book_id
                  JOIN parts pt ON pt.part_id=p.part_id WHERE 1=1"""
        params = []
    if book_id is not None:
        sql += " AND p.book_id=?"
        params.append(book_id)
    sql += " ORDER BY b.title, p.part_order, p.pdf_page"
    with db_connection() as connection:
        connection.row_factory = sqlite3.Row
        for row in connection.execute(sql, params):
            normalized_text = search_text(row["text"])
            match_pos = normalized_text.casefold().find(q_lower)
            if match_pos < 0:
                continue
            if offset <= hit_count < offset + limit:
                results.append(result_item(row, q, match_pos, normalized_text))
            hit_count += 1
    return hit_count, results


def catalog():
    """Grouped catalog for the UI; parts remain explicit for provenance."""
    with db_connection() as connection:
        connection.row_factory = sqlite3.Row
        books = []
        for book in connection.execute("SELECT * FROM books ORDER BY title, book_id"):
            parts = [dict(part) for part in connection.execute(
                "SELECT pdf_file, part_order, pdf_pages, declared_start, declared_end "
                "FROM parts WHERE book_id=? ORDER BY part_order", (book["book_id"],))]
            books.append({**dict(book), "parts": parts})
    return {"books": books}


def parse_nonnegative_int(values, default, maximum=None):
    """Parse one query value safely; invalid values fall back to default."""
    if not values:
        return default
    try:
        value = int(values[0])
    except (TypeError, ValueError):
        return default
    if value < 0:
        return default
    return min(value, maximum) if maximum is not None else value


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _file(self, rel):
        p = os.path.join(BASE, rel)
        if not os.path.abspath(p).startswith(BASE):
            return None
        return p if os.path.isfile(p) else None

    def _static(self, rel, content_type):
        path = self._file(rel)
        if path is None:
            _respond(self, 404, "text/plain", b"Not found")
            return
        with open(path, "rb") as handle:
            _respond(self, 200, content_type, handle.read())

    def do_HEAD(self):
        """Provide PDF-viewer metadata without sending an unnecessary body."""
        full_path, _, query_string = self.path.partition("?")
        if not full_path.startswith("/pdf/"):
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        name = os.path.basename(unquote(full_path[len("/pdf/"):]))
        page = None
        parsed = parse_qs(query_string) if query_string else {}
        page_values = parsed.get("page") or []
        if page_values and page_values[0].lstrip("+-").isdigit():
            page = int(page_values[0])
        entry = SLUG_MAP.get(name)
        if entry is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        page_url = full_path + "#page=" + str(max(1, min(page, entry["pages"]))) if page is not None else None
        stream_pdf(self, entry["source"], name, page_url, send_body=False)

    def do_GET(self):
        full_path, sep, query_string = self.path.partition("?")
        path = full_path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._static("index.html", "text/html; charset=utf-8")
            return
        if path == "/viewer.html":
            self._static("viewer.html", "text/html; charset=utf-8")
            return
        if path in ("/pdfjs/pdf.mjs", "/pdfjs/pdf.worker.mjs"):
            self._static(path.lstrip("/"), "text/javascript; charset=utf-8")
            return
        if path == "/index.json":
            _respond(self, 200, "application/json", open(INDEX, "rb").read())
            return
        if path == "/books.json":
            _respond(self, 200, "application/json", open(BOOKS, "rb").read())
            return
        if path == "/books":
            try:
                payload = json.dumps(catalog(), ensure_ascii=False).encode("utf-8")
            except sqlite3.Error as e:
                _respond(self, 503, "text/plain; charset=utf-8", f"search index unavailable: {e}".encode("utf-8"))
                return
            _respond(self, 200, "application/json; charset=utf-8", payload)
            return
        if path == "/search":
            parsed = parse_qs(query_string)
            q = (parsed.get("q", [""])[0]) if parsed else ""
            book_id = parsed.get("book_id", [None])[0]
            if book_id == "":
                book_id = None
            offset = parse_nonnegative_int(parsed.get("offset"), 0)
            limit = parse_nonnegative_int(parsed.get("limit"), DEFAULT_LIMIT, MAX_LIMIT)
            if limit == 0:
                limit = DEFAULT_LIMIT
            try:
                count, results = search_pages(q, book_id, offset, limit)
            except sqlite3.Error as e:
                _respond(self, 503, "text/plain; charset=utf-8", f"search index unavailable: {e}".encode("utf-8"))
                return
            payload = json.dumps({
                "query": q,
                "book_id": book_id,
                "count": count,
                "offset": offset,
                "limit": limit,
                "returned": len(results),
                "has_more": offset + len(results) < count,
                "results": results,
            }, ensure_ascii=False)
            _respond(self, 200, "application/json; charset=utf-8", payload.encode("utf-8"))
            return
        if path.startswith("/pdf/"):
            name = os.path.basename(unquote(path[len("/pdf/"):]))
            page = None
            parsed = parse_qs(query_string) if query_string else {}
            page_values = parsed.get("page") or []
            if page_values:
                raw_page = page_values[0]
                if raw_page.lstrip("+-").isdigit():
                    page = int(raw_page)
            entry = SLUG_MAP.get(name)
            if entry is not None:
                if page is not None:
                    page = max(1, min(page, entry["pages"]))
                    page_url = path + "#page=" + str(page)
                else:
                    page_url = None
                stream_pdf(self, entry["source"], name, page_url)
                return
            p = os.path.join(ROOT, name)
            if os.path.isfile(p) and os.path.abspath(p).startswith(ROOT):
                stream_pdf(self, p, name, None)
            else:
                _respond(self, 404, "text/plain", b"Not found")
            return
        _respond(self, 404, "text/plain", b"Not found")


if __name__ == "__main__":
    port = 8721
    import sys
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    host = os.environ.get("BOOK_SEARCH_HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"serving on {host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
